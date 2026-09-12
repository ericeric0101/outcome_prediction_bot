"""Read-only, per-tick journal view for the live Outcome orchestrator."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec


class OutcomeRuntimeJournalView:
    """Centralize hot-path reads and memoize identical queries for one tick.

    Durable intents, order events and fill reconciliation deliberately do not
    pass through this cache; this object is read-only and cannot authorize an
    exchange mutation.
    """

    def __init__(self, db_path: str | Path | None) -> None:
        self.db_path = str(db_path) if db_path is not None else None
        self._tick_cache: dict[tuple[object, ...], object] = {}
        self._provenance_cache: dict[tuple[object, ...], dict[str, object]] = {}

    def begin_tick(self) -> None:
        self._tick_cache.clear()

    def _connect(self) -> sqlite3.Connection:
        if self.db_path is None:
            raise sqlite3.OperationalError("journal unavailable")
        return sqlite3.connect(f"file:{Path(self.db_path).resolve()}?mode=ro", uri=True)

    def daily_calibration_entries(self) -> int:
        key = ("daily_calibration_entries",)
        if key not in self._tick_cache:
            try:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM strategy_events "
                        "WHERE event_type='OUTCOME_P3_CALIBRATION_ENTRY_PLACED' AND date(ts)=date('now')"
                    ).fetchone()
                self._tick_cache[key] = int(row[0] or 0)
            except sqlite3.Error:
                self._tick_cache[key] = 0
        return int(self._tick_cache[key])

    def continuation_entry_already_submitted(self, outcome_id: int) -> bool:
        key = ("continuation", int(outcome_id))
        if key in self._tick_cache:
            return bool(self._tick_cache[key])
        # Fail closed on unavailable journal.
        found = True
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT payload_json FROM strategy_events "
                    "WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED' ORDER BY id DESC LIMIT 500"
                ).fetchall()
            found = False
            for (raw,) in rows:
                try:
                    payload = json.loads(raw or "{}")
                    if int(payload.get("outcome_id")) == outcome_id and payload.get("entry_tier") == "tier_c_trend_continuation":
                        found = True
                        break
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        except sqlite3.Error:
            found = True
        self._tick_cache[key] = found
        return found

    def persisted_entry_policy(self, market: OutcomeMarketSpec, coin: str) -> tuple[str, dict[str, object]] | None:
        key = ("entry_policy", market.outcome_id, coin)
        if key in self._tick_cache:
            cached = self._tick_cache[key]
            return cached if isinstance(cached, tuple) else None
        result: tuple[str, dict[str, object]] | None = None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """SELECT ts, payload_json FROM strategy_events
                       WHERE event_type IN ('OUTCOME_P3_CALIBRATION_ENTRY_PLACED', 'OUTCOME_LIVE_STRATEGY_ENTRY_PLACED')
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=?
                       ORDER BY id DESC LIMIT 1""",
                    (market.outcome_id, coin),
                ).fetchone()
                if row:
                    timestamp, raw_payload = str(row[0]), row[1]
                    payload = json.loads(raw_payload)
                    if isinstance(payload, dict):
                        valid = True
                        if str(payload.get("sampling_policy", "")) in {"oi_spot_mark_confirmation", "spot_mark_tier_b"}:
                            order_id = str(payload.get("order_id") or "")
                            if order_id:
                                audit_row = conn.execute(
                                    """SELECT payload_json FROM order_events
                                       WHERE event_type='ORDER_SUBMIT' AND side='BUY' AND venue_order_id=?
                                       ORDER BY id DESC LIMIT 1""", (order_id,),
                                ).fetchone()
                                if audit_row:
                                    order_payload = json.loads(audit_row[0] or "{}")
                                    audit = order_payload.get("audit") if isinstance(order_payload, dict) else None
                                    if isinstance(audit, dict) and audit.get("entry_policy_schema_version") == 1:
                                        valid = all(str(audit.get(field)) == str(payload.get(field)) for field in ("target_return_pct", "maker_close_fee_rate"))
                        if valid:
                            result = (timestamp, payload)
                else:
                    rows = conn.execute(
                        """SELECT ts, payload_json FROM order_events
                           WHERE event_type='ORDER_SUBMIT' AND side='BUY' AND instrument_id=?
                             AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                           ORDER BY id DESC LIMIT 20""", (coin, market.outcome_id),
                    ).fetchall()
                    for timestamp, raw_payload in rows:
                        order_payload = json.loads(raw_payload or "{}")
                        audit = order_payload.get("audit") if isinstance(order_payload, dict) else None
                        if (
                            isinstance(audit, dict) and audit.get("entry_policy_schema_version") == 1
                            and audit.get("entry_policy_kind") in {"s0_oi_spot_mark_confirmation", "s0_spot_mark_tier_b"}
                        ):
                            result = (str(timestamp), audit)
                            break
        except (KeyError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError):
            result = None
        self._tick_cache[key] = result
        return result

    def latest_strategy_entry(self, market: OutcomeMarketSpec, coin: str) -> tuple[str, dict[str, Any]] | None:
        key = ("latest_strategy_entry", market.outcome_id, coin)
        if key in self._tick_cache:
            cached = self._tick_cache[key]
            return cached if isinstance(cached, tuple) else None
        result = None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """SELECT ts, payload_json FROM strategy_events
                       WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED'
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=? ORDER BY id DESC LIMIT 1""",
                    (market.outcome_id, coin),
                ).fetchone()
            if row:
                payload = json.loads(row[1] or "{}")
                if isinstance(payload, dict):
                    result = (str(row[0]), payload)
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            result = None
        self._tick_cache[key] = result
        return result

    def live_entry_age_sec(self, market: OutcomeMarketSpec, coin: str) -> float | None:
        entry = self.latest_strategy_entry(market, coin)
        try:
            return max(0.0, time.time() - datetime.fromisoformat(str(entry[0])).timestamp()) if entry else None
        except (TypeError, ValueError):
            return None

    def exact_holding_provenance(
        self, market: OutcomeMarketSpec, coin: str, inventory: Decimal, fill_vwap: Decimal,
    ) -> dict[str, object] | None:
        key = (market.outcome_id, coin, str(inventory), str(fill_vwap))
        if key in self._provenance_cache:
            return dict(self._provenance_cache[key])
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """SELECT ts, venue_order_id, price, qty, payload_json
                       FROM order_events
                       WHERE event_type='ORDER_FILLED' AND side='BUY' AND instrument_id=?
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=?
                         AND json_extract(payload_json, '$.actual_fill')=1
                       ORDER BY id DESC LIMIT 1""", (coin, market.outcome_id, coin),
                ).fetchone()
                if row is None:
                    return None
                journal_filled_at, order_id, price, quantity, fill_raw = row
                if Decimal(str(quantity)) != inventory or Decimal(str(price)) != fill_vwap:
                    return None
                entry_row = conn.execute(
                    """SELECT payload_json FROM strategy_events
                       WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED'
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=?
                         AND json_extract(payload_json, '$.order_id')=?
                       ORDER BY id DESC LIMIT 1""", (market.outcome_id, coin, str(order_id)),
                ).fetchone()
            fill_payload = json.loads(fill_raw or "{}")
            entry_payload = json.loads(entry_row[0] or "{}") if entry_row else {}
            if not isinstance(fill_payload, dict) or not isinstance(entry_payload, dict):
                return None
            trade_id = str(fill_payload.get("trade_id") or "")
            if not trade_id:
                return None
            try:
                official_timestamp_ms = int(fill_payload.get("timestamp_ms"))
                if official_timestamp_ms <= 0:
                    raise ValueError
                filled_at = datetime.fromtimestamp(official_timestamp_ms / 1000, tz=timezone.utc).isoformat()
                source = "official_fill_timestamp_ms"
            except (TypeError, ValueError, OSError, OverflowError):
                filled_at, source = str(journal_filled_at), "legacy_journal_fill_timestamp"
            filled_epoch = datetime.fromisoformat(filled_at).timestamp()
            provenance = {
                "entry_lifecycle_id": f"official_buy:{order_id}:{trade_id}",
                "entry_order_id": str(order_id), "entry_trade_id": trade_id,
                "entry_filled_at": filled_at,
                "entry_side_index": int(entry_payload.get("side_index")),
                "entry_filled_at_source": source,
                "entry_tier": str(entry_payload.get("entry_tier") or "unknown"),
                "entry_target_return_pct": (str(entry_payload.get("target_return_pct"))
                                            if entry_payload.get("target_return_pct") is not None else None),
                "entry_time_left_sec": max(0.0, float(market.expiry_timestamp) - filled_epoch),
            }
            self._provenance_cache[key] = provenance
            return dict(provenance)
        except (TypeError, ValueError, ArithmeticError, sqlite3.Error, json.JSONDecodeError):
            return None
