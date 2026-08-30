#!/usr/bin/env python3
"""Run the read-only X4 Outcome-only versus Outcome+OI walk-forward report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.outcome_oi_walkforward import as_dict, intraday_as_dict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--intraday", action="store_true", help="provisional 5m/15m rolling diagnostics; never live authorization")
    args = parser.parse_args()
    print(json.dumps(intraday_as_dict(args.db) if args.intraday else as_dict(args.db), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
