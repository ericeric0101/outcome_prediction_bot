#!/usr/bin/env python3
"""Inspect or repair duplicate exchange trade-id rows in an Outcome journal.

Stop all Outcome live/shadow writers before using --apply.  The repair keeps
the oldest ORDER_FILLED row per official trade id and never touches venue data.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitoring.trade_journal_db import TradeJournalDB

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--db", default="logs/outcome_shadow.db")
parser.add_argument("--apply", action="store_true", help="delete only proven duplicate local ORDER_FILLED rows")
args = parser.parse_args()
journal = TradeJournalDB(args.db)
result = journal.repair_duplicate_outcome_fills(run_id="outcome-fill-dedupe-maintenance", dry_run=not args.apply)
print(f"Outcome fill dedupe: duplicates={result['duplicate_count']} removed={result['removed_count']} dry_run={not args.apply}")
