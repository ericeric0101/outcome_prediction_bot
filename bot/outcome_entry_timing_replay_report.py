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
STALE_CANCEL_COUNTERFACTUAL_HORIZONS_SEC = (5 * 60, 15 * 60, 30 * 60)
STALE_CANCEL_SNAPSHOT_MAX_LAG_SEC = 20


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


def _full_depth_vwap(levels: object, quantity: float) -> float | None:
    """Return a hypothetical *sell* VWAP, only when all shares are visible.

    This deliberately does not use a top-of-book mark.  A stale BUY that was
    cancelled never created inventory, so any later result is counterfactual;
    claiming an executable exit without enough bid depth would overstate what
    the historical book can support.
    """
    if quantity <= 0 or not isinstance(levels, list):
        return None
    remaining, notional = quantity, 0.0
    for level in levels:
        if not isinstance(level, dict):
            continue
        price = _number(level.get("px", level.get("price")))
        size = _number(level.get("sz", level.get("size")))
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        taken = min(remaining, size)
        notional += taken * price
        remaining -= taken
        if remaining <= 1e-9:
            return notional / quantity
    return None


def _counterfactual_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    values = [float(row["gross_pnl_usdc"]) for row in rows if row.get("gross_pnl_usdc") is not None]
    returns = [float(row["gross_return_pct"]) for row in rows if row.get("gross_return_pct") is not None]
    return {
        "full_depth_evaluable": len(values),
        "positive_count": sum(value > 0 for value in values),
        "gross_pnl_usdc": _summary(values),
        "gross_return_pct": _summary(returns),
    }


