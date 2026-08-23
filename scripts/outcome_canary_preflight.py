#!/usr/bin/env python3
"""Read-only P4 readiness report; it contains no exchange client or signing code."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.outcome_canary_gate import OutcomeCanaryGate, OutcomeCanaryReadiness
from monitoring.trade_journal_db import TradeJournalDB


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--record", action="store_true", help="Record a read-only OUTCOME_CANARY_BLOCKED audit event")
    args = parser.parse_args()
    readiness = OutcomeCanaryReadiness.from_journal(args.db)
    print(json.dumps({
        "ready_for_live": readiness.ready_for_live,
        "official_resolutions": readiness.official_resolutions,
        "ws_resyncs": readiness.ws_resyncs,
        "parity_snapshots": readiness.parity_snapshots,
        "actual_fills": readiness.actual_fills,
        "reasons": readiness.reasons,
    }, indent=2))
    if args.record:
        OutcomeCanaryGate(TradeJournalDB(args.db), "outcome-canary-preflight").block(readiness)


if __name__ == "__main__":
    main()
