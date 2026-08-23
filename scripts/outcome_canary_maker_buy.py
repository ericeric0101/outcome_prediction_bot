#!/usr/bin/env python3
"""Submit the single user-approved Outcome mainnet maker-buy canary.

This is deliberately not a general execution entry point.  Its payload is
fixed to the user-authorised BTC Up #11530, 0.60 x 16.666667 shares (~$10),
with ALO/post-only behaviour and no retry.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.runtime_env import load_runtime_env
from monitoring.trade_journal_db import TradeJournalDB


PAYLOAD = {"marketId": "1153", "outcome": "#11530", "side": "buy", "price": "0.60", "amount": "16.666667", "timeInForce": "ALO"}


def invoke(command: str, *, enable_execution: bool = False) -> dict:
    sidecar = REPO_ROOT / "outcome_sdk_sidecar"
    env = os.environ.copy()
    if enable_execution:
        env["OUTCOME_SIDECAR_CANARY_EXECUTION"] = "1"
    request = {"id": uuid.uuid4().hex, "command": command, "testnet": False}
    if command == "canary_limit_buy":
        request["payload"] = PAYLOAD
    completed = subprocess.run(
        ["node", "dist/main.js"], input=json.dumps(request) + "\n", text=True,
        capture_output=True, cwd=sidecar, env=env, check=False, timeout=45,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "Outcome SDK sidecar failed")
    return json.loads(completed.stdout.strip())


def main() -> None:
    load_runtime_env()
    if os.getenv("HL_TESTNET", "0").lower() in {"1", "true", "yes", "on"}:
        raise SystemExit("Refusing canary: HL_TESTNET is enabled, but this approved order is mainnet-only.")
    preflight = invoke("canary_preflight")
    if not preflight.get("ok"):
        raise SystemExit(f"Preflight failed: {preflight.get('error')}")
    print("Preflight passed: BTC Up #11530, ALO maker buy 0.60 x 16.666667 (~$10). Submitting one order.")
    journal = TradeJournalDB(str(REPO_ROOT / "logs" / "outcome_shadow.db"))
    run_id = f"OUTCOME_CANARY_{uuid.uuid4().hex[:12]}"
    response = invoke("canary_limit_buy", enable_execution=True)
    journal.log_strategy_event(run_id, "OUTCOME_CANARY_ORDER_SUBMITTED", {
        "user_authorized_one_shot": True, "payload": PAYLOAD,
        "preflight": preflight.get("result"), "response": response,
    })
    print(json.dumps(response, ensure_ascii=False))
    if not response.get("ok"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
