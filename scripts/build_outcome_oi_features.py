"""Build the offline, leak-free X3 Outcome 1d/OI research table."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from monitoring.trade_journal_db import TradeJournalDB
from bot.outcome_oi_features import OutcomeOiFeaturePipeline

def main() -> None:
    parser = argparse.ArgumentParser(description="Build Outcome 1d × Binance OI research features; no venue calls")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--include-backfilled", action="store_true", help="research-only override; default excludes backfill")
    args = parser.parse_args()
    result = OutcomeOiFeaturePipeline(TradeJournalDB(args.db), include_backfilled=args.include_backfilled).build()
    print(f"X3 feature build: snapshots={result.eligible_snapshots} rows={result.rows_written} oi_joined={result.oi_joined} maker_fill_rows={result.maker_fill_rows} labels={result.labels_available}")

if __name__ == "__main__":
    main()
