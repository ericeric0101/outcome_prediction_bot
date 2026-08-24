from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_maker_state_machine import OutcomeMakerStateMachine


def market():
    return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "15m", "")


class Account:
    def __init__(self, total="0", orders=None): self.total, self.orders = total, orders or []
    def get_spot_clearinghouse_state_sync(self, _): return {"balances": [{"coin": "#11530", "total": self.total}]}
    def get_open_orders_sync(self, _): return self.orders


class Gateway:
    def __init__(self): self.calls = []
    def outcome_coin(self, _, side): return "#11530" if side == 0 else "#11531"
    def fetch_order_book(self, **_): return {"bids": [{"price": "0.77"}], "asks": [{"price": "0.78"}]}
    def place_alo(self, **kwargs): self.calls.append(("place", kwargs)); return {"orderId": "9"}
    def cancel_owned_order(self, **kwargs): self.calls.append(("cancel", kwargs)); return {"ok": True}


def test_tick_places_one_buy_without_waiting():
    gateway = Gateway()
    result = OutcomeMakerStateMachine(account=Account(), gateway=gateway, wallet="w").tick(market=market(), side_index=0, entry_permitted=True)
    assert result.state == "buy_placed"
    assert [kind for kind, _ in gateway.calls] == ["place"]


def test_tick_never_adds_to_filled_inventory_and_places_protective_sell():
    gateway = Gateway()
    result = OutcomeMakerStateMachine(account=Account("13"), gateway=gateway, wallet="w").tick(market=market(), side_index=0, entry_permitted=True)
    assert result.state == "sell_placed"
    assert gateway.calls[0][1]["is_buy"] is False
    assert gateway.calls[0][1]["requested_shares"] == Decimal("13")


def test_tick_cancels_partial_buy_before_sale():
    gateway = Gateway()
    result = OutcomeMakerStateMachine(account=Account("3", [{"coin": "#11530", "side": "B", "oid": 7, "sz": "10"}]), gateway=gateway, wallet="w").tick(market=market(), side_index=0, entry_permitted=True)
    assert result.state == "blocked"
    assert gateway.calls[0][0] == "cancel"


def test_tick_observes_existing_order_without_second_submission():
    gateway = Gateway()
    result = OutcomeMakerStateMachine(account=Account("0", [{"coin": "#11530", "side": "B", "oid": 7, "sz": "13"}]), gateway=gateway, wallet="w").tick(market=market(), side_index=0, entry_permitted=True)
    assert result.state == "buy_resting"
    assert not gateway.calls
