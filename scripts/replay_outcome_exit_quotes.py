#!/usr/bin/env python3
"""Write read-only E3 Outcome exit quote counterfactuals to the journal."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.outcome_exit_requote_replay import replay_exit_quotes


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome ALO exit cancel/replace replay")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--target-return-pct", default="0.05")
    parser.add_argument("--loss-reprice-pct", default="0.05")
    parser.add_argument("--maker-close-fee-rate", default="0.0004")
    args = parser.parse_args()
    report = replay_exit_quotes(db_path=args.db, period=args.period, target_return_pct=Decimal(args.target_return_pct),
        loss_reprice_pct=Decimal(args.loss_reprice_pct), maker_close_fee_rate=Decimal(args.maker_close_fee_rate))
    print(json.dumps(asdict(report), indent=2))


if __name__ == "__main__":
    main()
