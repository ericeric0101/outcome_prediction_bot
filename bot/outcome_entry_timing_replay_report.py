"""Read-only S0 timing and short-lookback replay over immutable evidence.

This report never invents a counterfactual maker fill.  Shorter-window rows
are therefore evaluated only on fills the live 5-minute policy actually got;
the result is an execution-quality screen, not a claim of counterfactual PnL.
"""
from __future__ import annotations

import bisect
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any


WINDOWS_SEC = (30, 60, 120, 300)
P3_HORIZONS_SEC = (5, 10, 30)
STALE_CANCEL_AGES_SEC = (15, 30, 60)


def _number(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp_ms(value: str) -> float:
    return datetime.fromisoformat(value).timestamp() * 1000


def _summary(values: list[float]) -> dict[str, object]:
    if not values:
        return {"n": 0, "median": None, "mean": None, "negative_rate": None}
    return {
        "n": len(values), "median": median(values), "mean": mean(values),
        "negative_rate": sum(value < 0 for value in values) / len(values),
    }


def as_dict(db_path: str | Path) -> dict[str, object]:
    path = Path(db_path).resolve()
    if not path.exists():
        return {"report": "outcome_entry_timing_replay", "status": "journal_missing"}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        submitted: dict[str, tuple[float, int]] = {}
        for ts, order_id, raw in conn.execute(
            "SELECT ts,venue_order_id,payload_json FROM order_events WHERE event_type='ORDER_SUBMIT' AND side='BUY'"
        ):
            try:
                payload = json.loads(raw or "{}")
                audit = payload.get("audit") if isinstance(payload, dict) else None
                if not isinstance(audit, dict) or audit.get("entry_policy_kind") not in {
                    "s0_oi_spot_mark_confirmation", "s0_spot_mark_tier_b",
                }:
                    continue
                decision_ms = int(audit["target_decision_at_ms"])
                submitted[str(order_id)] = (_timestamp_ms(str(ts)), decision_ms)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue

        evidence: dict[str, dict[str, object]] = {}
        for (raw,) in conn.execute(
            "SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED'"
        ):
            try:
                payload = json.loads(raw or "{}")
                if isinstance(payload, dict) and isinstance(payload.get("entry_evidence"), dict):
                    evidence[str(payload.get("order_id"))] = dict(payload["entry_evidence"])
            except (TypeError, json.JSONDecodeError):
                continue

        markouts: dict[str, dict[int, float]] = defaultdict(dict)
        for (raw,) in conn.execute("SELECT payload_json FROM order_events WHERE event_type='FILL_MARKOUT'"):
            try:
                payload = json.loads(raw or "{}")
                horizon = int(payload.get("horizon_sec"))
                value = _number(payload.get("signed_markout_ps"))
                if (
                    payload.get("actual_fill") is True
                    and payload.get("p3_markout_schema_version") == 2
                    and horizon in P3_HORIZONS_SEC and value is not None
                    and payload.get("fill_id")
                ):
                    markouts[str(payload["fill_id"])][horizon] = value
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

        oi_rows: list[tuple[int, float, float]] = []
        for ts, oi, mark in conn.execute(
            """SELECT local_received_at_ms,open_interest,mark_price
               FROM binance_oi_observations
               WHERE symbol='BTCUSDT' AND backfilled=0 AND mark_price IS NOT NULL
               ORDER BY local_received_at_ms"""
        ):
            oi_value, mark_value = _number(oi), _number(mark)
            if oi_value is not None and mark_value is not None and oi_value > 0 and mark_value > 0:
                oi_rows.append((int(ts), oi_value, mark_value))

        fills: list[dict[str, object]] = []
        for ts, order_id, raw in conn.execute(
            "SELECT ts,venue_order_id,payload_json FROM order_events WHERE event_type='ORDER_FILLED' AND side='BUY'"
        ):
            try:
                payload = json.loads(raw or "{}")
                if not (
                    payload.get("actual_fill") is True and payload.get("period") == "1d"
                    and payload.get("liquidity_class") == "maker" and str(order_id) in submitted
                ):
                    continue
                current_evidence = evidence.get(str(order_id))
                spot_bps = _number(current_evidence.get("spot_strike_bps")) if current_evidence else None
                if spot_bps is None:
                    continue
                submit_ms, decision_ms = submitted[str(order_id)]
                fills.append({
                    "order_id": str(order_id), "decision_ms": decision_ms,
                    "submit_ms": submit_ms, "fill_ms": _timestamp_ms(str(ts)),
                    "side_index": 0 if spot_bps >= 0 else 1,
                    "trade_id": str(payload.get("trade_id") or ""),
                })
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

    oi_times = [row[0] for row in oi_rows]

    def as_of(timestamp_ms: int) -> tuple[int, float, float] | None:
        index = bisect.bisect_right(oi_times, timestamp_ms) - 1
        return oi_rows[index] if index >= 0 else None

    delays = {"decision_to_submit_ms": [], "submit_to_fill_ms": [], "decision_to_fill_ms": []}
    delay_markouts: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    replay: dict[int, dict[str, object]] = {}
    replay_values: dict[int, dict[int, list[float]]] = {window: defaultdict(list) for window in WINDOWS_SEC}
    replay_baseline: dict[int, dict[int, list[float]]] = {window: defaultdict(list) for window in WINDOWS_SEC}
    replay_counts: dict[int, list[int]] = {window: [0, 0] for window in WINDOWS_SEC}
    cancel_replay: dict[int, dict[str, object]] = {}

    for fill in fills:
        decision_ms, submit_ms, fill_ms = int(fill["decision_ms"]), float(fill["submit_ms"]), float(fill["fill_ms"])
        delays["decision_to_submit_ms"].append(submit_ms - decision_ms)
        delays["submit_to_fill_ms"].append(fill_ms - submit_ms)
        delays["decision_to_fill_ms"].append(fill_ms - decision_ms)
        delay_sec = (fill_ms - submit_ms) / 1000
        bucket = "le_15s" if delay_sec <= 15 else "15_to_60s" if delay_sec <= 60 else "60_to_300s" if delay_sec <= 300 else "gt_300s"
        fill_markouts = markouts.get(str(fill["trade_id"]), {})
        for horizon, value in fill_markouts.items():
            delay_markouts[bucket][horizon].append(value)

        current = as_of(decision_ms)
        if current is None:
            continue
        for window in WINDOWS_SEC:
            prior = as_of(decision_ms - window * 1000)
            if prior is None:
                continue
            replay_counts[window][0] += 1
            mark_return_bps = (current[2] / prior[2] - 1) * 10_000
            oi_return_bps = (current[1] / prior[1] - 1) * 10_000
            side_index = int(fill["side_index"])
            qualifies = (
                (side_index == 0 and mark_return_bps >= 5 and oi_return_bps >= 1)
                or (side_index == 1 and mark_return_bps <= -5 and oi_return_bps <= -1)
            )
            for horizon, value in fill_markouts.items():
                replay_baseline[window][horizon].append(value)
            if not qualifies:
                continue
            replay_counts[window][1] += 1
            for horizon, value in fill_markouts.items():
                replay_values[window][horizon].append(value)

    for window in WINDOWS_SEC:
        evaluable, qualified = replay_counts[window]
        replay[window] = {
            "replayable_actual_fills": evaluable,
            "would_still_qualify": qualified,
            "qualification_rate": qualified / evaluable if evaluable else None,
            "markouts_if_still_qualified": {str(h): _summary(replay_values[window][h]) for h in P3_HORIZONS_SEC},
            "same_fill_baseline_markouts": {str(h): _summary(replay_baseline[window][h]) for h in P3_HORIZONS_SEC},
        }

    # A cancel-only replay uses no synthetic replacement order.  For a fill
    # that happened after ``age``, it asks only what known adverse markout
    # would not have been taken.  It cannot claim a new fill, opportunity PnL,
    # or a changed future signal after the cancellation.
    for age in STALE_CANCEL_AGES_SEC:
        kept = [item for item in fills if (float(item["fill_ms"]) - float(item["submit_ms"])) / 1000 <= age]
        cancelled = [item for item in fills if item not in kept]
        kept_markouts: dict[int, list[float]] = defaultdict(list)
        cancelled_markouts: dict[int, list[float]] = defaultdict(list)
        for item in kept:
            for horizon, value in markouts.get(str(item["trade_id"]), {}).items():
                kept_markouts[horizon].append(value)
        for item in cancelled:
            for horizon, value in markouts.get(str(item["trade_id"]), {}).items():
                cancelled_markouts[horizon].append(value)
        cancel_replay[age] = {
            "actual_fills": len(fills),
            "fills_kept_before_cancel_age": len(kept),
            "delayed_fills_cancelled_counterfactually": len(cancelled),
            "kept_fill_rate": len(kept) / len(fills) if fills else None,
            "kept_fill_markouts": {str(h): _summary(kept_markouts[h]) for h in P3_HORIZONS_SEC},
            "avoided_delayed_fill_markouts": {str(h): _summary(cancelled_markouts[h]) for h in P3_HORIZONS_SEC},
        }

    return {
        "report": "outcome_entry_timing_replay",
        "schema_version": 1,
        "status": "read_only",
        "actual_maker_buy_fills_with_exact_submission_audit": len(fills),
        "latency_ms": {name: _summary(values) for name, values in delays.items()},
        "fill_delay_bucket_markouts": {
            bucket: {str(horizon): _summary(values[horizon]) for horizon in P3_HORIZONS_SEC}
            for bucket, values in sorted(delay_markouts.items())
        },
        "short_lookback_replay": {str(window): replay[window] for window in WINDOWS_SEC},
        "stale_passive_cancel_only_replay": {str(age): cancel_replay[age] for age in STALE_CANCEL_AGES_SEC},
        "limits": [
            "Short-window rows are conditioned on fills produced by the live 5-minute policy; they do not estimate counterfactual maker fill probability.",
            "Legacy decision timestamps are the immutable pre-submit audit time, not a claim of exchange-side signal formation time.",
            "No result from this report changes live entry, exit, sizing, or risk authority.",
            "Cancel-only rows show the observed markout of fills that would be avoided; they do not estimate replacement-fill probability or counterfactual PnL.",
        ],
    }
