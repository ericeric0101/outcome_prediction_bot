"""X3: leak-free Outcome 1d / Binance OI feature and executable-label builder.

This module is deliberately offline and read-only with respect to venues.  A
Binance row is eligible only if it had reached this machine before the Outcome
snapshot; historical backfills are excluded by default.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from bot.outcome_p2_quality import is_eligible_p2_snapshot
from bot.outcome_markout import P3_MARKOUT_HORIZONS_SEC, P3_MARKOUT_SCHEMA_VERSION
from monitoring.trade_journal_db import TradeJournalDB

FEATURE_SCHEMA_VERSION = 2
# 15m is intentionally explicit: it is the fast provisional X4a horizon for
# the 1d contract, not a claim that an Outcome 15m market exists.
LABEL_HORIZONS_SEC = (60, 300, 600, 900, 1800, 3600)
LABEL_TOLERANCE_MS = 120_000


def _number(value: Any) -> float | None:
    try:
        result = float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bbo(book: Mapping[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    levels = book.get("levels") if isinstance(book, Mapping) else None
    if not isinstance(levels, list) or len(levels) != 2:
        return None, None, None, None
    bids, asks = levels
    bid = _number(bids[0].get("px")) if bids and isinstance(bids[0], Mapping) else None
    ask = _number(asks[0].get("px")) if asks and isinstance(asks[0], Mapping) else None
    bid_size = _number(bids[0].get("sz")) if bids and isinstance(bids[0], Mapping) else None
    ask_size = _number(asks[0].get("sz")) if asks and isinstance(asks[0], Mapping) else None
    return bid, ask, bid_size, ask_size


@dataclass(frozen=True)
class _Oi:
    id: int
    exchange_timestamp_ms: int
    local_received_at_ms: int
    open_interest: float
    mark_price: float | None
    taker_imbalance: float | None
    backfilled: bool


@dataclass(frozen=True)
class X3BuildResult:
    eligible_snapshots: int
    rows_written: int
    oi_joined: int
    labels_available: dict[int, int]
    maker_fill_rows: int


class OutcomeOiFeaturePipeline:
    """Builds per-snapshot research rows; never mutates strategy/runtime state."""

    def __init__(self, journal: TradeJournalDB, *, include_backfilled: bool = False) -> None:
        self.journal = journal
        self.include_backfilled = include_backfilled

    def _snapshots(self, conn: sqlite3.Connection) -> list[tuple[int, dict[str, Any]]]:
        rows = conn.execute("SELECT id, payload_json FROM strategy_events WHERE event_type='OUTCOME_P2_PARITY_SNAPSHOT' ORDER BY id").fetchall()
        output = []
        for event_id, raw in rows:
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("period") != "1d" or not is_eligible_p2_snapshot(payload):
                continue
            output.append((int(event_id), payload))
        return output

    def _observations(self, conn: sqlite3.Connection) -> list[_Oi]:
        where = "" if self.include_backfilled else "WHERE backfilled=0"
        rows = conn.execute(f"""
            SELECT id,exchange_timestamp_ms,local_received_at_ms,open_interest,mark_price,taker_imbalance,backfilled
            FROM binance_oi_observations {where}
            WHERE exchange_timestamp_ms <= local_received_at_ms
            ORDER BY local_received_at_ms
        """.replace("WHERE exchange_timestamp", "AND exchange_timestamp") if where else """
            SELECT id,exchange_timestamp_ms,local_received_at_ms,open_interest,mark_price,taker_imbalance,backfilled
            FROM binance_oi_observations
            WHERE exchange_timestamp_ms <= local_received_at_ms
            ORDER BY local_received_at_ms
        """).fetchall()
        output = []
        for row in rows:
            oi = _number(row[3])
            if oi is not None and oi > 0:
                output.append(_Oi(int(row[0]), int(row[1]), int(row[2]), oi, _number(row[4]), _number(row[5]), bool(row[6])))
        return output

    @staticmethod
    def _actual_maker_fills(conn: sqlite3.Connection) -> list[tuple[int, dict[str, Any]]]:
        rows = conn.execute("SELECT id,payload_json FROM order_events WHERE event_type='ORDER_FILLED' ORDER BY id").fetchall()
        output = []
        for event_id, raw in rows:
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("actual_fill") is not True or payload.get("period") != "1d":
                continue
            if payload.get("liquidity_class") != "maker" or not isinstance(payload.get("timestamp_ms"), int):
                continue
            output.append((int(event_id), payload))
        return output

    @staticmethod
    def _markouts_by_fill(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for (raw,) in conn.execute("SELECT payload_json FROM order_events WHERE event_type='FILL_MARKOUT'").fetchall():
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("actual_fill") is not True:
                continue
            if int(payload.get("p3_markout_schema_version") or 0) != P3_MARKOUT_SCHEMA_VERSION:
                continue
            if payload.get("fill_context_status") != "asof_or_before_fill":
                continue
            fill_id, horizon = payload.get("fill_id"), payload.get("horizon_sec")
            try:
                valid_timing = (
                    int(horizon) in P3_MARKOUT_HORIZONS_SEC
                    and int(payload["actual_elapsed_ms"]) >= 0
                    and abs(int(payload["target_lag_ms"])) <= int(payload["horizon_tolerance_ms"])
                )
            except (KeyError, TypeError, ValueError):
                valid_timing = False
            if fill_id and isinstance(horizon, int) and valid_timing:
                output.setdefault(str(fill_id), {})[str(horizon)] = {
                    "signed_markout_ps": payload.get("signed_markout_ps"), "fee_per_share": payload.get("fee_per_share"),
                    "executable_quote": payload.get("executable_quote") is True,
                    "actual_elapsed_ms": payload.get("actual_elapsed_ms"),
                    "target_lag_ms": payload.get("target_lag_ms"),
                }
        return output

    @staticmethod
    def _as_of(points: list[_Oi], timestamp_ms: int) -> _Oi | None:
        # Linear scan is fine for the present research volume and transparent.
        known = [point for point in points if point.local_received_at_ms <= timestamp_ms]
        return known[-1] if known else None

    def _oi_features(self, points: list[_Oi], snapshot_ms: int) -> tuple[_Oi | None, dict[str, Any]]:
        current = self._as_of(points, snapshot_ms)
        if current is None:
            return None, {"oi_available": False}
        result: dict[str, Any] = {
            "oi_available": True, "open_interest": current.open_interest,
            "binance_mark_price": current.mark_price, "taker_imbalance": current.taker_imbalance,
        }
        returns: dict[int, float | None] = {}
        price_returns: dict[int, float | None] = {}
        for horizon in (300, 900, 3600):
            prior = self._as_of(points, snapshot_ms - horizon * 1000)
            oi_return = ((current.open_interest / prior.open_interest) - 1.0) * 10_000 if prior else None
            returns[horizon] = oi_return
            result[f"oi_return_{horizon}s_bps"] = oi_return
            result[f"oi_delta_{horizon}s"] = current.open_interest - prior.open_interest if prior else None
            price_return = ((current.mark_price / prior.mark_price) - 1.0) * 10_000 if prior and current.mark_price and prior.mark_price else None
            price_returns[horizon] = price_return
            result[f"btc_mark_return_{horizon}s_bps"] = price_return
        result["oi_acceleration_5m_vs_15m_bps"] = (returns[300] - returns[900] / 3.0) if returns[300] is not None and returns[900] is not None else None
        result["price_oi_divergence_5m_bps"] = (price_returns[300] - returns[300]) if price_returns[300] is not None and returns[300] is not None else None
        if returns[300] is not None and price_returns[300] is not None:
            result["price_oi_regime_5m"] = f"price_{'up' if price_returns[300] >= 0 else 'down'}|oi_{'up' if returns[300] >= 0 else 'down'}"
        else:
            result["price_oi_regime_5m"] = None
        trailing = [p.open_interest for p in points if snapshot_ms - 3_600_000 <= p.local_received_at_ms <= snapshot_ms]
        if len(trailing) >= 10:
            mean = sum(trailing) / len(trailing)
            variance = sum((value - mean) ** 2 for value in trailing) / (len(trailing) - 1)
            result["oi_zscore_1h"] = (current.open_interest - mean) / math.sqrt(variance) if variance > 0 else None
        else:
            result["oi_zscore_1h"] = None
        return current, result

    @staticmethod
    def _labels(snapshots: list[tuple[int, dict[str, Any]]], index: int) -> dict[str, Any]:
        _, current = snapshots[index]
        timestamp = int(current["snapshot_timestamp_ms"])
        outcome_id = int(current["outcome_id"])
        labels: dict[str, Any] = {}
        for horizon in LABEL_HORIZONS_SEC:
            target = timestamp + horizon * 1000
            future = next((item for item in snapshots[index + 1:] if int(item[1]["outcome_id"]) == outcome_id and int(item[1]["snapshot_timestamp_ms"]) >= target), None)
            key = f"future_{horizon}s"
            if future is None or int(future[1]["snapshot_timestamp_ms"]) > target + LABEL_TOLERANCE_MS:
                labels[key] = {"available": False, "reason": "future_accepted_snapshot_unavailable"}
                continue
            _, payload = future
            record: dict[str, Any] = {"available": True, "label_timestamp_ms": int(payload["snapshot_timestamp_ms"]), "label_lag_ms": int(payload["snapshot_timestamp_ms"]) - target}
            for side in ("yes", "no"):
                entry_bid, entry_ask, _, _ = _bbo(current[f"{side}_l2"])
                future_bid, future_ask, _, _ = _bbo(payload[f"{side}_l2"])
                record[f"{side}_future_bid"] = future_bid
                record[f"{side}_future_ask"] = future_ask
                # executable counterfactuals: long bought at ask exits at bid; short sells at bid covers at ask.
                record[f"{side}_long_markout_ps"] = future_bid - entry_ask if future_bid is not None and entry_ask is not None else None
                record[f"{side}_short_markout_ps"] = entry_bid - future_ask if entry_bid is not None and future_ask is not None else None
            labels[key] = record
        return labels

    def build(self) -> X3BuildResult:
        with sqlite3.connect(self.journal.db_path) as conn:
            snapshots = self._snapshots(conn)
            observations = self._observations(conn)
            maker_fills = self._actual_maker_fills(conn)
            markouts_by_fill = self._markouts_by_fill(conn)
        written = joined = 0
        coverage = {horizon: 0 for horizon in LABEL_HORIZONS_SEC}
        for index, (event_id, snapshot) in enumerate(snapshots):
            timestamp = int(snapshot["snapshot_timestamp_ms"])
            oi, features = self._oi_features(observations, timestamp)
            yes_bid, yes_ask, yes_bid_size, yes_ask_size = _bbo(snapshot["yes_l2"])
            no_bid, no_ask, no_bid_size, no_ask_size = _bbo(snapshot["no_l2"])
            features.update({
                # X4 can only use this decision-time value.  Old snapshots
                # without it remain valid X3 data but are excluded from the
                # time-to-expiry walk-forward comparison.
                "time_left_sec": _number(snapshot.get("time_left_sec")),
                "strike": _number(snapshot.get("strike")),
                "yes_bid": yes_bid, "yes_ask": yes_ask, "yes_spread": yes_ask - yes_bid if yes_bid is not None and yes_ask is not None else None,
                "yes_bid_size": yes_bid_size, "yes_ask_size": yes_ask_size,
                "no_bid": no_bid, "no_ask": no_ask, "no_spread": no_ask - no_bid if no_bid is not None and no_ask is not None else None,
                "no_bid_size": no_bid_size, "no_ask_size": no_ask_size,
            })
            labels = self._labels(snapshots, index)
            for horizon in LABEL_HORIZONS_SEC:
                coverage[horizon] += int(labels[f"future_{horizon}s"]["available"])
            context = {"market_instance": str(snapshot["outcome_id"]), "snapshot_event_id": event_id,
                       "event_time_ms": timestamp, "oi_join_rule": "as_of_local_received_at", "oi_backfill_included": self.include_backfilled}
            if oi is not None:
                joined += 1
                context.update({"oi_observation_id": oi.id, "oi_exchange_timestamp_ms": oi.exchange_timestamp_ms,
                                "oi_local_received_at_ms": oi.local_received_at_ms, "oi_age_ms": timestamp - oi.local_received_at_ms})
            if self.journal.upsert_outcome_oi_feature_row(feature_schema_version=FEATURE_SCHEMA_VERSION,
                    outcome_snapshot_event_id=event_id, outcome_id=int(snapshot["outcome_id"]), period="1d",
                    snapshot_timestamp_ms=timestamp, oi_observation_id=oi.id if oi else None,
                    oi_exchange_timestamp_ms=oi.exchange_timestamp_ms if oi else None,
                    oi_local_received_at_ms=oi.local_received_at_ms if oi else None,
                    oi_age_ms=timestamp - oi.local_received_at_ms if oi else None,
                    oi_join_direction="as_of_local_received_at", oi_backfilled=bool(oi and oi.backfilled),
                    features=features, labels=labels, market_context=context):
                written += 1
        fill_rows = 0
        for event_id, fill in maker_fills:
            timestamp = int(fill["timestamp_ms"])
            oi, features = self._oi_features(observations, timestamp)
            features.update({"fill_id": fill.get("trade_id"), "fill_side": fill.get("side"), "fill_price": fill.get("price"),
                             "fill_quantity": fill.get("quantity"), "actual_fill": True, "maker": True,
                             "oi_join_rule": "as_of_local_received_at"})
            if self.journal.upsert_outcome_oi_fill_feature_row(feature_schema_version=FEATURE_SCHEMA_VERSION,
                    fill_order_event_id=event_id, outcome_id=int(fill["outcome_id"]), period="1d", fill_timestamp_ms=timestamp,
                    oi_observation_id=oi.id if oi else None, oi_local_received_at_ms=oi.local_received_at_ms if oi else None,
                    oi_age_ms=timestamp - oi.local_received_at_ms if oi else None, features=features,
                    actual_markouts=markouts_by_fill.get(str(fill.get("trade_id")), {})):
                fill_rows += 1
        return X3BuildResult(len(snapshots), written, joined, coverage, fill_rows)
