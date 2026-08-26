#!/usr/bin/env python3
"""Print a read-only Binance OI collection quality report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.binance_oi_report import as_dict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    args = parser.parse_args()
    print(json.dumps(as_dict(args.db), indent=2))


if __name__ == "__main__":
    main()
