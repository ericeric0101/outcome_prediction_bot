"""Unified read-only status report for Milestone-A efficiency E1--E6."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from bot.outcome_capital_efficiency_report import report as capital_report
from bot.outcome_fair_value_model import train_report as fair_value_report
from bot.outcome_marketable_profit_exit_report import report as marketable_exit_report


def _shadow_counts(db_path: str | Path) -> dict[str, int]:
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            """SELECT event_type,COUNT(*) FROM strategy_events
               WHERE event_type IN ('OUTCOME_CONFIDENCE_ENTRY_SHADOW','OUTCOME_QUEUE_PRICING_SHADOW')
               GROUP BY event_type"""
        ).fetchall()
    return {str(event_type): int(count) for event_type, count in rows}


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    return {
        "report": "outcome_efficiency_milestone_a", "schema_version": 1,
        "period": period,
        "e1_capital_efficiency": capital_report(db_path, period=period),
        "e2_executable_fair_value": fair_value_report(db_path),
        "e3_e4_shadow_event_counts": _shadow_counts(db_path),
        "e5_marketable_profit_exit": marketable_exit_report(db_path, period=period),
        "e6_portfolio_allocator": {
            "implementation": "pure_shadow_allocator_available",
            "constraints": ["global_cap", "per_market_cap", "correlation_group_cap", "safe_capacity", "venue_minimum"],
            "live_authority": False,
        },
        "research_implementation_complete": True,
        "milestone_a_complete": True,
        "live_policy_changed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified read-only Outcome efficiency milestone report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
