"""
SQLite trade journal for run_bot live/simulation diagnostics and analytics.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    """Keep journal payloads queryable when strategy telemetry uses Decimal."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _summarize_adverse_markouts(
    values: list[float],
    *,
    min_samples: int,
    horizon_sec: int,
    lookback_hours: float,
) -> Optional[Dict[str, float | int | str]]:
    """Build a robust calibration from non-negative per-share observations."""
    values = sorted(max(0.0, float(value)) for value in values)
    sample_count = len(values)
    if sample_count < int(min_samples):
        return None
    cap_index = max(0, math.ceil(sample_count * 0.90) - 1)
    p90_cap = values[cap_index]
    adverse = sum(min(value, p90_cap) for value in values) / sample_count
    if adverse <= 0:
        return None
    return {
        "sample_count": sample_count,
        "adverse_markout_per_share": adverse,
        "raw_mean_adverse_markout_per_share": sum(values) / sample_count,
        "winsor_cap_per_share": p90_cap,
        "method": "winsorized_p90_mean",
        "horizon_sec": float(horizon_sec),
        "lookback_hours": float(lookback_hours),
    }


class TradeJournalDB:
    """
    Lightweight SQLite writer.
    - Opens a short-lived connection per write (safe with multi-thread callbacks)
    - Never raises to strategy path; logs and continues
    """

    def __init__(self, db_path: str = "./logs/trade_journal.db") -> None:
        self.db_path = str(Path(db_path))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS strategy_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            mode TEXT NOT NULL,
            test_mode INTEGER NOT NULL,
            maker_mode INTEGER NOT NULL,
            instrument_id TEXT,
            selected_slug TEXT,
            notes_json TEXT
        );

        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            client_order_id TEXT,
            venue_order_id TEXT,
            side TEXT,
            price REAL,
            qty REAL,
            status TEXT,
            reason TEXT,
            instrument_id TEXT,
            token_id TEXT,
            fee_rate_bps INTEGER,
            expected_net_usdc REAL,
            commission_usdc REAL,
            payload_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_order_events_run_ts ON order_events(run_id, ts);
        CREATE INDEX IF NOT EXISTS idx_order_events_client ON order_events(client_order_id);

        -- An Outcome exchange trade ID is one fact even when both the live
        -- runtime and P3 collector observe it concurrently.
        CREATE TABLE IF NOT EXISTS outcome_fill_registry (
            trade_id TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS strategy_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_strategy_events_run_ts ON strategy_events(run_id, ts);

        -- Append-only Binance USDⓈ-M observations for Outcome BTC 1d research.
        -- A dedicated table avoids treating a historical REST backfill as if
        -- it had been observable at the same latency as a live event.
        CREATE TABLE IF NOT EXISTS binance_oi_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            source TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            symbol TEXT NOT NULL,
            exchange_timestamp_ms INTEGER NOT NULL,
            local_received_at_ms INTEGER NOT NULL,
            request_latency_ms REAL NOT NULL,
            open_interest TEXT NOT NULL,
            open_interest_value TEXT,
            mark_price TEXT,
            index_price TEXT,
            taker_buy_notional TEXT,
            taker_sell_notional TEXT,
            taker_imbalance REAL,
            backfilled INTEGER NOT NULL DEFAULT 0,
            raw_payload_hash TEXT NOT NULL,
            raw_payload_json TEXT NOT NULL,
            context_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(source, endpoint, symbol, exchange_timestamp_ms, raw_payload_hash)
        );

        CREATE INDEX IF NOT EXISTS idx_binance_oi_symbol_time
            ON binance_oi_observations(symbol, exchange_timestamp_ms);
        CREATE INDEX IF NOT EXISTS idx_binance_oi_run_time
            ON binance_oi_observations(run_id, exchange_timestamp_ms);

        -- X3 research rows.  Snapshot rows are recomputable derived data, but
        -- retain the exact source ids/timestamps used for each as-of join.
        CREATE TABLE IF NOT EXISTS outcome_oi_feature_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feature_schema_version INTEGER NOT NULL,
            outcome_snapshot_event_id INTEGER NOT NULL,
            outcome_id INTEGER NOT NULL,
            period TEXT NOT NULL,
            snapshot_timestamp_ms INTEGER NOT NULL,
            oi_observation_id INTEGER,
            oi_exchange_timestamp_ms INTEGER,
            oi_local_received_at_ms INTEGER,
            oi_age_ms INTEGER,
            oi_join_direction TEXT NOT NULL,
            oi_backfilled INTEGER NOT NULL DEFAULT 0,
            features_json TEXT NOT NULL,
            labels_json TEXT NOT NULL,
            market_context_json TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            UNIQUE(feature_schema_version, outcome_snapshot_event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_outcome_oi_features_market_time
            ON outcome_oi_feature_rows(outcome_id, period, snapshot_timestamp_ms);

        CREATE TABLE IF NOT EXISTS outcome_oi_fill_feature_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feature_schema_version INTEGER NOT NULL,
            fill_order_event_id INTEGER NOT NULL,
            outcome_id INTEGER NOT NULL,
            period TEXT NOT NULL,
            fill_timestamp_ms INTEGER NOT NULL,
            oi_observation_id INTEGER,
            oi_local_received_at_ms INTEGER,
            oi_age_ms INTEGER,
            features_json TEXT NOT NULL,
            actual_markouts_json TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            UNIQUE(feature_schema_version, fill_order_event_id)
        );
        """
        try:
            with self._connect() as conn:
                conn.executescript(ddl)
                # Seed the registry from journals created before the atomic
                # registry existed.  We preserve raw history for audit, while
                # preventing a restarted collector from inserting it again.
                conn.execute(
                    """
                    INSERT OR IGNORE INTO outcome_fill_registry (trade_id, first_seen_at)
                    SELECT json_extract(payload_json, '$.trade_id'), MIN(ts)
                    FROM order_events
                    WHERE event_type='ORDER_FILLED'
                      AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                      AND COALESCE(json_extract(payload_json, '$.trade_id'), '') <> ''
                    GROUP BY json_extract(payload_json, '$.trade_id')
                    """
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"TradeJournalDB schema init failed: {e}")

    def log_run_start(
        self,
        run_id: str,
        mode: str,
        test_mode: bool,
        maker_mode: bool,
        instrument_id: Optional[str] = None,
        selected_slug: Optional[str] = None,
        notes: Optional[Dict[str, Any]] = None,
    ) -> None:
        sql = """
        INSERT OR REPLACE INTO strategy_runs
        (run_id, started_at, ended_at, mode, test_mode, maker_mode, instrument_id, selected_slug, notes_json)
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    sql,
                    (
                        run_id,
                        _utc_now_iso(),
                        mode,
                        int(test_mode),
                        int(maker_mode),
                        instrument_id,
                        selected_slug,
                        _json_dumps(notes or {}),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"TradeJournalDB log_run_start failed: {e}")

    def log_run_stop(self, run_id: str, notes: Optional[Dict[str, Any]] = None) -> None:
        select_sql = "SELECT notes_json FROM strategy_runs WHERE run_id=?"
        update_sql = "UPDATE strategy_runs SET ended_at=?, notes_json=? WHERE run_id=?"
        try:
            with self._connect() as conn:
                existing_notes: Dict[str, Any] = {}
                row = conn.execute(select_sql, (run_id,)).fetchone()
                if row and row[0]:
                    try:
                        parsed = json.loads(row[0])
                        if isinstance(parsed, dict):
                            existing_notes = parsed
                    except Exception:
                        existing_notes = {}
                merged_notes = {**existing_notes, **(notes or {})}
                conn.execute(
                    update_sql,
                    (_utc_now_iso(), _json_dumps(merged_notes), run_id),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"TradeJournalDB log_run_stop failed: {e}")

    def load_market_guard_counts(self, slug: str) -> Dict[str, int]:
        """Recover per-market risk limits after a process or node restart."""
        slug = str(slug or "")
        if not slug:
            return {"buy_count": 0, "protective_exit_count": 0}
        try:
            with self._connect() as conn:
                buy_row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT client_order_id)
                    FROM order_events
                    WHERE event_type='ORDER_FILLED'
                      AND side='BUY'
                      AND json_extract(payload_json, '$.slug')=?
                    """,
                    (slug,),
                ).fetchone()
                exit_row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT fill.client_order_id)
                    FROM order_events AS fill
                    JOIN order_events AS submit
                      ON submit.client_order_id=fill.client_order_id
                    WHERE fill.event_type='ORDER_FILLED'
                      AND fill.side='SELL'
                      AND submit.event_type='ORDER_TAKER_EXIT_SUBMIT'
                      AND submit.reason IN ('stop_loss', 'invalidation_recovery', 'offside_near_close')
                      AND json_extract(fill.payload_json, '$.slug')=?
                    """,
                    (slug,),
                ).fetchone()
            return {
                "buy_count": int(buy_row[0] or 0),
                "protective_exit_count": int(exit_row[0] or 0),
            }
        except Exception as e:
            logger.debug(f"TradeJournalDB load_market_guard_counts failed: {e}")
            return {"buy_count": 0, "protective_exit_count": 0}

    def load_maker_buy_markout_calibration(
        self,
        *,
        lookback_hours: float,
        horizon_sec: int,
        min_samples: int,
        entry_regime_bucket: Optional[str] = None,
    ) -> Optional[Dict[str, float | int | str]]:
        """Return a robust adverse BUY markout estimate for one entry regime.

        A single arithmetic mean lets a few violent reversals dominate every
        future maker quote.  We cap observations at their own P90 before
        averaging: adverse selection remains a real cost, while one 67.5-cent
        event cannot dictate the cost of an otherwise ordinary regime.
        """
        try:
            with self._connect() as conn:
                where_bucket = ""
                params: list[Any] = [int(horizon_sec), f"-{float(lookback_hours):g} hours"]
                if entry_regime_bucket:
                    where_bucket = " AND json_extract(payload_json, '$.entry_regime_bucket')=?"
                    params.append(str(entry_regime_bucket))
                rows = conn.execute(
                    """
                    SELECT CASE
                      WHEN CAST(json_extract(payload_json, '$.signed_markout_ps') AS REAL) < 0
                      THEN -CAST(json_extract(payload_json, '$.signed_markout_ps') AS REAL)
                      ELSE 0
                    END AS adverse_markout_per_share
                    FROM order_events
                    WHERE event_type='FILL_MARKOUT'
                      AND side='BUY'
                      AND json_extract(payload_json, '$.liquidity_class')='maker'
                      AND CAST(json_extract(payload_json, '$.horizon_sec') AS INTEGER)=?
                      AND julianday(ts) >= julianday('now', ?)
                    """ + where_bucket,
                    params,
                ).fetchall()
            return _summarize_adverse_markouts(
                [float(row[0] or 0.0) for row in rows],
                min_samples=min_samples,
                horizon_sec=horizon_sec,
                lookback_hours=lookback_hours,
            )
        except Exception as e:
            logger.debug(f"TradeJournalDB load maker markout calibration failed: {e}")
            return None

    def load_maker_buy_markout_calibrations(
        self,
        *,
        lookback_hours: float,
        horizon_sec: int,
        min_samples: int,
    ) -> Dict[str, Dict[str, float | int | str]]:
        """Return global fallback plus independently measured entry regimes."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT
                      CASE WHEN CAST(json_extract(payload_json, '$.signed_markout_ps') AS REAL) < 0
                        THEN -CAST(json_extract(payload_json, '$.signed_markout_ps') AS REAL)
                        ELSE 0
                      END AS adverse_markout_per_share,
                      json_extract(payload_json, '$.entry_regime_bucket') AS entry_regime_bucket,
                      CAST(json_extract(payload_json, '$.entry_side_score') AS REAL) AS entry_side_score,
                      CAST(json_extract(payload_json, '$.entry_time_left_sec') AS REAL) AS entry_time_left_sec
                    FROM order_events
                    WHERE event_type='FILL_MARKOUT'
                      AND side='BUY'
                      AND json_extract(payload_json, '$.liquidity_class')='maker'
                      AND CAST(json_extract(payload_json, '$.horizon_sec') AS INTEGER)=?
                      AND julianday(ts) >= julianday('now', ?)
                    """,
                    (int(horizon_sec), f"-{float(lookback_hours):g} hours"),
                ).fetchall()
        except Exception as e:
            logger.debug(f"TradeJournalDB load markout calibrations failed: {e}")
            return {}
        global_values = [float(row[0] or 0.0) for row in rows]
        global_calibration = _summarize_adverse_markouts(
            global_values,
            min_samples=min_samples,
            horizon_sec=horizon_sec,
            lookback_hours=lookback_hours,
        )
        if not global_calibration:
            return {}
        calibrations: Dict[str, Dict[str, float | int | str]] = {
            "global": {**global_calibration, "source": "global_fallback"},
        }
        for bucket in ("10_30", "30_60", "60_plus"):
            calibration = _summarize_adverse_markouts(
                [
                    float(row[0] or 0.0)
                    for row in rows
                    if str(row[1] or "") == bucket
                    and abs(float(row[2] or 0.0)) >= 0.35
                    and 300.0 <= float(row[3] or -1.0) < 600.0
                ],
                lookback_hours=lookback_hours,
                horizon_sec=horizon_sec,
                min_samples=min_samples,
            )
            if calibration:
                calibrations[bucket] = {
                    **calibration,
                    "source": f"entry_regime_bucket:{bucket}",
                }
        return calibrations

    def load_strong_directional_regime_calibrations(
        self,
        *,
        lookback_hours: float,
        min_score_abs: float,
        min_samples: int,
    ) -> Dict[str, Dict[str, float | int]]:
        """Return settled hit-rates for independently measured distance bins.

        This intentionally uses the first qualifying observation per market.
        Repeated quote-cycle telemetry must not turn one market into many
        statistically dependent training examples.  Only bins with enough
        independent settled markets are returned.
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    WITH eligible_candidates AS (
                      SELECT
                        e.id,
                        e.ts,
                        json_extract(e.payload_json, '$.slug') AS slug,
                        CASE json_extract(e.payload_json, '$.main_candidate_side')
                          WHEN 'BUY_UP' THEN 'UP'
                          WHEN 'BUY_DOWN' THEN 'DOWN'
                        END AS outcome,
                        ABS(CAST(json_extract(e.payload_json, '$.main_score') AS REAL)) AS score_abs,
                        CASE json_extract(e.payload_json, '$.main_candidate_side')
                          WHEN 'BUY_UP' THEN CAST(json_extract(e.payload_json, '$.spot_minus_strike') AS REAL)
                          WHEN 'BUY_DOWN' THEN -CAST(json_extract(e.payload_json, '$.spot_minus_strike') AS REAL)
                        END AS signed_spot_distance,
                        CAST(json_extract(e.payload_json, '$.time_left_sec') AS REAL) AS time_left_sec
                      FROM strategy_events e
                      WHERE e.event_type='LIVE_SIGNAL_COMPARE'
                        AND ABS(CAST(json_extract(e.payload_json, '$.main_score') AS REAL)) >= ?
                        AND CAST(json_extract(e.payload_json, '$.main_side_locked') AS INTEGER)=1
                        AND CAST(json_extract(e.payload_json, '$.time_left_sec') AS REAL) >= 300
                        AND CAST(json_extract(e.payload_json, '$.time_left_sec') AS REAL) < 600
                        AND CASE json_extract(e.payload_json, '$.main_candidate_side')
                          WHEN 'BUY_UP' THEN CAST(json_extract(e.payload_json, '$.spot_minus_strike') AS REAL)
                          WHEN 'BUY_DOWN' THEN -CAST(json_extract(e.payload_json, '$.spot_minus_strike') AS REAL)
                        END >= 10
                        AND julianday(e.ts) >= julianday('now', ?)
                    ), candidates AS (
                      SELECT
                        *,
                        ROW_NUMBER() OVER (
                          PARTITION BY slug
                          ORDER BY ts ASC, id ASC
                        ) AS rn
                      FROM eligible_candidates
                    ), settlements AS (
                      SELECT
                        json_extract(payload_json, '$.slug') AS slug,
                        UPPER(json_extract(payload_json, '$.outcome')) AS outcome,
                        ROW_NUMBER() OVER (
                          PARTITION BY json_extract(payload_json, '$.slug')
                          ORDER BY ts DESC, id DESC
                        ) AS rn
                      FROM strategy_events
                      WHERE event_type='MARKET_SETTLEMENT'
                    )
                    SELECT
                      CASE
                        WHEN c.signed_spot_distance >= 10 AND c.signed_spot_distance < 30 THEN '10_30'
                        WHEN c.signed_spot_distance >= 30 AND c.signed_spot_distance < 60 THEN '30_60'
                        WHEN c.signed_spot_distance >= 60 THEN '60_plus'
                      END AS distance_bucket,
                      COUNT(*) AS sample_count,
                      SUM(CASE WHEN c.outcome=s.outcome THEN 1 ELSE 0 END) AS wins
                    FROM candidates c
                    JOIN settlements s ON s.slug=c.slug AND s.rn=1
                    WHERE c.rn=1
                      AND c.outcome IN ('UP', 'DOWN')
                      AND s.outcome IN ('UP', 'DOWN')
                      AND c.signed_spot_distance >= 10
                    GROUP BY distance_bucket
                    """,
                    (float(min_score_abs), f"-{float(lookback_hours):g} hours"),
                ).fetchall()
            calibrated: Dict[str, Dict[str, float | int]] = {}
            for row in rows:
                bucket = str(row[0] or "")
                sample_count = int(row[1] or 0)
                wins = int(row[2] or 0)
                if bucket not in {"10_30", "30_60", "60_plus"} or sample_count < int(min_samples):
                    continue
                calibrated[bucket] = {
                    "sample_count": sample_count,
                    "wins": wins,
                    "losses": sample_count - wins,
                    "win_probability": float(wins / sample_count),
                    "min_score_abs": float(min_score_abs),
                    "lookback_hours": float(lookback_hours),
                }
            return calibrated
        except Exception as e:
            logger.debug(f"TradeJournalDB load strong directional regime calibrations failed: {e}")
            return {}

    def reconcile_redeem_cycle(
        self,
        slug: str,
        redeem_value_usdc: float,
        *,
        tx_hash: str = "",
        condition_id: str = "",
    ) -> Optional[Dict[str, float | bool]]:
        """Reconcile a market's journal PnL to its confirmed redemption cash flow.

        Settlement is initially estimated from the local outcome snapshot.  A
        successful redemption is the authoritative cash event, so it must
        replace that estimate in-place.  Appending another MARKET_CYCLE_PNL
        creates two totals for the same market and makes dashboard sums wrong.
        """
        slug = str(slug or "")
        if not slug:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, payload_json FROM strategy_events
                    WHERE event_type='MARKET_CYCLE_PNL'
                      AND json_extract(payload_json, '$.slug')=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (slug,),
                ).fetchone()
                cash_row = conn.execute(
                    """
                    SELECT
                      COALESCE(SUM(CASE WHEN side='BUY' THEN price * qty +
                        COALESCE(CAST(json_extract(payload_json, '$.effective_fee_usdc') AS REAL), 0.0)
                      ELSE 0 END), 0.0) AS buy_cost,
                      COALESCE(SUM(CASE WHEN side='SELL' THEN price * qty -
                        COALESCE(CAST(json_extract(payload_json, '$.effective_fee_usdc') AS REAL), 0.0)
                      ELSE 0 END), 0.0) AS sell_proceeds
                    FROM order_events
                    WHERE event_type='ORDER_FILLED'
                      AND json_extract(payload_json, '$.slug')=?
                    """,
                    (slug,),
                ).fetchone()
                buy_cost = float(cash_row[0] or 0.0)
                sell_proceeds = float(cash_row[1] or 0.0)
                if buy_cost <= 0 and sell_proceeds <= 0:
                    return None

                redeem_value = max(0.0, float(redeem_value_usdc or 0.0))
                # Keep the two components additive and cash-based.  The
                # resulting combined value is authoritative even when a
                # restart lost the in-memory inventory-cost allocation.
                fill_realized = sell_proceeds
                settlement_pnl = redeem_value - buy_cost
                cycle_combined = fill_realized + settlement_pnl
                reconciled_at = _utc_now_iso()
                reconciliation = {
                    "buy_cost_usdc": buy_cost,
                    "sell_proceeds_usdc": sell_proceeds,
                    "redeem_value_usdc": redeem_value,
                    "cycle_fill_realized_usdc": fill_realized,
                    "cycle_settlement_pnl_usdc": settlement_pnl,
                    "cycle_combined_pnl_usdc": cycle_combined,
                    "cycle_pnl_reconciled_source": "onchain_redeem",
                    "cycle_pnl_reconciled_at": reconciled_at,
                    "redeem_tx_hash": str(tx_hash or ""),
                    "redeem_condition_id": str(condition_id or ""),
                }

                settlement = conn.execute(
                    """
                    SELECT id, payload_json FROM strategy_events
                    WHERE event_type='MARKET_SETTLEMENT'
                      AND json_extract(payload_json, '$.slug')=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (slug,),
                ).fetchone()
                if settlement is not None:
                    settlement_payload = json.loads(settlement[1] or "{}")
                    if not isinstance(settlement_payload, dict):
                        settlement_payload = {}
                    inventory_shares = float(settlement_payload.get("inventory_shares") or 0.0)
                    inventory_cost = float(settlement_payload.get("inventory_cost_usdc") or 0.0)
                    settlement_payload.update(
                        {
                            "redeem_value_usdc": redeem_value,
                            "redeem_per_share": (
                                redeem_value / inventory_shares if inventory_shares > 0 else 0.0
                            ),
                            "settlement_pnl_usdc": redeem_value - inventory_cost,
                            "settlement_reconciled_source": "onchain_redeem",
                            "settlement_reconciled_at": reconciled_at,
                            "redeem_tx_hash": str(tx_hash or ""),
                            "redeem_condition_id": str(condition_id or ""),
                        }
                    )
                    conn.execute(
                        "UPDATE strategy_events SET payload_json=? WHERE id=?",
                        (_json_dumps(settlement_payload), int(settlement[0])),
                    )

                wrote_cycle_pnl = row is None
                if row is not None:
                    cycle_payload = json.loads(row[1] or "{}")
                    if not isinstance(cycle_payload, dict):
                        cycle_payload = {}
                    cycle_payload.update(reconciliation)
                    conn.execute(
                        "UPDATE strategy_events SET payload_json=? WHERE id=?",
                        (_json_dumps(cycle_payload), int(row[0])),
                    )
                conn.commit()
            return {**reconciliation, "wrote_cycle_pnl": wrote_cycle_pnl}
        except Exception as e:
            logger.debug(f"TradeJournalDB reconcile_redeem_cycle failed: {e}")
            return None

    def load_shadow_simulation(self, slug: str) -> Optional[Dict[str, Any]]:
        """Return the latest lifecycle state for a dry-run shadow simulation."""
        slug = str(slug or "")
        if not slug:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT payload_json
                    FROM order_events
                    WHERE event_type IN (
                      'SHADOW_SIM_ENTRY_CANDIDATE',
                      'SHADOW_SIM_ENTRY_REQUOTED',
                      'SHADOW_SIM_ENTRY_CANCELLED',
                      'SHADOW_SIM_ENTRY_FILLED',
                      'SHADOW_SIM_ENTRY_EXPIRED',
                      'SHADOW_SIM_SETTLED'
                    )
                      AND json_extract(payload_json, '$.slug')=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (slug,),
                ).fetchone()
            if not row or not row[0]:
                return None
            payload = json.loads(row[0])
            return payload if isinstance(payload, dict) else None
        except Exception as e:
            logger.debug(f"TradeJournalDB load_shadow_simulation failed: {e}")
            return None

    def load_fair_edge_bucket_shadow_simulations(self, slug: str) -> list[Dict[str, Any]]:
        """Return the latest persisted state for each fair-edge research candidate."""
        slug = str(slug or "")
        if not slug:
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT payload_json
                    FROM order_events
                    WHERE event_type IN (
                      'FAIR_EDGE_BUCKET_SHADOW_CANDIDATE',
                      'FAIR_EDGE_BUCKET_SHADOW_FILLED',
                      'FAIR_EDGE_BUCKET_SHADOW_EXPIRED',
                      'FAIR_EDGE_BUCKET_SHADOW_SETTLED'
                    )
                      AND json_extract(payload_json, '$.slug')=?
                    ORDER BY id ASC
                    """,
                    (slug,),
                ).fetchall()
            states: Dict[str, Dict[str, Any]] = {}
            for (raw_payload,) in rows:
                payload = json.loads(raw_payload or "{}")
                if not isinstance(payload, dict):
                    continue
                simulation_id = str(payload.get("simulation_id") or "")
                if simulation_id:
                    states[simulation_id] = payload
            return list(states.values())
        except Exception as e:
            logger.debug(f"TradeJournalDB load_fair_edge_bucket_shadow_simulations failed: {e}")
            return []

    def log_strategy_event(self, run_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        sql = "INSERT INTO strategy_events (ts, run_id, event_type, payload_json) VALUES (?, ?, ?, ?)"
        try:
            with self._connect() as conn:
                conn.execute(
                    sql,
                    (
                        _utc_now_iso(),
                        run_id,
                        event_type,
                        _json_dumps(payload or {}),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"TradeJournalDB log_strategy_event failed: {e}")

    def upsert_outcome_oi_feature_row(self, *, feature_schema_version: int, outcome_snapshot_event_id: int,
                                      outcome_id: int, period: str, snapshot_timestamp_ms: int,
                                      oi_observation_id: int | None, oi_exchange_timestamp_ms: int | None,
                                      oi_local_received_at_ms: int | None, oi_age_ms: int | None,
                                      oi_join_direction: str, oi_backfilled: bool, features: Dict[str, Any],
                                      labels: Dict[str, Any], market_context: Dict[str, Any]) -> bool:
        """Persist a derived X3 row without making it part of execution state."""
        sql = """
        INSERT INTO outcome_oi_feature_rows (
          feature_schema_version,outcome_snapshot_event_id,outcome_id,period,snapshot_timestamp_ms,
          oi_observation_id,oi_exchange_timestamp_ms,oi_local_received_at_ms,oi_age_ms,
          oi_join_direction,oi_backfilled,features_json,labels_json,market_context_json,generated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(feature_schema_version,outcome_snapshot_event_id) DO UPDATE SET
          oi_observation_id=excluded.oi_observation_id, oi_exchange_timestamp_ms=excluded.oi_exchange_timestamp_ms,
          oi_local_received_at_ms=excluded.oi_local_received_at_ms, oi_age_ms=excluded.oi_age_ms,
          oi_join_direction=excluded.oi_join_direction, oi_backfilled=excluded.oi_backfilled,
          features_json=excluded.features_json, labels_json=excluded.labels_json,
          market_context_json=excluded.market_context_json, generated_at=excluded.generated_at
        """
        try:
            with self._connect() as conn:
                conn.execute(sql, (feature_schema_version, outcome_snapshot_event_id, outcome_id, period,
                    snapshot_timestamp_ms, oi_observation_id, oi_exchange_timestamp_ms,
                    oi_local_received_at_ms, oi_age_ms, oi_join_direction, int(oi_backfilled),
                    _json_dumps(features), _json_dumps(labels), _json_dumps(market_context), _utc_now_iso()))
                conn.commit()
            return True
        except Exception as e:
            logger.debug(f"TradeJournalDB upsert_outcome_oi_feature_row failed: {e}")
            return False

    def upsert_outcome_oi_fill_feature_row(self, *, feature_schema_version: int, fill_order_event_id: int,
                                           outcome_id: int, period: str, fill_timestamp_ms: int,
                                           oi_observation_id: int | None, oi_local_received_at_ms: int | None,
                                           oi_age_ms: int | None, features: Dict[str, Any],
                                           actual_markouts: Dict[str, Any]) -> bool:
        """Persist fill-time OI conditioning plus only exchange-confirmed P3 markouts."""
        sql = """
        INSERT INTO outcome_oi_fill_feature_rows (
          feature_schema_version,fill_order_event_id,outcome_id,period,fill_timestamp_ms,
          oi_observation_id,oi_local_received_at_ms,oi_age_ms,features_json,actual_markouts_json,generated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(feature_schema_version,fill_order_event_id) DO UPDATE SET
          oi_observation_id=excluded.oi_observation_id,oi_local_received_at_ms=excluded.oi_local_received_at_ms,
          oi_age_ms=excluded.oi_age_ms,features_json=excluded.features_json,
          actual_markouts_json=excluded.actual_markouts_json,generated_at=excluded.generated_at
        """
        try:
            with self._connect() as conn:
                conn.execute(sql, (feature_schema_version, fill_order_event_id, outcome_id, period, fill_timestamp_ms,
                    oi_observation_id, oi_local_received_at_ms, oi_age_ms, _json_dumps(features),
                    _json_dumps(actual_markouts), _utc_now_iso()))
                conn.commit()
            return True
        except Exception as e:
            logger.debug(f"TradeJournalDB upsert_outcome_oi_fill_feature_row failed: {e}")
            return False

    def record_binance_oi_observation(
        self,
        *,
        run_id: str,
        source: str,
        endpoint: str,
        symbol: str,
        exchange_timestamp_ms: int,
        local_received_at_ms: int,
        request_latency_ms: float,
        open_interest: str,
        raw_payload_hash: str,
        raw_payload: Dict[str, Any],
        open_interest_value: Optional[str] = None,
        mark_price: Optional[str] = None,
        index_price: Optional[str] = None,
        taker_buy_notional: Optional[str] = None,
        taker_sell_notional: Optional[str] = None,
        taker_imbalance: Optional[float] = None,
        backfilled: bool = False,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Durably append one validated public Binance OI observation.

        The uniqueness key makes current polling and startup backfill safe to
        repeat after a restart.  ``backfilled`` is intentionally immutable
        provenance rather than a convenience flag for model code to ignore.
        """
        sql = """
        INSERT OR IGNORE INTO binance_oi_observations (
            run_id, source, endpoint, symbol, exchange_timestamp_ms,
            local_received_at_ms, request_latency_ms, open_interest,
            open_interest_value, mark_price, index_price,
            taker_buy_notional, taker_sell_notional, taker_imbalance,
            backfilled, raw_payload_hash, raw_payload_json, context_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    sql,
                    (
                        run_id, source, endpoint, symbol, int(exchange_timestamp_ms),
                        int(local_received_at_ms), float(request_latency_ms), str(open_interest),
                        open_interest_value, mark_price, index_price,
                        taker_buy_notional, taker_sell_notional, taker_imbalance,
                        int(bool(backfilled)), raw_payload_hash, _json_dumps(raw_payload),
                        _json_dumps(context or {}),
                    ),
                )
                conn.commit()
                return cursor.rowcount == 1
        except Exception as e:
            logger.debug(f"TradeJournalDB record_binance_oi_observation failed: {e}")
            return False

    def log_order_event(
        self,
        run_id: str,
        event_type: str,
        client_order_id: Optional[str] = None,
        venue_order_id: Optional[str] = None,
        side: Optional[str] = None,
        price: Optional[float] = None,
        qty: Optional[float] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        instrument_id: Optional[str] = None,
        token_id: Optional[str] = None,
        fee_rate_bps: Optional[int] = None,
        expected_net_usdc: Optional[float] = None,
        commission_usdc: Optional[float] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        sql = """
        INSERT INTO order_events (
            ts, run_id, event_type, client_order_id, venue_order_id, side, price, qty, status, reason,
            instrument_id, token_id, fee_rate_bps, expected_net_usdc, commission_usdc, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    sql,
                    (
                        _utc_now_iso(),
                        run_id,
                        event_type,
                        client_order_id,
                        venue_order_id,
                        side,
                        price,
                        qty,
                        status,
                        reason,
                        instrument_id,
                        token_id,
                        fee_rate_bps,
                        expected_net_usdc,
                        commission_usdc,
                        _json_dumps(payload or {}),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"TradeJournalDB log_order_event failed: {e}")

    def log_outcome_fill_once(
        self,
        run_id: str,
        *,
        trade_id: str,
        client_order_id: Optional[str] = None,
        venue_order_id: Optional[str] = None,
        side: Optional[str] = None,
        price: Optional[float] = None,
        qty: Optional[float] = None,
        status: Optional[str] = "FILLED",
        instrument_id: Optional[str] = None,
        commission_usdc: Optional[float] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Atomically persist one HIP-4 fill across every local writer."""
        normalized_trade_id = str(trade_id or "").strip()
        if not normalized_trade_id:
            raise ValueError("Outcome fill requires a non-empty exchange trade_id")
        sql = """
        INSERT INTO order_events (
            ts, run_id, event_type, client_order_id, venue_order_id, side, price, qty, status,
            instrument_id, commission_usdc, payload_json
        ) VALUES (?, ?, 'ORDER_FILLED', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._connect() as conn:
                claimed = conn.execute(
                    "INSERT OR IGNORE INTO outcome_fill_registry (trade_id, first_seen_at) VALUES (?, ?)",
                    (normalized_trade_id, _utc_now_iso()),
                )
                if claimed.rowcount != 1:
                    return False
                conn.execute(
                    sql,
                    (
                        _utc_now_iso(), run_id, client_order_id, venue_order_id, side, price, qty,
                        status, instrument_id, commission_usdc, _json_dumps(payload or {}),
                    ),
                )
                conn.commit()
            return True
        except Exception as e:
            logger.debug(f"TradeJournalDB log_outcome_fill_once failed: {e}")
            return False

    def verified_outcome_fill_vwap_for_inventory(
        self, *, coin: str, inventory: Decimal,
    ) -> Optional[Decimal]:
        """Rebuild remaining FIFO lots from locally verified HIP-4 fills.

        This is deliberately a *fallback* to the exchange's ``userFills``
        response.  It is usable only when the durable journal reconstructs the
        exact currently reconciled inventory, so a partial local history can
        never turn into an invented entry price.
        """
        if inventory <= 0:
            return None
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT side, price, qty
                    FROM order_events
                    WHERE event_type='ORDER_FILLED'
                      AND instrument_id=?
                      AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                      AND json_extract(payload_json, '$.actual_fill')=1
                    ORDER BY CAST(json_extract(payload_json, '$.timestamp_ms') AS INTEGER), id
                    """,
                    (str(coin),),
                ).fetchall()
        except (sqlite3.Error, ValueError):
            return None

        lots: list[list[Decimal]] = []
        try:
            for side, price_raw, qty_raw in rows:
                quantity, price = Decimal(str(qty_raw)), Decimal(str(price_raw))
                if quantity <= 0 or not Decimal("0") < price < Decimal("1"):
                    return None
                normalized_side = str(side or "").upper()
                if normalized_side == "BUY":
                    lots.append([quantity, price])
                elif normalized_side == "SELL":
                    remaining = quantity
                    while remaining > 0 and lots:
                        lot = lots[0]
                        consumed = min(remaining, lot[0])
                        lot[0] -= consumed
                        remaining -= consumed
                        if lot[0] == 0:
                            lots.pop(0)
                    if remaining > 0:
                        return None
                else:
                    return None
        except (ArithmeticError, ValueError):
            return None

        reconstructed = sum((lot[0] for lot in lots), Decimal("0"))
        if reconstructed != inventory:
            return None
        return sum((lot[0] * lot[1] for lot in lots), Decimal("0")) / inventory

    def repair_duplicate_outcome_fills(self, *, run_id: str, dry_run: bool = True) -> Dict[str, int]:
        """Remove only proven duplicate HIP-4 fill rows, preserving the first fact.

        This maintenance action is intentionally explicit: callers must stop
        live writers before applying it.  P3 markouts key by exchange trade id
        and are retained because one per horizon is still the same fact.
        """
        duplicate_ids: list[int] = []
        try:
            with self._connect() as conn:
                rows = conn.execute("""
                    SELECT id, json_extract(payload_json, '$.trade_id') AS trade_id
                    FROM order_events
                    WHERE event_type='ORDER_FILLED'
                      AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                      AND COALESCE(json_extract(payload_json, '$.trade_id'), '') <> ''
                    ORDER BY trade_id, id
                """).fetchall()
                seen: set[str] = set()
                for event_id, trade_id in rows:
                    if str(trade_id) in seen:
                        duplicate_ids.append(int(event_id))
                    else:
                        seen.add(str(trade_id))
                if not dry_run and duplicate_ids:
                    marks = ",".join("?" for _ in duplicate_ids)
                    conn.execute(f"DELETE FROM outcome_oi_fill_feature_rows WHERE fill_order_event_id IN ({marks})", duplicate_ids)
                    conn.execute(f"DELETE FROM order_events WHERE id IN ({marks})", duplicate_ids)
                conn.commit()
            self.log_strategy_event(run_id, "OUTCOME_FILL_DEDUPE_REPAIR", {
                "venue": "hyperliquid_outcome", "dry_run": dry_run,
                "duplicate_order_event_ids": duplicate_ids, "removed_count": 0 if dry_run else len(duplicate_ids),
                "preserved_rule": "lowest_order_events.id_per_exchange_trade_id",
            })
            return {"duplicate_count": len(duplicate_ids), "removed_count": 0 if dry_run else len(duplicate_ids)}
        except Exception as e:
            logger.warning(f"TradeJournalDB repair_duplicate_outcome_fills failed: {e}")
            return {"duplicate_count": 0, "removed_count": 0}

    def last_binance_live_received_at_ms(self, *, symbol: str = "BTCUSDT") -> Optional[int]:
        try:
            with self._connect() as conn:
                row = conn.execute("""
                    SELECT MAX(local_received_at_ms) FROM binance_oi_observations
                    WHERE symbol=? AND backfilled=0
                """, (symbol,)).fetchone()
            return int(row[0]) if row and row[0] is not None else None
        except Exception as e:
            logger.debug(f"TradeJournalDB last_binance_live_received_at_ms failed: {e}")
            return None

    def load_recent_buy_submits(self, instrument_id: str, limit: int = 20) -> list[Dict[str, Any]]:
        if not instrument_id:
            return []
        sql = """
        SELECT ts, client_order_id, price, qty, payload_json
        FROM order_events
        WHERE event_type = 'ORDER_SUBMIT'
          AND UPPER(COALESCE(side, '')) = 'BUY'
          AND (
              instrument_id = ?
              OR json_extract(payload_json, '$.submitted_instrument_id') = ?
              OR json_extract(payload_json, '$.instrument_id') = ?
          )
        ORDER BY id DESC
        LIMIT ?
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, (instrument_id, instrument_id, instrument_id, int(limit))).fetchall()
        except Exception as e:
            logger.debug(f"TradeJournalDB load_recent_buy_submits failed: {e}")
            return []

        out: list[Dict[str, Any]] = []
        for ts, client_order_id, price, qty, payload_json in rows:
            payload: Dict[str, Any] = {}
            try:
                parsed = json.loads(payload_json or "{}")
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}
            epoch_ts = 0.0
            try:
                epoch_ts = datetime.fromisoformat(str(ts)).timestamp()
            except Exception:
                epoch_ts = 0.0
            out.append(
                {
                    "ts": ts,
                    "epoch_ts": epoch_ts,
                    "client_order_id": client_order_id,
                    "price": price,
                    "qty": qty,
                    "payload": payload,
                }
            )
        return out

    def load_latest_locked_strike(self, slug: str) -> Optional[Dict[str, Any]]:
        if not slug:
            return None
        sql = """
        SELECT ts, payload_json
        FROM strategy_events
        WHERE event_type = 'MARKET_STRIKE_LOCKED'
        ORDER BY id DESC
        LIMIT 200
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(sql).fetchall()
            for ts, payload_json in rows:
                try:
                    payload = json.loads(payload_json or "{}")
                except Exception:
                    continue
                if str(payload.get("slug") or "") != str(slug):
                    continue
                strike = payload.get("strike")
                source = str(payload.get("strike_source") or "")
                if strike is None or not source:
                    continue
                strike_dec = Decimal(str(strike))
                if strike_dec <= 0:
                    continue
                return {
                    "ts": str(ts or ""),
                    "slug": str(slug),
                    "strike": strike_dec,
                    "strike_source": source,
                    "authoritative": bool(payload.get("authoritative", False)),
                    "sample_dt_sec": payload.get("sample_dt_sec"),
                }
        except Exception as e:
            logger.debug(f"TradeJournalDB load_latest_locked_strike failed: {e}")
        return None
