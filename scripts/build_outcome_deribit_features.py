"""Build D2 Outcome/Deribit as-of research rows; no live authority."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.outcome_deribit_features import OutcomeDeribitFeaturePipeline
from monitoring.trade_journal_db import TradeJournalDB


def main() -> None:
    parser = argparse.ArgumentParser(description="Build read-only Outcome/Deribit D2 as-of features")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    result = OutcomeDeribitFeaturePipeline(TradeJournalDB(args.db)).build(
        batch_size=args.batch_size, rebuild=args.rebuild,
    )
    print(json.dumps(asdict(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
