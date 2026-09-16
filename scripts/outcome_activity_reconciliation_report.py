#!/usr/bin/env python3
"""Read-only official Outcome activity reconciliation; never submits orders."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.outcome_activity_reconciliation_report import as_dict
from bot.outcome_sdk_sidecar import OutcomeSdkSidecarClient
from bot.runtime_env import load_runtime_env


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--activity-json", help="offline SDK activity array fixture; avoids any venue call")
    parser.add_argument("--testnet", action="store_true", help="query Outcome testnet public account snapshot")
    args = parser.parse_args()
    load_runtime_env(repo_root=REPO_ROOT)
    if args.activity_json:
        decoded = json.loads(Path(args.activity_json).read_text(encoding="utf-8"))
        if not isinstance(decoded, list):
            parser.error("--activity-json must contain an array")
        activity = decoded
    else:
        wallet = os.environ.get("HL_WALLET_ADDRESS")
        if not wallet:
            parser.error("HL_WALLET_ADDRESS is required unless --activity-json is supplied")
        with OutcomeSdkSidecarClient(REPO_ROOT / "outcome_sdk_sidecar") as sidecar:
            snapshot = sidecar.request("fetch_account_snapshot", testnet=args.testnet, payload={"wallet": wallet})
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("activity"), list):
            raise RuntimeError("official SDK account snapshot returned no activity array")
        activity = snapshot["activity"]
    print(json.dumps(as_dict(args.db, activity), indent=2))


if __name__ == "__main__":
    main()
