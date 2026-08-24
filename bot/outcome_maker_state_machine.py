"""Non-blocking, reconciled maker lifecycle for one Outcome side.

``tick`` performs at most one exchange mutation.  It is safe for a strategy
loop to call repeatedly: account state is the source of truth, not process
memory.  The only order styles emitted are ALO limit orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_execution_gateway import OutcomeExecutionGateway


class AccountReader(Protocol):
    def get_spot_clearinghouse_state_sync(self, user: str) -> dict[str, Any]: ...
    def get_open_orders_sync(self, user: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class MakerTickResult:
    state: Literal["flat", "buy_resting", "sell_resting", "buy_placed", "sell_placed", "blocked"]
    detail: str
    order_id: str | None = None


class OutcomeMakerStateMachine:
    """Venue state machine; strategy decides only whether entry is permitted."""

    def __init__(self, *, account: AccountReader, gateway: OutcomeExecutionGateway, wallet: str) -> None:
        self.account = account
        self.gateway = gateway
        self.wallet = wallet

    @staticmethod
    def _coin_balance(state: dict[str, Any], coin: str) -> Decimal:
        for balance in state.get("balances", []):
            if balance.get("coin") == coin:
                return Decimal(str(balance.get("total", "0")))
        return Decimal("0")

    def _orders(self, coin: str) -> list[dict[str, Any]]:
        return [order for order in self.account.get_open_orders_sync(self.wallet) if order.get("coin") == coin]

    @staticmethod
    def _best(levels: list[dict[str, Any]], label: str) -> Decimal:
        if not levels:
            raise RuntimeError(f"Outcome book has no {label}")
        price = Decimal(str(levels[0]["price"]))
        if not Decimal("0") < price < Decimal("1"):
            raise RuntimeError(f"invalid best {label}: {price}")
        return price

    def tick(self, *, market: OutcomeMarketSpec, side_index: int, entry_permitted: bool) -> MakerTickResult:
        coin = self.gateway.outcome_coin(market, side_index)
        inventory = self._coin_balance(self.account.get_spot_clearinghouse_state_sync(self.wallet), coin)
        orders = self._orders(coin)
        buys = [order for order in orders if order.get("side") == "B"]
        sells = [order for order in orders if order.get("side") == "A"]

        if inventory > 0:
            covering = next((order for order in sells if Decimal(str(order.get("sz", "0"))) >= inventory), None)
            if covering:
                return MakerTickResult("sell_resting", "inventory is protected by owned ALO sell", str(covering.get("oid")))
            if buys:
                # Never add exposure after any fill.  The next tick will see
                # the cancelled remainder and then post the protective sale.
                order = buys[0]
                self.gateway.cancel_owned_order(market=market, side_index=side_index, order_id=str(order["oid"]))
                return MakerTickResult("blocked", "cancelled unfilled buy remainder before protective sell", str(order["oid"]))
            book = self.gateway.fetch_order_book(market=market, side_index=side_index)
            ask = self._best(book["asks"], "ask")
            result = self.gateway.place_alo(market=market, side_index=side_index, is_buy=False, price=ask, requested_shares=inventory)
            return MakerTickResult("sell_placed", "placed ALO sell for reconciled inventory", str(result["orderId"]))

        if sells:
            return MakerTickResult("blocked", "wallet has sell order without inventory; reconcile explicitly", str(sells[0].get("oid")))
        if buys:
            return MakerTickResult("buy_resting", "owned ALO buy remains resting", str(buys[0].get("oid")))
        if not entry_permitted:
            return MakerTickResult("flat", "strategy did not permit a new entry")

        book = self.gateway.fetch_order_book(market=market, side_index=side_index)
        bid = self._best(book["bids"], "bid")
        result = self.gateway.place_alo(market=market, side_index=side_index, is_buy=True, price=bid)
        return MakerTickResult("buy_placed", "placed first-level ALO buy", str(result["orderId"]))
