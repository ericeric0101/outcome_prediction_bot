"""Read-only execution-quality report for capacity-aware Outcome entries.

This report is intentionally an audit of immutable journal facts.  It does
not replay a book, estimate queue priority, or make a claim about future
profitability.  A row starts only from an ``ORDER_SUBMIT`` which carries the
F5 capacity audit; fills, protective sells and canonical FIFO lots are then
joined only through durable order/trade identifiers and the one-inventory
runtime interval.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def _decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return result if result >= 0 else None


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)
    except ValueError:
        return None


def _bucket(notional: Decimal | None) -> str:
    if notional is None:
        return "unknown"
    # Whole-share alignment intentionally leaves a $20 cap slightly below
    # $20 in most books. Include it in the approved $20 phase rather than
    # classifying every compliant canary entry as "under_20".
    if notional <= Decimal("20"):
        return "up_to_20"
    if notional < Decimal("30"):
        return "20_to_under_30"
    if notional < Decimal("50"):
        return "30_to_under_50"
    return "50_plus"


def _text(value: Decimal | None) -> str | None:
    """Stable human/JSON representation; SQLite REAL must not create 22.0 noise."""
    if value is None:
        return None
    return format(value.normalize(), "f")


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_capacity_submit(event: dict[str, Any]) -> bool:
    audit = _payload(event["payload_json"]).get("audit")
    return (
        event["event_type"] == "ORDER_SUBMIT"
        and event["side"] == "BUY"
        and isinstance(audit, dict)
        and audit.get("entry_capacity_canary_enabled") is True
    )


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    """Summarise F5 submit/fill/exit facts without exchange access."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        order_rows = conn.execute(
            """SELECT id, ts, event_type, venue_order_id, side, price, qty, status,
                      reason, instrument_id, payload_json
               FROM order_events ORDER BY id"""
        ).fetchall()
        lifecycle_rows = conn.execute(
            """SELECT ts, event_type, payload_json FROM strategy_events
               WHERE event_type IN ('OUTCOME_ENTRY_LIFECYCLE', 'OUTCOME_EXIT_LIFECYCLE')
               ORDER BY id"""
        ).fetchall()
        pnl_rows = conn.execute(
            """SELECT open_trade_id, cost_usdc, realized_net_usdc
               FROM outcome_realized_pnl_lots"""
        ).fetchall()

    events = [dict(row) for row in order_rows]
    markouts_by_fill: dict[str, dict[str, Any]] = defaultdict(dict)
    p3_horizon_counts: dict[str, int] = defaultdict(int)
    current_p3_horizon_counts: dict[str, int] = defaultdict(int)
    for event in events:
        if event["event_type"] != "FILL_MARKOUT":
            continue
        payload = _payload(event["payload_json"])
        fill_id = payload.get("fill_id")
        horizon = payload.get("horizon_sec")
        if fill_id is not None and horizon is not None:
            markouts_by_fill[str(fill_id)][str(horizon)] = payload.get("signed_markout_ps")
            p3_horizon_counts[str(horizon)] += 1
            if str(horizon) in {"5", "10", "30"} and payload.get("p3_markout_schema_version") == 2:
                current_p3_horizon_counts[str(horizon)] += 1
    canary_submits = [event for event in events if _is_capacity_submit(event)]
    all_buy_submit_times = [event["ts"] for event in events if event["event_type"] == "ORDER_SUBMIT" and event["side"] == "BUY"]
    lifecycle = [(str(row["ts"]), str(row["event_type"]), _payload(row["payload_json"])) for row in lifecycle_rows]
    pnl_by_trade: dict[str, tuple[Decimal, Decimal]] = defaultdict(lambda: (Decimal("0"), Decimal("0")))
    for row in pnl_rows:
        cost, pnl = _decimal(row["cost_usdc"]), _decimal(row["realized_net_usdc"])
        # Net PnL is allowed to be negative, unlike notional fields.
        try:
            net = Decimal(str(row["realized_net_usdc"]))
        except (ArithmeticError, ValueError):
            continue
        if cost is None:
            continue
        prior_cost, prior_pnl = pnl_by_trade[str(row["open_trade_id"])]
        pnl_by_trade[str(row["open_trade_id"])] = (prior_cost + cost, prior_pnl + net)

    rows: list[dict[str, Any]] = []
    for submit in canary_submits:
        order_id = str(submit["venue_order_id"] or "")
        instrument = str(submit["instrument_id"] or "")
        audit = _payload(submit["payload_json"]).get("audit", {})
        if not order_id or not instrument or not isinstance(audit, dict):
            continue
        requested = _decimal(audit.get("entry_requested_shares"))
        safe = _decimal(audit.get("entry_safe_max_shares"))
        submitted = _decimal(audit.get("entry_submitted_shares"))
        decision_bid = _decimal(audit.get("entry_submit_bid"))
        intended_notional = submitted * decision_bid if submitted is not None and decision_bid is not None else None
        fills = [
            event for event in events
            if event["event_type"] == "ORDER_FILLED" and str(event["venue_order_id"] or "") == order_id
            and event["side"] == "BUY"
        ]
        filled_shares = sum((_decimal(event["qty"]) or Decimal("0") for event in fills), Decimal("0"))
        fill_notional = sum(
            (((_decimal(event["qty"]) or Decimal("0")) * (_decimal(event["price"]) or Decimal("0"))) for event in fills),
            Decimal("0"),
        )
        last_fill_ts = max((str(event["ts"]) for event in fills), default=None)
        fill_trade_ids = {
            str(_payload(event["payload_json"]).get("trade_id"))
            for event in fills if _payload(event["payload_json"]).get("trade_id") is not None
        }
        cancelled = any(
            event["event_type"] == "ORDER_CANCEL" and str(event["venue_order_id"] or "") == order_id
            for event in events
        )
        next_buy_ts = min((ts for ts in all_buy_submit_times if ts > str(submit["ts"])), default=None)

        protective: dict[str, Any] | None = None
        if last_fill_ts is not None:
            sell_candidates = [
                event for event in events
                if event["event_type"] == "ORDER_SUBMIT" and event["side"] == "SELL"
                and str(event["instrument_id"] or "") == instrument and str(event["ts"]) >= last_fill_ts
                and (next_buy_ts is None or str(event["ts"]) < next_buy_ts)
            ]
            if sell_candidates:
                protective = min(sell_candidates, key=lambda event: str(event["ts"]))

        exit_fills: list[dict[str, Any]] = []
        exit_end = next_buy_ts
        if protective is not None:
            exit_fills = [
                event for event in events
                if event["event_type"] == "ORDER_FILLED" and event["side"] == "SELL"
                and str(event["instrument_id"] or "") == instrument and str(event["ts"]) >= str(protective["ts"])
                and (exit_end is None or str(event["ts"]) < exit_end)
            ]
        last_exit_ts = max((str(event["ts"]) for event in exit_fills), default=None)
        replacement_count = sum(
            1 for ts, event_type, payload in lifecycle
            if event_type == "OUTCOME_EXIT_LIFECYCLE" and ts >= (str(protective["ts"]) if protective else "~")
            and (exit_end is None or ts < exit_end) and str(payload.get("coin") or "") == instrument
            and payload.get("state") == "SELL_RESTING" and payload.get("reason") == "target_reprice"
        )
        canonical_cost = sum((pnl_by_trade[trade_id][0] for trade_id in fill_trade_ids), Decimal("0"))
        canonical_pnl = sum((pnl_by_trade[trade_id][1] for trade_id in fill_trade_ids), Decimal("0"))
        reconciled_ts = next((
            ts for ts, event_type, payload in lifecycle
            if event_type == "OUTCOME_ENTRY_LIFECYCLE" and str(payload.get("order_id") or "") == order_id
            and payload.get("state") == "FILL_RECONCILED"
        ), None)
        if filled_shares == 0:
            lifecycle_state = "cancelled_unfilled" if cancelled else "resting_unfilled"
        elif last_exit_ts is not None:
            lifecycle_state = "closed" if canonical_cost > 0 else "exit_filled_pnl_pending"
        else:
            lifecycle_state = "filled_open"
        row = {
            "entry_order_id": order_id, "submitted_at": str(submit["ts"]),
            "instrument_id": instrument, "entry_tier": audit.get("entry_tier"),
            "requested_shares": _text(requested), "safe_max_shares": _text(safe),
            "submitted_shares": _text(submitted),
            "submitted_notional_estimate": _text(intended_notional),
            "size_bucket": _bucket(intended_notional), "spread_bps": audit.get("entry_spread_bps"),
            "entry_bid": audit.get("entry_submit_bid"), "entry_time_left_sec": audit.get("entry_time_left_sec"),
            "entry_regime_state": audit.get("entry_regime_state"),
            "entry_regime_reason": audit.get("entry_regime_reason"),
            "entry_regime_event_id": audit.get("entry_regime_event_id"),
            "top3_depth_shares": audit.get("entry_top3_depth_shares"),
            "recent_trade_shares_5m": audit.get("entry_recent_trade_shares_5m"),
            "filled_shares": _text(filled_shares), "fill_notional": _text(fill_notional),
            "fill_count": len(fills), "last_entry_fill_at": last_fill_ts,
            "submit_to_last_fill_sec": _seconds_between(str(submit["ts"]), last_fill_ts),
            "entry_reconciled_at": reconciled_ts,
            "protective_sell_order_id": str(protective["venue_order_id"]) if protective else None,
            "protective_sell_delay_sec": _seconds_between(last_fill_ts, str(protective["ts"])) if protective else None,
            "target_replacement_count": replacement_count,
            "exit_filled_shares": _text(sum((_decimal(event["qty"]) or Decimal("0") for event in exit_fills), Decimal("0"))),
            "last_exit_fill_at": last_exit_ts,
            "holding_sec": _seconds_between(last_fill_ts, last_exit_ts),
            "canonical_realized_cost": _text(canonical_cost) if canonical_cost > 0 else None,
            "canonical_realized_net_usdc": _text(canonical_pnl) if canonical_cost > 0 else None,
            "p3_fee_adjusted_markout_per_share": {
                trade_id: markouts_by_fill[trade_id]
                for trade_id in sorted(fill_trade_ids) if trade_id in markouts_by_fill
            },
            "lifecycle_state": lifecycle_state,
        }
        rows.append(row)

    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        aggregate = buckets[row["size_bucket"]]
        aggregate["submits"] += 1
        aggregate[row["lifecycle_state"]] += 1
        if Decimal(str(row["filled_shares"])) > 0:
            aggregate["filled_entries"] += 1
        if row["canonical_realized_net_usdc"] is not None:
            aggregate["canonical_realized_net_usdc"] = str(
                Decimal(str(aggregate.get("canonical_realized_net_usdc", "0"))) + Decimal(str(row["canonical_realized_net_usdc"]))
            )
    return {
        "report": "outcome_f5_execution_quality",
        "schema_version": 2,
        "period": period,
        "entries": rows,
        "entry_count": len(rows),
        "p3_fee_adjusted_markout_observations": {
            "total": sum(p3_horizon_counts.values()),
            "by_horizon_sec": dict(sorted(p3_horizon_counts.items(), key=lambda item: int(item[0]))),
            "current_5_10_30sec_schema_v2": dict(sorted(current_p3_horizon_counts.items(), key=lambda item: int(item[0]))),
        },
        "size_buckets": [{"size_bucket": key, **dict(value)} for key, value in sorted(buckets.items())],
        "limits": [
            "Only entries with the durable F5 capacity audit are included.",
            "This is execution and lifecycle evidence, not a claim of scalable alpha or future fill probability.",
            "Canonical realised PnL is reported only when FIFO lots link to immutable official BUY trade ids.",
            "P3 markouts are joined only through immutable official fill trade IDs; an empty mapping means the horizon was not yet observed or was unavailable.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only F5 Outcome execution-quality report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
