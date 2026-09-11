"""Unified B1--B5 research status; read-only and non-authoritative."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from bot.outcome_active_dataset import dataset_report
from bot.outcome_active_model import train_report
from bot.outcome_active_replay import replay_report
from bot.outcome_active_shadow_report import shadow_report


def _b5_counts(db_path: str | Path) -> dict[str, int]:
    path = Path(db_path)
    if not path.exists():
        return {}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            """SELECT event_type,COUNT(*) FROM strategy_events
               WHERE event_type IN ('OUTCOME_ACTIVE_CHALLENGER_SHADOW','OUTCOME_ACTIVE_HOLDING_CHALLENGER_SHADOW')
               GROUP BY event_type"""
        ).fetchall()
    return {str(event_type): int(count) for event_type, count in rows}


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    return {
        "report": "outcome_active_milestone_b",
        "schema_version": 1,
        "period": period,
        "b1_dataset": dataset_report(db_path, period=period),
        "b2_models": train_report(db_path, period=period, artifact_path=None),
        "b3_action_optimizer": {
            "actions": ["WAIT", "JOIN_BEST_BID", "IMPROVE_ONE_TICK", "BOUNDED_MARKETABLE_BUY", "MARKETABLE_PROFIT_EXIT", "BOUNDED_RISK_EXIT", "HOLD_OR_PASSIVE_EXIT"],
            "implementation": "pure_deterministic_shadow_optimizer",
            "live_authority": False,
        },
        "b4_replay": replay_report(db_path, period=period),
        "b5_shadow_event_counts": _b5_counts(db_path),
        "b5_unseen_shadow_report": shadow_report(db_path, period=period),
        "b1_b5_implementation_complete": True,
        "b6_live_production_authorized": False,
        "live_policy_changed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Report Milestone B active challenger status")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
