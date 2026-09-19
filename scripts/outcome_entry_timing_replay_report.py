#!/usr/bin/env python3
"""Run the read-only S0 timing / short-lookback replay."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.outcome_entry_timing_replay_report import as_dict

parser = argparse.ArgumentParser(description="Read-only Outcome S0 timing replay")
parser.add_argument("--db", default="logs/outcome_shadow.db")
args = parser.parse_args()
print(json.dumps(as_dict(args.db), indent=2, sort_keys=True))
