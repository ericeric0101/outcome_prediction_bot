#!/usr/bin/env python3
"""Read-only inventory check for a future Outcome maker take-profit order."""
from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.adapters.outcome_auth import OutcomeAuth, align_outcome_price
from bot.adapters.outcome_client import OutcomeClient
from bot.outcome_account_sync import OutcomeAccountSynchronizer
from bot.runtime_env import load_runtime_env


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profit-pct", type=Decimal, default=Decimal("10"))
    args = parser.parse_args()
    if args.profit_pct <= 0:
        parser.error("--profit-pct must be positive")
    load_runtime_env()
    wallet = os.getenv("HL_WALLET_ADDRESS") or os.getenv("HYPERLIQUID_WALLET_ADDRESS")
    if not wallet:
        parser.error("HL_WALLET_ADDRESS is required")
    auth = OutcomeAuth(
        wallet_address=wallet,
        is_testnet=os.getenv("HL_TESTNET", "0").lower() in {"1", "true", "yes", "on"},
        base_url=os.getenv("HL_BASE_URL") or None,
    )
    snapshot = OutcomeAccountSynchronizer(OutcomeClient(auth), wallet).fetch_snapshot()
    multiplier = Decimal("1") + args.profit_pct / Decimal("100")
    print("Outcome sell preflight: read-only; no signing and no /exchange request.")
    candidates = 0
    for balance in snapshot.balances:
        raw_target = balance.avg_entry_price * multiplier
        try:
            target = align_outcome_price(raw_target)
            status = "eligible" if balance.available_qty > 0 else "locked_or_no_sellable_qty"
        except ValueError:
            target = "n/a"
            status = "invalid_target_outside_(0,1)"
        if balance.available_qty > 0:
            candidates += 1
        print(
            f"coin={balance.coin} outcome_id={balance.outcome_id} side_index={balance.side_index} "
            f"total={balance.total_qty} held={balance.held_qty} sellable={balance.available_qty} "
            f"avg_entry={balance.avg_entry_price} tp_{args.profit_pct}%={target} status={status}"
        )
    if candidates == 0:
        print("No sellable Outcome inventory found; do not submit a maker sell.")


if __name__ == "__main__":
    main()
