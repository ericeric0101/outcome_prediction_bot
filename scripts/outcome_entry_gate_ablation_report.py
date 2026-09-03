#!/usr/bin/env python3
"""Run the read-only Outcome S0 entry-gate ablation report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.outcome_entry_gate_ablation_report import as_dict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--recent-event-limit", type=int, default=50_000,
                        help="bounded primary-key scan; must be positive")
    args = parser.parse_args()
    if args.recent_event_limit <= 0:
        parser.error("--recent-event-limit must be positive")
    print(json.dumps(
        as_dict(args.db, period=args.period, recent_event_limit=args.recent_event_limit),
        indent=2, sort_keys=True,
    ))


if __name__ == "__main__":
    main()