def as_dict(db_path: str | Path) -> dict[str, object]:
    path = Path(db_path).resolve()
    if not path.exists():
        return {"report": "outcome_entry_timing_replay", "status": "journal_missing"}
    stale_cancels: list[dict[str, object]] = []
    trade_tape: list[dict[str, object]] = []
    p2_snapshots: list[dict[str, object]] = []
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

        # The live 60-second policy emits one durable confirmation only after
        # cancel-and-confirm succeeds.  These are not fills.  We retain the
        # immutable submitted price/quantity solely to create a clearly
        # labelled, read-only counterfactual in this report.
        submitted_audits: dict[str, dict[str, object]] = {}
        for order_id, raw in conn.execute(
            "SELECT venue_order_id,payload_json FROM order_events WHERE event_type='ORDER_SUBMIT' AND side='BUY'"
        ):
            try:
                payload = json.loads(raw or "{}")
                audit = payload.get("audit") if isinstance(payload, dict) else None
                if isinstance(audit, dict):
                    submitted_audits[str(order_id)] = dict(audit)
            except (TypeError, json.JSONDecodeError):
                continue
        for event_id, ts, raw in conn.execute(
            "SELECT id,ts,payload_json FROM strategy_events WHERE event_type='OUTCOME_STALE_ENTRY_CANCEL_CONFIRMED'"
        ):
            try:
                payload = json.loads(raw or "{}")
                order_id = str(payload.get("order_id") or "")
                audit = submitted_audits.get(order_id)
                if not audit:
                    continue
                limit_price = _number(audit.get("entry_submit_bid"))
                shares = _number(audit.get("entry_submitted_shares"))
                coin = payload.get("coin")
                outcome_id = payload.get("outcome_id")
                if limit_price is None or shares is None or shares <= 0 or not isinstance(coin, str):
                    continue
                stale_cancels.append({
                    "order_id": order_id, "outcome_id": outcome_id, "coin": coin,
                    "source_event_id": int(event_id),
                    "cancelled_at_ms": _timestamp_ms(str(ts)), "cancelled_at": str(ts),
                    "resting_limit_price": limit_price, "submitted_shares": shares,
                    "order_age_sec": _number(payload.get("order_age_sec")),
                })
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

        if stale_cancels:
            earliest_cancel_ms = min(float(row["cancelled_at_ms"]) for row in stale_cancels)
            earliest_cancel_event_id = min(int(row["source_event_id"]) for row in stale_cancels)
            # ``OUTCOME_WS_TRADES`` is the public venue tape.  An ask-side
            # trade at/below a resting BUY's limit is a price-through signal,
            # not a reconstructed maker fill: queue position and intervening
            # cancellations are intentionally unavailable from this data.
            for (raw,) in conn.execute(
                "SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_WS_TRADES' AND id>=?",
                (earliest_cancel_event_id,),
            ):
                try:
                    payload = json.loads(raw or "{}")
                    data = ((payload.get("raw") or {}).get("data") if isinstance(payload, dict) else None)
                    if not isinstance(data, list):
                        continue
                    for trade in data:
                        trade_time = _number(trade.get("time")) if isinstance(trade, dict) else None
                        if isinstance(trade, dict) and trade_time is not None and trade_time >= earliest_cancel_ms:
                            trade_tape.append(dict(trade))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            for (raw,) in conn.execute(
                "SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_P2_PARITY_SNAPSHOT' AND id>=?",
                (earliest_cancel_event_id,),
            ):
                try:
                    payload = json.loads(raw or "{}")
                    timestamp = _number(payload.get("snapshot_timestamp_ms")) if isinstance(payload, dict) else None
                    if isinstance(payload, dict) and timestamp is not None and timestamp >= earliest_cancel_ms:
                        p2_snapshots.append(payload)
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
    stale_cancel_counterfactual_rows: list[dict[str, object]] = []

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

    # The historical stale-cancel analysis is intentionally conditional.  We
    # first require an observed public tape price-through; even then it says
    # only that a maker fill *could* have become possible.  We cannot recover
    # queue priority, displayed queue ahead, or the book that our still-live
    # order itself would have changed.  Therefore no row is labelled as an
    # actual fill or canonical PnL.
    # SQLite's event-type index is ordered newest-first.  The counterfactual
    # needs the *first chronological* price-through, not whichever matching
    # row that query plan happened to return first.
    trade_tape.sort(key=lambda row: float(_number(row.get("time")) or 0))
    p2_snapshots.sort(key=lambda row: float(row.get("snapshot_timestamp_ms", 0)))
    p2_times = [float(row.get("snapshot_timestamp_ms", 0)) for row in p2_snapshots]
    for cancel in sorted(stale_cancels, key=lambda row: float(row["cancelled_at_ms"])):
        cancelled_at_ms = float(cancel["cancelled_at_ms"])
        limit_price = float(cancel["resting_limit_price"])
        matches = [
            trade for trade in trade_tape
            if str(trade.get("coin")) == str(cancel["coin"])
            and str(trade.get("side")) == "A"
            and (_number(trade.get("px")) is not None and float(_number(trade.get("px")) or 0) <= limit_price)
            and (_number(trade.get("time")) is not None and float(_number(trade.get("time")) or 0) >= cancelled_at_ms)
        ]
        row: dict[str, object] = {
            **cancel,
            "queue_fill_status": "not_reconstructible_from_public_tape_and_l2",
            "tape_price_through_observed": bool(matches),
            "price_through_interpretation": (
                "ask_side_trade_at_or_below_resting_buy_limit; not_proof_of_our_fill"
                if matches else "no_observed_ask_side_trade_at_or_below_resting_buy_limit_in_retained_tape"
            ),
            "forward_full_depth": {},
        }
        if matches:
            trade = matches[0]
            trade_at_ms = float(_number(trade.get("time")) or 0)
            row.update({
                "first_price_through_at_ms": int(trade_at_ms),
                "first_price_through_after_cancel_sec": round((trade_at_ms - cancelled_at_ms) / 1000, 3),
                "first_price_through_price": _number(trade.get("px")),
                "first_price_through_size": _number(trade.get("sz")),
                "first_price_through_trade_id": str(trade.get("tid") or "") or None,
            })
            for horizon_sec in STALE_CANCEL_COUNTERFACTUAL_HORIZONS_SEC:
                target_ms = trade_at_ms + horizon_sec * 1000
                index = bisect.bisect_left(p2_times, target_ms)
                snapshot = p2_snapshots[index] if index < len(p2_snapshots) else None
                result: dict[str, object] = {"status": "snapshot_missing"}
                if snapshot is not None:
                    snapshot_ms = float(snapshot.get("snapshot_timestamp_ms", 0))
                    lag_sec = (snapshot_ms - target_ms) / 1000
                    if lag_sec <= STALE_CANCEL_SNAPSHOT_MAX_LAG_SEC:
                        coin = str(cancel["coin"])
                        book = snapshot.get("yes_l2") if snapshot.get("yes_coin") == coin else (
                            snapshot.get("no_l2") if snapshot.get("no_coin") == coin else None
                        )
                        levels = book.get("levels", [None])[0] if isinstance(book, dict) else None
                        exit_vwap = _full_depth_vwap(levels, float(cancel["submitted_shares"]))
                        result = {
                            "status": "full_depth_available" if exit_vwap is not None else "full_depth_insufficient",
                            "snapshot_at_ms": int(snapshot_ms), "snapshot_lag_sec": round(lag_sec, 3),
                            "marketable_exit_vwap": exit_vwap,
                            "gross_return_pct": (
                                (exit_vwap / limit_price - 1) * 100 if exit_vwap is not None else None
                            ),
                            "gross_pnl_usdc": (
                                float(cancel["submitted_shares"]) * (exit_vwap - limit_price)
                                if exit_vwap is not None else None
                            ),
                        }
                    else:
                        result = {"status": "snapshot_outside_max_lag", "snapshot_lag_sec": round(lag_sec, 3)}
                row["forward_full_depth"][str(horizon_sec // 60)] = result  # type: ignore[index]
        stale_cancel_counterfactual_rows.append(row)

    stale_cancel_counterfactual_summary: dict[str, object] = {
        "stale_cancelled_orders": len(stale_cancel_counterfactual_rows),
        "price_through_candidates": sum(bool(row["tape_price_through_observed"]) for row in stale_cancel_counterfactual_rows),
        "no_price_through_in_retained_tape": sum(not bool(row["tape_price_through_observed"]) for row in stale_cancel_counterfactual_rows),
        "forward_full_depth_by_minutes": {},
    }
    for horizon_sec in STALE_CANCEL_COUNTERFACTUAL_HORIZONS_SEC:
        horizon_rows = [
            dict(row.get("forward_full_depth", {})).get(str(horizon_sec // 60), {})
            for row in stale_cancel_counterfactual_rows if bool(row["tape_price_through_observed"])
        ]
        stale_cancel_counterfactual_summary["forward_full_depth_by_minutes"][str(horizon_sec // 60)] = _counterfactual_summary(  # type: ignore[index]
            [row for row in horizon_rows if isinstance(row, dict)]
        )

    return {
        "report": "outcome_entry_timing_replay",
        "schema_version": 2,
        "status": "read_only",
        "actual_maker_buy_fills_with_exact_submission_audit": len(fills),
        "latency_ms": {name: _summary(values) for name, values in delays.items()},
        "fill_delay_bucket_markouts": {
            bucket: {str(horizon): _summary(values[horizon]) for horizon in P3_HORIZONS_SEC}
            for bucket, values in sorted(delay_markouts.items())
        },
        "short_lookback_replay": {str(window): replay[window] for window in WINDOWS_SEC},
        "stale_passive_cancel_only_replay": {str(age): cancel_replay[age] for age in STALE_CANCEL_AGES_SEC},
        "stale_cancel_price_through_counterfactual": {
            "status": "read_only_conditional_not_a_fill_reconstruction",
            "summary": stale_cancel_counterfactual_summary,
            "orders": stale_cancel_counterfactual_rows,
        },
        "limits": [
            "Short-window rows are conditioned on fills produced by the live 5-minute policy; they do not estimate counterfactual maker fill probability.",
            "Legacy decision timestamps are the immutable pre-submit audit time, not a claim of exchange-side signal formation time.",
            "No result from this report changes live entry, exit, sizing, or risk authority.",
            "Cancel-only rows show the observed markout of fills that would be avoided; they do not estimate replacement-fill probability or counterfactual PnL.",
            "Stale-cancel price-through rows require a later ask-side public trade at or below the cancelled BUY limit, but queue priority and intervening book changes are unavailable; they are candidate fills, never asserted fills.",
            "Forward results use the first retained P2 full-depth snapshot after each fixed horizon and are gross of hypothetical fees; they are not a simulated stop, take-profit, or final realized PnL.",
        ],
    }
