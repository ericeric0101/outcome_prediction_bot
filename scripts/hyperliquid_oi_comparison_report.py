#!/usr/bin/env python3
"""Print a read-only native Hyperliquid BTC OI vs Binance OI report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.hyperliquid_oi_report import as_dict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--max-binance-age-sec", type=float, default=60.0)
    args = parser.parse_args()
    if args.max_binance_age_sec <= 0:
        parser.error("--max-binance-age-sec must be positive")
    print(json.dumps(as_dict(args.db, max_binance_age_ms=round(args.max_binance_age_sec * 1000)), indent=2))


if __name__ == "__main__":
    main()
