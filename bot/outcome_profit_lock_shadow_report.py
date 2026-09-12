"""B5 read-only evidence report for full-depth +1% marketable profit locks.

The report compares a first observed executable opportunity with the actual
recorded lifecycle.  It does *not* invent a fill, a re-entry, or a counterfactual
path after that hypothetical exit.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


HORIZONS_SEC = (300, 900, 1800, 3600)


def _payload(value: object) -> dict[str, Any]:
    try:
        item = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return item if isinstance(item, dict) else {}


def _decimal(value: object) -> Decimal | None:
    try:
        item = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return item if item.is_finite() else None


def _epoch(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _nearest(items: list[tuple[float, dict[str, Any]]], target: float, *, tolerance_sec: int) -> dict[str, Any] | None:
    times = [item[0] for item in items]
    pos = bisect.bisect_left(times, target)
    choices = items[max(0, pos - 1): min(len(items), pos + 2)]
    if not choices:
        return None
    timestamp, payload = min(choices, key=lambda item: abs(item[0] - target))
    return payload if abs(timestamp - target) <= tolerance_sec else None


def report(db_path: str | Path, *, period: str = "1d", tolerance_sec: int = 90) -> dict[str, Any]:
    path = Path(db_path)
    empty = {
        "report": "outcome_b5_profit_lock_shadow", "schema_version": 1, "period": period,
        "eligible_full_depth_b5_holding_events": 0, "profit_lock_lifecycles": 0,
        "ready_for_live": False, "live_authority": False,
        "blockers": ["b5_profit_lock_unseen_evidence_required", "b6_requires_separate_operator_authorization"],
    }
    if not path.exists():
        return empty
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "strategy_events" not in tables:
            return empty
        event_rows = conn.execute(
            """SELECT ts,payload_json FROM strategy_events
               WHERE event_type='OUTCOME_ACTIVE_HOLDING_CHALLENGER_SHADOW' ORDER BY id"""
        ).fetchall()
        path_rows = conn.execute(
            """SELECT ts,payload_json FROM strategy_events
               WHERE event_type='OUTCOME_HOLDING_PATH_OBSERVATION' ORDER BY id"""
        ).fetchall()
        pnl_rows = conn.execute(
            """SELECT open_trade_id,SUM(CAST(realized_net_usdc AS REAL))
               FROM outcome_realized_pnl_lots GROUP BY open_trade_id"""
        ).fetchall() if "outcome_realized_pnl_lots" in tables else []

    paths: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    for raw_ts, raw in path_rows:
        ts, payload = _epoch(raw_ts), _payload(raw)
        lifecycle = str(payload.get("entry_lifecycle_id") or "")
        if ts is not None and lifecycle and payload.get("period") == period:
            paths[lifecycle].append((ts, payload))
    for values in paths.values():
        values.sort(key=lambda item: item[0])
    actual_pnl = {str(trade): _decimal(pnl) for trade, pnl in pnl_rows}

    candidates: dict[str, tuple[float, dict[str, Any]]] = {}
    eligible_events = 0
    for raw_ts, raw in event_rows:
        timestamp, payload = _epoch(raw_ts), _payload(raw)
        lifecycle = str(payload.get("entry_lifecycle_id") or "")
        depth = payload.get("full_depth_execution")
        challenger = payload.get("challenger")
        if timestamp is None or not lifecycle or payload.get("period") != period or not isinstance(depth, dict) or not isinstance(challenger, dict):
            continue
        if depth.get("marketable_exit_full_inventory") is not True:
            continue
        eligible_events += 1
        if str(challenger.get("action")) != "MARKETABLE_PROFIT_EXIT":
            continue
        # First actionable B5 observation is the only counterfactual candidate
        # for the lifecycle; later events are post-opportunity diagnostics.
        candidates.setdefault(lifecycle, (timestamp, payload))

    lifecycles: list[dict[str, Any]] = []
    for lifecycle, (hit_ts, payload) in sorted(candidates.items(), key=lambda item: item[1][0]):
        depth = payload["full_depth_execution"]
        hit_return = _decimal(depth.get("marketable_net_exit_vs_entry_pct"))
        inventory, fill_vwap, net_price = (
            _decimal(depth.get("inventory")), _decimal(depth.get("fill_vwap")), _decimal(depth.get("marketable_net_exit_price")),
        )
        hypothetical_pnl = inventory * (net_price - fill_vwap) if inventory is not None and fill_vwap is not None and net_price is not None else None
        future = paths.get(lifecycle, [])
        future_returns = [
            _decimal(item[1].get("marketable_net_exit_vs_entry_pct"))
            for item in future if item[0] >= hit_ts and item[1].get("marketable_exit_full_inventory") is True
        ]
        valid_returns = [value for value in future_returns if value is not None]
        by_horizon: dict[str, Any] = {}
        for horizon in HORIZONS_SEC:
            future_payload = _nearest(future, hit_ts + horizon, tolerance_sec=tolerance_sec)
            by_horizon[str(horizon)] = (
                future_payload.get("marketable_net_exit_vs_entry_pct")
                if future_payload is not None and future_payload.get("marketable_exit_full_inventory") is True else None
            )
        trade_id = str((future[0][1] if future else {}).get("entry_trade_id") or "")
        realized = actual_pnl.get(trade_id)
        lifecycles.append({
            "entry_lifecycle_id": lifecycle, "entry_trade_id": trade_id or None,
            "outcome_id": payload.get("outcome_id"), "coin": payload.get("coin"),
            "first_profit_lock_ts": datetime.fromtimestamp(hit_ts).astimezone().isoformat(),
            "holding_age_sec": payload.get("holding_age_sec"),
            "observed_net_return_pct": str(hit_return) if hit_return is not None else None,
            "displayed_depth_shares": depth.get("marketable_exit_depth_shares"),
            "hypothetical_full_depth_net_pnl_usdc": str(hypothetical_pnl) if hypothetical_pnl is not None else None,
            "actual_realized_net_pnl_usdc": str(realized) if realized is not None else None,
            "actual_minus_hypothetical_net_pnl_usdc": str(realized - hypothetical_pnl) if realized is not None and hypothetical_pnl is not None else None,
            "later_full_depth_net_return_by_horizon_sec": by_horizon,
            "later_min_full_depth_net_return_pct": str(min(valid_returns)) if valid_returns else None,
            "later_max_full_depth_net_return_pct": str(max(valid_returns)) if valid_returns else None,
            "later_fell_below_cost": any(value < 0 for value in valid_returns),
            "limits": "Actual lifecycle is observational; it is not the counterfactual result after a hypothetical taker exit.",
        })
    blockers = ["b5_profit_lock_unseen_evidence_required"]
    if len(lifecycles) < 20:
        blockers.append("fewer_than_20_full_depth_profit_lock_lifecycles")
    if len({row["outcome_id"] for row in lifecycles if row["outcome_id"] is not None}) < 5:
        blockers.append("fewer_than_5_unseen_daily_markets")
    blockers.append("b6_requires_separate_operator_authorization")
    return {
        "report": "outcome_b5_profit_lock_shadow", "schema_version": 1, "period": period,
        "eligible_full_depth_b5_holding_events": eligible_events,
        "profit_lock_lifecycles": len(lifecycles), "lifecycles": lifecycles,
        "ready_for_live": False, "live_authority": False, "blockers": blockers,
        "limits": [
            "A B5 marketable-profit action proves displayed full-depth and fee-adjusted opportunity only, not IOC fill certainty.",
            "No B6 authority is granted; compare multiple unseen markets before an explicitly authorized canary.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only B5 full-depth marketable profit-lock evidence")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--tolerance-sec", type=int, default=90)
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period, tolerance_sec=args.tolerance_sec), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
