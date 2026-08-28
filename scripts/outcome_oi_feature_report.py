#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from bot.outcome_oi_feature_report import as_dict
parser = argparse.ArgumentParser(description="Read-only X3 feature/label coverage report")
parser.add_argument("--db", default="logs/outcome_shadow.db")
args = parser.parse_args()
print(json.dumps(as_dict(args.db), indent=2, sort_keys=True))
