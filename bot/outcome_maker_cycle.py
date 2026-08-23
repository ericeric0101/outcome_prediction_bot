"""One bounded, post-only maker buy → maker sell cycle for an Outcome side.

This is an operational module, not a test helper.  It has no default live
entrypoint: both the process environment and the caller must explicitly enable
execution in the SDK sidecar.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from decimal import Decimal, ROUND_UP
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.adapters.outcome_auth import OutcomeAuth
from bot.adapters.outcome_client import OutcomeClient
from bot.outcome_sdk_sidecar import OutcomeSdkSidecarClient
from bot.runtime_env import load_runtime_env
from monitoring.trade_journal_db import TradeJournalDB


MIN_NOTIONAL = Decimal("10")


def whole_shares_for_min_notional(price: Decimal) -> int:
    if price <= 0:
        raise ValueError("best bid must be positive")
    return int((MIN_NOTIONAL / price).to_integral_value(rounding=ROUND_UP))


def _best_price(levels: list[dict[str, str]], label: str) -> Decimal:
    if not levels:
        raise RuntimeError(f"Outcome order book has no {label}")
    price = Decimal(str(levels[0]["price"]))
    if price <= 0 or price >= 1:
        raise RuntimeError(f"invalid best {label}: {price}")
    return price


class OutcomeMakerCycle:
    def __init__(self, *, market_id: str, outcome: str, interval_sec: float, journal_path: str) -> None:
        load_runtime_env()
        self.market_id = str(market_id)
        self.outcome = outcome
        self.interval_sec = interval_sec
        self.wallet = os.environ.get("HL_WALLET_ADDRESS")
        if not self.wallet:
            raise RuntimeError("HL_WALLET_ADDRESS is required")
        self.testnet = os.environ.get("HL_TESTNET", "0").lower() in {"1", "true", "yes", "on"}
        if self.testnet:
            raise RuntimeError("maker-cycle runner is mainnet-only until testnet market coverage is verified")
        if os.environ.get("OUTCOME_MAKER_CYCLE_ENABLED") != "1":
            raise RuntimeError("set OUTCOME_MAKER_CYCLE_ENABLED=1 after explicit operator approval")
        if os.environ.get("OUTCOME_SDK_EXECUTION_ENABLED") != "1":
            raise RuntimeError("set OUTCOME_SDK_EXECUTION_ENABLED=1 after explicit operator approval")
        self.sidecar = OutcomeSdkSidecarClient(REPO_ROOT / "outcome_sdk_sidecar")
        auth = OutcomeAuth(wallet_address=self.wallet, is_testnet=False, base_url=os.environ.get("HL_BASE_URL") or None)
        self.account = OutcomeClient(auth)
        self.journal = TradeJournalDB(journal_path)
        self.run_id = f"OUTCOME_MAKER_CYCLE_{uuid.uuid4().hex[:12]}"

    def book(self) -> dict[str, Any]:
        return self.sidecar.request(
            "fetch_order_book", payload={"marketId": self.market_id, "outcome": self.outcome}, testnet=False,
        )

    def outcome_balance(self) -> Decimal:
        state = self.account.get_spot_clearinghouse_state_sync(self.wallet)
        coin = "+" + self.outcome.removeprefix("#")
        for balance in state.get("balances", []):
            if balance.get("coin") == coin:
                return Decimal(str(balance.get("total", "0")))
        return Decimal("0")

    def open_order(self, order_id: str) -> dict[str, Any] | None:
        return next((order for order in self.account.get_open_orders_sync(self.wallet) if str(order.get("oid")) == order_id), None)

    def protected_sell(self, inventory: Decimal) -> dict[str, Any] | None:
        for order in self.account.get_open_orders_sync(self.wallet):
            if order.get("coin") != self.outcome or order.get("side") != "A":
                continue
            if Decimal(str(order.get("sz", "0"))) >= inventory:
                return order
        return None

    def run(self, *, timeout_sec: float, buy_order_id: str | None = None, status_every_sec: float = 30.0) -> None:
        baseline = self.outcome_balance()
        if baseline != 0:
            protected = self.protected_sell(baseline)
            if protected is not None:
                self.journal.log_strategy_event(self.run_id, "OUTCOME_MAKER_POSITION_ALREADY_PROTECTED", {"market_id": self.market_id, "outcome": self.outcome, "shares": str(baseline), "sell_order_id": protected.get("oid"), "sell_price": protected.get("limitPx")})
                print(f"maker cycle: existing {baseline} {self.outcome} position is already protected by ALO sell order={protected.get('oid')} @ {protected.get('limitPx')}; no action taken", flush=True)
                return
            raise RuntimeError(f"existing {self.outcome} inventory {baseline} has no covering maker sell; reconcile explicitly before a new cycle")
        if buy_order_id:
            order_id = buy_order_id
            existing = self.open_order(order_id)
            if not existing or existing.get("coin") != self.outcome or existing.get("side") != "B":
                raise RuntimeError("resume order is not the configured wallet's open buy order for this outcome")
            bid = Decimal(str(existing["limitPx"]))
            shares = int(Decimal(str(existing["origSz"])))
            self.journal.log_strategy_event(self.run_id, "OUTCOME_MAKER_BUY_RESUMED", {"market_id": self.market_id, "outcome": self.outcome, "price": str(bid), "shares": shares, "order_id": order_id})
        else:
            book = self.book()
            bid = _best_price(book["bids"], "bid")
            shares = whole_shares_for_min_notional(bid)
            buy = self.sidecar.request(
                "place_limit_order",
                payload={"marketId": self.market_id, "outcome": self.outcome, "side": "buy", "price": str(bid), "amount": str(shares), "timeInForce": "ALO"},
                allow_execution=True,
            )
            order_id = str(buy.get("orderId", ""))
            if buy.get("status") != "resting" or not order_id:
                raise RuntimeError(f"maker buy was not resting: {buy}")
            self.journal.log_strategy_event(self.run_id, "OUTCOME_MAKER_BUY_RESTING", {"market_id": self.market_id, "outcome": self.outcome, "price": str(bid), "shares": shares, "order_id": order_id})
        print(f"maker cycle: monitoring buy order={order_id}, price={bid}, shares={shares}; waiting for fill", flush=True)
        deadline = time.monotonic() + timeout_sec
        next_status = time.monotonic() + max(status_every_sec, self.interval_sec)
        while time.monotonic() < deadline:
            held = self.outcome_balance() - baseline
            open_buy = self.open_order(order_id)
            if held > 0:
                # Avoid accumulating inventory while waiting for an exit: cancel
                # any unfilled remainder before quoting the sale.
                if open_buy is not None:
                    self.sidecar.request(
                        "cancel_order", payload={"marketId": self.market_id, "outcome": self.outcome, "orderId": order_id}, allow_execution=True,
                    )
                whole_held = int(held.to_integral_value(rounding=ROUND_UP))
                if Decimal(whole_held) != held:
                    raise RuntimeError(f"venue returned non-integer Outcome inventory: {held}")
                sell_book = self.book()
                ask = _best_price(sell_book["asks"], "ask")
                sell = self.sidecar.request(
                    "place_limit_order",
                    payload={"marketId": self.market_id, "outcome": self.outcome, "side": "sell", "price": str(ask), "amount": str(whole_held), "timeInForce": "ALO"},
                    allow_execution=True,
                )
                if sell.get("status") != "resting":
                    raise RuntimeError(f"maker sell was not resting: {sell}")
                self.journal.log_strategy_event(self.run_id, "OUTCOME_MAKER_SELL_RESTING", {"market_id": self.market_id, "outcome": self.outcome, "price": str(ask), "shares": whole_held, "order_id": sell.get("orderId"), "buy_order_id": order_id})
                print(f"maker cycle: buy filled {whole_held} @ {bid}; sell resting @ {ask}, order={sell.get('orderId')}", flush=True)
                return
            if open_buy is None:
                raise RuntimeError("maker buy disappeared without a position; refusing to place a sell")
            if time.monotonic() >= next_status:
                seconds_left = max(0, int(deadline - time.monotonic()))
                print(f"maker cycle: buy still resting, order={order_id}, remaining={seconds_left}s", flush=True)
                next_status = time.monotonic() + max(status_every_sec, self.interval_sec)
            time.sleep(self.interval_sec)
        # A bounded cycle must not leave an unmonitored entry order behind.
        # Cancel only if it is still open; a concurrent fill is handled by the
        # next operator invocation after account reconciliation.
        if self.open_order(order_id) is not None:
            self.sidecar.request(
                "cancel_order", payload={"marketId": self.market_id, "outcome": self.outcome, "orderId": order_id}, allow_execution=True,
            )
            self.journal.log_strategy_event(self.run_id, "OUTCOME_MAKER_BUY_TIMEOUT_CANCELLED", {"market_id": self.market_id, "outcome": self.outcome, "order_id": order_id})
            print(f"maker cycle: timeout; cancelled unfilled buy order={order_id}", flush=True)
        else:
            print(f"maker cycle: timeout; buy order={order_id} no longer open, reconcile account before another cycle", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one post-only Outcome maker buy → maker sell cycle")
    parser.add_argument("--market-id", required=True)
    parser.add_argument("--outcome", required=True, help="Outcome side coin, e.g. #11530")
    parser.add_argument("--interval-sec", type=float, default=2.0)
    parser.add_argument("--timeout-sec", type=float, default=900.0)
    parser.add_argument("--buy-order-id", help="Resume monitoring an existing owned maker buy; does not place another buy")
    parser.add_argument("--status-every-sec", type=float, default=30.0, help="Print resting-order status at this interval")
    parser.add_argument("--journal-path", default=str(REPO_ROOT / "logs" / "outcome_shadow.db"))
    args = parser.parse_args()
    try:
        OutcomeMakerCycle(market_id=args.market_id, outcome=args.outcome, interval_sec=args.interval_sec, journal_path=args.journal_path).run(timeout_sec=args.timeout_sec, buy_order_id=args.buy_order_id, status_every_sec=args.status_every_sec)
    except KeyboardInterrupt:
        print("maker cycle: monitoring stopped by operator; no order was cancelled. Reconcile or cancel the resting buy explicitly.", flush=True)
    except RuntimeError as exc:
        print(f"maker cycle: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
