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
    def _coin_position(state: dict[str, Any], coin: str) -> tuple[Decimal, Decimal]:
        # HIP-4 books use ``#<id>`` while spot-clearinghouse inventory is
        # returned as ``+<id>``.  Fixtures may use either representation.
        balance_coin = "+" + coin[1:] if coin.startswith("#") else coin
        for balance in state.get("balances", []):
            if balance.get("coin") in {coin, balance_coin}:
                return Decimal(str(balance.get("total", "0"))), Decimal(str(balance.get("entryNtl", "0")))
        return Decimal("0"), Decimal("0")

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

    @staticmethod
    def _target_sell_price(
        *, avg_entry_price: Decimal, minimum_return_pct: Decimal | None, maker_close_fee_rate: Decimal | None,
    ) -> Decimal | None:
        if minimum_return_pct is None:
            return None
        if avg_entry_price <= 0 or minimum_return_pct < 0 or maker_close_fee_rate is None:
            return None
        target = avg_entry_price * (Decimal("1") + minimum_return_pct) / (Decimal("1") - maker_close_fee_rate)
        if target >= Decimal("0.99999"):
            return None
        return target

    @staticmethod
    def _loss_reprice_floor(
        *, avg_entry_price: Decimal, loss_reprice_pct: Decimal | None, maker_close_fee_rate: Decimal | None,
    ) -> Decimal | None:
        if loss_reprice_pct is None:
            return None
        if avg_entry_price <= 0 or loss_reprice_pct < 0 or maker_close_fee_rate is None:
            return None
        floor = avg_entry_price * (Decimal("1") - loss_reprice_pct) / (Decimal("1") - maker_close_fee_rate)
        return floor if Decimal("0") < floor < Decimal("0.99999") else None

    def tick(
        self, *, market: OutcomeMarketSpec, side_index: int, entry_permitted: bool,
        minimum_return_pct: Decimal | None = None, maker_close_fee_rate: Decimal | None = None,
        loss_reprice_pct: Decimal | None = None,
    ) -> MakerTickResult:
        coin = self.gateway.outcome_coin(market, side_index)
        inventory, entry_notional = self._coin_position(self.account.get_spot_clearinghouse_state_sync(self.wallet), coin)
        orders = self._orders(coin)
        buys = [order for order in orders if order.get("side") == "B"]
        sells = [order for order in orders if order.get("side") == "A"]

        if inventory > 0:
            avg_entry = (entry_notional / inventory) if inventory else Decimal("0")
            profit_target = self._target_sell_price(
                avg_entry_price=avg_entry, minimum_return_pct=minimum_return_pct, maker_close_fee_rate=maker_close_fee_rate,
            )
            loss_floor = self._loss_reprice_floor(
                avg_entry_price=avg_entry, loss_reprice_pct=loss_reprice_pct, maker_close_fee_rate=maker_close_fee_rate,
            )
            if minimum_return_pct is not None and profit_target is None:
                return MakerTickResult("blocked", "calibration take-profit target is not executable; inventory requires explicit reconciliation")
            covering = next((order for order in sells if Decimal(str(order.get("sz", "0"))) >= inventory), None)
            if covering:
                # ALO cannot guarantee an immediate stop.  Once the midpoint
                # has crossed the configured loss threshold, cancel the old
                # profit quote once and let the next tick rest a new maker-only
                # protection price.  Never cross the bid or submit a taker.
                book = self.gateway.fetch_order_book(market=market, side_index=side_index)
                bid, ask = self._best(book["bids"], "bid"), self._best(book["asks"], "ask")
                midpoint = (bid + ask) / Decimal("2")
                loss_triggered = loss_floor is not None and midpoint <= avg_entry * (Decimal("1") - loss_reprice_pct)
                existing_price = Decimal(str(covering.get("limitPx", covering.get("px", "0"))))
                if loss_triggered and existing_price > loss_floor:
                    self.gateway.cancel_owned_order(market=market, side_index=side_index, order_id=str(covering["oid"]))
                    return MakerTickResult("blocked", "loss threshold crossed; cancelled old profit sell for maker-only protection reprice", str(covering["oid"]))
                return MakerTickResult("sell_resting", "inventory is protected by owned ALO sell", str(covering.get("oid")))
            if buys:
                # Never add exposure after any fill.  The next tick will see
                # the cancelled remainder and then post the protective sale.
                order = buys[0]
                self.gateway.cancel_owned_order(market=market, side_index=side_index, order_id=str(order["oid"]))
                return MakerTickResult("blocked", "cancelled unfilled buy remainder before protective sell", str(order["oid"]))
            book = self.gateway.fetch_order_book(market=market, side_index=side_index)
            bid, ask = self._best(book["bids"], "bid"), self._best(book["asks"], "ask")
            midpoint = (bid + ask) / Decimal("2")
            loss_triggered = loss_floor is not None and midpoint <= avg_entry * (Decimal("1") - loss_reprice_pct)
            target = loss_floor if loss_triggered else profit_target
            assert target is not None or minimum_return_pct is None
            target = target or ask
            result = self.gateway.place_alo(
                market=market,
                side_index=side_index,
                is_buy=False,
                price=max(ask, target),
                requested_shares=inventory,
                # ``inventory`` came from this tick's wallet reconciliation;
                # allow the official SDK's documented residual-close exception.
                reduce_only=True,
            )
            detail = "placed maker-only loss-band protection sell" if loss_triggered else "placed net take-profit ALO sell for reconciled inventory"
            return MakerTickResult("sell_placed", detail, str(result["orderId"]))

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
