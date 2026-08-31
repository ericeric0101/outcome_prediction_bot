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
    requested_value = requested or Decimal("0")
    requested_whole = requested_value.to_integral_value(rounding=ROUND_UP)
    if not enforce_minimum and requested_value != requested_whole:
        raise ValueError("Outcome reduce-only inventory must be an integer number of shares")
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
        price: Decimal, requested_shares: Decimal | None = None, reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Place an ALO order through the SDK with explicit close-flow semantics.

        HIP-4 permits a position-closing residual below the normal $10 opening
        minimum.  The caller may request that SDK exception only for a sell
        marked ``reduce_only``; the reconciled maker state machine is the
        production caller which derives ``requested_shares`` from wallet
        inventory.  A generic sell cannot silently bypass the opening limit.
        """
        if is_buy and reduce_only:
            raise ValueError("reduce_only is valid only for an Outcome sell")
        shares = whole_share_size(price, requested_shares, enforce_minimum=is_buy)
        if shares <= 0:
            raise ValueError("Outcome order must contain at least one whole share")
        residual_exit = not is_buy and price * shares < MIN_NOTIONAL
        if residual_exit and not reduce_only:
            raise RuntimeError(
                "sub-minimum Outcome sell requires reduce_only=True after wallet inventory reconciliation"
            )
        payload: dict[str, Any] = {
            "marketId": str(market.outcome_id),
            "outcome": self.outcome_coin(market, side_index),
            "side": "buy" if is_buy else "sell",
            "price": str(price),
            "amount": str(shares),
            "timeInForce": "ALO",
        }
        if reduce_only:
            # Official SDK documented close-flow escape hatch.  It is
            # intentionally never sent for opening orders or generic sells.
            payload["skipMinNotionalCheck"] = True
        result = self.sidecar.request(
            "place_limit_order",
            payload=payload,
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

    def place_price_protected_ioc_exit(
        self, *, market: OutcomeMarketSpec, side_index: int, limit_price: Decimal,
        requested_shares: Decimal,
    ) -> dict[str, Any]:
        """Submit one reduce-only, price-capped FAK/IOC exit via the official SDK.

        This is intentionally narrow: it can only sell a wallet-reconciled
        integral inventory at a caller-provided limit.  It never accepts a
        buy, a raw market order, a fractional amount, or an opening-order
        minimum-notional path.
        """
        shares = whole_share_size(limit_price, requested_shares, enforce_minimum=False)
        if shares <= 0:
            raise ValueError("Outcome emergency IOC exit requires positive whole inventory")
        result = self.sidecar.request(
            "place_emergency_ioc_exit",
            payload={
                "marketId": str(market.outcome_id),
                "outcome": self.outcome_coin(market, side_index),
                "price": str(limit_price),
                "amount": str(shares),
                "skipMinNotionalCheck": True,
            },
            allow_execution=True,
        )
        if not result.get("orderId"):
            raise RuntimeError(f"Outcome price-protected IOC exit was not accepted: {result}")
        return {**result, "shares": shares, "coin": self.outcome_coin(market, side_index)}

    def cancel_owned_order(self, *, market: OutcomeMarketSpec, side_index: int, order_id: str) -> dict[str, Any]:
        return self.sidecar.request(
            "cancel_order",
            payload={"marketId": str(market.outcome_id), "outcome": self.outcome_coin(market, side_index), "orderId": str(order_id)},
            allow_execution=True,
        )
