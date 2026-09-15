"""Read-only Phase-A/B audit for Outcome adverse-selection research."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    """Summarise shadow action candidates; never query the exchange."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            """SELECT ts,event_type,payload_json FROM strategy_events
               WHERE event_type IN ('OUTCOME_ENTRY_QUALITY_SHADOW','OUTCOME_POST_FILL_QUALITY_SHADOW')
               ORDER BY id"""
        ).fetchall()
        markout_rows = conn.execute(
            """SELECT payload_json FROM order_events WHERE event_type='FILL_MARKOUT' ORDER BY id"""
        ).fetchall()
    observations = [(str(ts), str(kind), _payload(raw)) for ts, kind, raw in rows]
    prefill = [item for item in observations if item[1] == "OUTCOME_ENTRY_QUALITY_SHADOW" and item[2].get("period") == period]
    postfill = [item for item in observations if item[1] == "OUTCOME_POST_FILL_QUALITY_SHADOW" and item[2].get("period") == period]
    stale_by_order: dict[str, dict[str, Any]] = {}
    for ts, _kind, payload in prefill:
        order_id = str(payload.get("order_id") or "")
        action = payload.get("stale_cancel_shadow", {}).get("action") if isinstance(payload.get("stale_cancel_shadow"), dict) else None
        if order_id and action == "CANCEL_STALE_SHADOW":
            stale_by_order.setdefault(order_id, {"first_seen_at": ts, "payload": payload})
    p3: dict[str, dict[str, Any]] = defaultdict(dict)
    for (raw,) in markout_rows:
        payload = _payload(raw)
        fill_id, horizon = payload.get("fill_id"), payload.get("horizon_sec")
        if fill_id is not None and horizon is not None:
            p3[str(fill_id)][str(horizon)] = payload.get("signed_markout_ps")
    postfill_by_trade: dict[str, dict[str, Any]] = {}
    for ts, _kind, payload in postfill:
        trade_id = str(payload.get("fill_trade_id") or "")
        if trade_id:
            postfill_by_trade.setdefault(trade_id, {"first_seen_at": ts, "payload": payload})
    signal_states = Counter(str(payload.get("signal_state") or "unknown") for _ts, _kind, payload in prefill)
    actions = Counter(
        str((payload.get("stale_cancel_shadow") or {}).get("action") or "unknown")
        for _ts, _kind, payload in prefill
    )
    return {
        "report": "outcome_entry_quality_shadow", "schema_version": 1, "period": period,
        "prefill_observation_count": len(prefill), "postfill_observation_count": len(postfill),
        "prefill_signal_states": dict(signal_states), "stale_cancel_shadow_actions": dict(actions),
        "first_stale_cancel_candidate_by_order": [
            {"order_id": order_id, **value} for order_id, value in sorted(stale_by_order.items())
        ],
        "first_postfill_watch_by_trade": [
            {"fill_trade_id": trade_id, **value, "p3_signed_markout_ps": p3.get(trade_id, {})}
            for trade_id, value in sorted(postfill_by_trade.items())
        ],
        "limits": [
            "Shadow candidates are not fills, cancellations, or IOC simulations.",
            "IOC counterfactual uses only as-of top-of-book and excludes full-depth/slippage/fee certainty.",
            "P3 markout is joined only by immutable official fill trade ID when available.",
        ],
        "promotion_blockers": [
            "Need independent daily markets and official fill/P3 outcomes before any live cancellation or active-entry canary.",
            "Need full-depth and fee-aware replay before comparing JOIN to price-protected IOC EV.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome adverse-selection shadow report")
    parser.add_argument("--db", required=True)
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
