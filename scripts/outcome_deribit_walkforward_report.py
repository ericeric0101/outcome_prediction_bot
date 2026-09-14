"""Print D3 baseline-versus-Deribit read-only walk-forward report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.outcome_deribit_walkforward import as_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome baseline-vs-Deribit walk-forward")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    args = parser.parse_args()
    print(json.dumps(as_json(args.db), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
