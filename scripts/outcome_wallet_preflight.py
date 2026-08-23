#!/usr/bin/env python3
"""Read-only Hyperliquid spot-balance check for the configured Outcome wallet."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.adapters.outcome_auth import OutcomeAuth
from bot.adapters.outcome_client import OutcomeClient
from bot.runtime_env import load_runtime_env


def main() -> None:
    load_runtime_env()
    wallet = os.getenv("HL_WALLET_ADDRESS") or os.getenv("HYPERLIQUID_WALLET_ADDRESS")
    if not wallet:
        raise SystemExit("HL_WALLET_ADDRESS is required")
    auth = OutcomeAuth(
        wallet_address=wallet,
        is_testnet=os.getenv("HL_TESTNET", "0").lower() in {"1", "true", "yes", "on"},
        base_url=os.getenv("HL_BASE_URL") or None,
    )
    state = OutcomeClient(auth).get_spot_clearinghouse_state_sync(wallet)
    print("Outcome wallet preflight: read-only; no signing and no /exchange request.")
    for balance in state.get("balances", []):
        coin = str(balance.get("coin", ""))
        if coin.upper() == "USDC" or coin.startswith("+"):
            print(
                f"coin={coin} total={balance.get('total', '0')} hold={balance.get('hold', '0')} "
                f"entryNtl={balance.get('entryNtl', '0')}"
            )


if __name__ == "__main__":
    main()
