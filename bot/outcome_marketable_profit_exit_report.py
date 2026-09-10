"""E5: read-only +1%/+2% depth-aware marketable-profit counterfactual."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

TARGETS = (Decimal("0.01"), Decimal("0.02"))


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        observations = conn.execute(
            """SELECT ts,payload_json FROM strategy_events
               WHERE event_type='OUTCOME_HOLDING_PATH_OBSERVATION' ORDER BY id"""
        ).fetchall()
        pnl_rows = conn.execute(
            """SELECT open_trade_id,SUM(CAST(cost_usdc AS REAL)),SUM(CAST(realized_net_usdc AS REAL))
               FROM outcome_realized_pnl_lots GROUP BY open_trade_id"""
        ).fetchall()
    actual = {str(trade_id): (_decimal(cost), _decimal(pnl)) for trade_id, cost, pnl in pnl_rows}
    lifecycles: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    excluded = 0
    for ts, raw in observations:
        payload = _payload(raw)
        lifecycle = str(payload.get("entry_lifecycle_id") or "")
        if payload.get("period") != period or not lifecycle:
            continue
        if payload.get("marketable_exit_full_inventory") is not True or payload.get("marketable_net_exit_vs_entry_pct") is None:
            excluded += 1
            continue
        lifecycles[lifecycle].append((str(ts), payload))
    rows: list[dict[str, Any]] = []
    aggregates: dict[str, dict[str, Decimal | int]] = {
        "1%": {"eligible_lifecycles": 0, "hypothetical_net_usdc": Decimal("0"), "actual_net_usdc": Decimal("0")},
        "2%": {"eligible_lifecycles": 0, "hypothetical_net_usdc": Decimal("0"), "actual_net_usdc": Decimal("0")},
    }
    for lifecycle, items in lifecycles.items():
        items.sort(key=lambda item: item[0])
        first = items[0][1]
        trade_id = str(first.get("entry_trade_id") or "")
        inventory, vwap = _decimal(first.get("inventory")), _decimal(first.get("fill_vwap"))
        lifecycle_row: dict[str, Any] = {
            "entry_lifecycle_id": lifecycle, "entry_trade_id": trade_id,
            "outcome_id": first.get("outcome_id"), "coin": first.get("coin"), "thresholds": {},
        }
        actual_cost, actual_pnl = actual.get(trade_id, (None, None))
        lifecycle_row["actual_realized_net_usdc"] = str(actual_pnl) if actual_pnl is not None else None
        for target in TARGETS:
            label = f"{int(target * 100)}%"
            hit = next((item for item in items if (_decimal(item[1].get("marketable_net_exit_vs_entry_pct")) or Decimal("-99")) >= target), None)
            hypothetical = None
            if hit is not None and inventory is not None and vwap is not None:
                net_price = _decimal(hit[1].get("marketable_net_exit_price"))
                if net_price is not None:
                    hypothetical = inventory * (net_price - vwap)
                    aggregates[label]["eligible_lifecycles"] = int(aggregates[label]["eligible_lifecycles"]) + 1
                    aggregates[label]["hypothetical_net_usdc"] = Decimal(aggregates[label]["hypothetical_net_usdc"]) + hypothetical
                    if actual_pnl is not None:
                        aggregates[label]["actual_net_usdc"] = Decimal(aggregates[label]["actual_net_usdc"]) + actual_pnl
            lifecycle_row["thresholds"][label] = {
                "first_depth_sufficient_hit_ts": hit[0] if hit else None,
                "holding_age_sec": hit[1].get("holding_age_sec") if hit else None,
                "marketable_exit_vwap": hit[1].get("marketable_exit_vwap") if hit else None,
                "hypothetical_net_usdc": str(hypothetical) if hypothetical is not None else None,
                "actual_minus_hypothetical_net_usdc": str(actual_pnl - hypothetical) if actual_pnl is not None and hypothetical is not None else None,
            }
        rows.append(lifecycle_row)
    return {
        "report": "outcome_marketable_profit_exit_counterfactual", "schema_version": 1,
        "period": period, "lifecycles_with_depth_and_taker_fee": len(rows),
        "legacy_observations_excluded": excluded, "lifecycles": rows,
        "threshold_summary": [
            {"threshold": label, **{key: str(value) if isinstance(value, Decimal) else value for key, value in values.items()}}
            for label, values in aggregates.items()
        ],
        "ready_for_live": False,
        "limits": [
            "A row requires full-position bid-depth VWAP and the entry-time taker fee; legacy top-BBO observations are excluded.",
            "Crossing a threshold proves an executable displayed-depth opportunity, not an IOC fill guarantee or queue outcome.",
            "This report cannot authorize taker exits; Milestone A remains shadow-only.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only marketable-profit exit counterfactual")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
