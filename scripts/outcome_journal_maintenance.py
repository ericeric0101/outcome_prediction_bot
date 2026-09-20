#!/usr/bin/env python3
"""Operator-invoked, conservative Outcome telemetry retention maintenance.

Default invocation is read-only.  The command never runs from the live tick
and never VACUUMs automatically.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.outcome_telemetry_retention import audit_database, json_report, prune_database, retention_enabled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-path", default="logs/outcome_shadow.db")
    parser.add_argument("--audit", action="store_true", help="read-only table/event inventory (default)")
    parser.add_argument("--report", action="store_true", help="backward-compatible alias for --audit")
    parser.add_argument("--dry-run", action="store_true", help="measure the allowlist; deletes nothing")
    parser.add_argument("--apply", action="store_true", help="delete only explicit allowlisted telemetry")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--vacuum", action="store_true", help="unsupported: VACUUM is always operator-separate")
    args = parser.parse_args()
    if args.vacuum:
        raise SystemExit("refusing automatic VACUUM; run a separately planned offline SQLite VACUUM after backup")
    path = Path(args.journal_path)
    if not path.exists():
        raise SystemExit(f"journal does not exist: {path}")
    # --audit and no arguments are identical read-only operations.  --apply
    # still requires the explicit disabled-by-default environment gate.
    if args.apply and args.dry_run:
        raise SystemExit("choose either --dry-run or --apply")
    result = prune_database(
        path, apply=True, enabled=retention_enabled(os.environ), batch_size=args.batch_size,
    ) if args.apply else (prune_database(path, apply=False, enabled=False, batch_size=args.batch_size)
                          if args.dry_run else audit_database(path))
    print(json_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
