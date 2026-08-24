"""Official-SDK-only execution boundary for Outcome strategy runtimes.

The strategy layer speaks in ``OutcomeMarketSpec``/side-index terms.  This
gateway translates that intent to the narrow TypeScript SDK sidecar contract;
it never signs with the legacy Python REST client.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_UP
from pathlib import Path
from typing import Any

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_sdk_sidecar import OutcomeSdkSidecarClient


MIN_NOTIONAL = Decimal("10")


def whole_share_size(price: Decimal, requested: Decimal | None = None, *, enforce_minimum: bool = True) -> int:
    """Return an integer Outcome share count without inventing sell inventory."""
    if not Decimal("0") < price < Decimal("1"):
        raise ValueError("Outcome limit price must be strictly between 0 and 1")
    minimum = (MIN_NOTIONAL / price).to_integral_value(rounding=ROUND_UP)
    requested_whole = (requested or Decimal("0")).to_integral_value(rounding=ROUND_UP)
    return int(max(minimum, requested_whole) if enforce_minimum else requested_whole)


class OutcomeExecutionGateway:
    """Synchronous, official-SDK gateway used by the launcher and state machine."""

    def __init__(self, sidecar: OutcomeSdkSidecarClient | None = None) -> None:
        self.sidecar = sidecar or OutcomeSdkSidecarClient(Path(__file__).resolve().parent.parent / "outcome_sdk_sidecar")

    @staticmethod
    def outcome_coin(market: OutcomeMarketSpec, side_index: int) -> str:
        if side_index == 0:
            return market.yes_coin
        if side_index == 1:
            return market.no_coin
        raise ValueError("Outcome side_index must be 0 or 1")

    def place_alo(
        self, *, market: OutcomeMarketSpec, side_index: int, is_buy: bool,
        price: Decimal, requested_shares: Decimal | None = None,
    ) -> dict[str, Any]:
        shares = whole_share_size(price, requested_shares, enforce_minimum=is_buy)
        if not is_buy and (shares <= 0 or price * shares < MIN_NOTIONAL):
            raise RuntimeError("cannot post a partial Outcome exit below the venue's 10 USDC minimum; hold and reconcile")
        result = self.sidecar.request(
            "place_limit_order",
            payload={
                "marketId": str(market.outcome_id),
                "outcome": self.outcome_coin(market, side_index),
                "side": "buy" if is_buy else "sell",
                "price": str(price),
                "amount": str(shares),
                "timeInForce": "ALO",
            },
            allow_execution=True,
        )
        if result.get("status") != "resting" or not result.get("orderId"):
            raise RuntimeError(f"Outcome ALO order was not resting: {result}")
        return {**result, "shares": shares, "coin": self.outcome_coin(market, side_index)}

    def fetch_order_book(self, *, market: OutcomeMarketSpec, side_index: int) -> dict[str, Any]:
        return self.sidecar.request(
            "fetch_order_book",
            payload={"marketId": str(market.outcome_id), "outcome": self.outcome_coin(market, side_index)},
        )

    def cancel_owned_order(self, *, market: OutcomeMarketSpec, side_index: int, order_id: str) -> dict[str, Any]:
        return self.sidecar.request(
            "cancel_order",
            payload={"marketId": str(market.outcome_id), "outcome": self.outcome_coin(market, side_index), "orderId": str(order_id)},
            allow_execution=True,
        )
