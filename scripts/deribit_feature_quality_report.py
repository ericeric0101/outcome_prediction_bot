"""Print read-only Deribit public-feature collection quality."""
from __future__ import annotations

import argparse
import json

from bot.deribit_feature_report import as_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Deribit public-feature collection quality report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    args = parser.parse_args()
    print(json.dumps(as_json(args.db), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
