from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_maker_state_machine import OutcomeMakerStateMachine


def market():
    return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "15m", "")


class Account:
    def __init__(self, total="0", orders=None, fills=None): self.total, self.orders, self.fills = total, orders or [], fills or []
    def get_spot_clearinghouse_state_sync(self, _): return {"balances": [{"coin": "#11530", "total": self.total}]}
    def get_open_orders_sync(self, _): return self.orders
    def get_user_fills_sync(self, _): return self.fills


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
    assert gateway.calls[0][1]["reduce_only"] is True


def test_tick_recognizes_hyperliquid_plus_prefixed_spot_inventory():
    gateway = Gateway()
    account = Account("0")
    account.get_spot_clearinghouse_state_sync = lambda _: {"balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}]}
    result = OutcomeMakerStateMachine(account=account, gateway=gateway, wallet="w").tick(
        market=market(), side_index=0, entry_permitted=False,
    )
    assert result.state == "sell_placed"


def test_calibration_inventory_uses_net_ten_percent_take_profit():
    gateway = Gateway()
    account = Account("0")
    account.get_spot_clearinghouse_state_sync = lambda _: {"balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}]}
    account.get_user_fills_sync = lambda _: [{"coin": "#11530", "side": "B", "px": "0.80", "sz": "13", "time": 1}]
    result = OutcomeMakerStateMachine(account=account, gateway=gateway, wallet="w").tick(
        market=market(), side_index=0, entry_permitted=False,
        minimum_return_pct=Decimal("0.10"), maker_close_fee_rate=Decimal("0.0004"),
    )
    assert result.state == "sell_placed"
    assert gateway.calls[0][1]["price"] == Decimal("0.80") * Decimal("1.10") / Decimal("0.9996")
    assert result.audit == {
        "inventory": "13", "account_entry_vwap": str(Decimal("10") / Decimal("13")), "fill_entry_vwap": "0.80",
        "pricing_basis": "exchange_fill_vwap", "requested_price": str(Decimal("0.80") * Decimal("1.10") / Decimal("0.9996")),
        "take_profit_price": str(Decimal("0.80") * Decimal("1.10") / Decimal("0.9996")),
        "loss_reprice_floor": "unavailable", "exit_mode": "take_profit",
    }


def test_calibration_refuses_unverifiable_account_entry_notional():
    gateway = Gateway()
    account = Account("0")
    account.get_spot_clearinghouse_state_sync = lambda _: {"balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}]}
    result = OutcomeMakerStateMachine(account=account, gateway=gateway, wallet="w").tick(
        market=market(), side_index=0, entry_permitted=False,
        minimum_return_pct=Decimal("0.05"), maker_close_fee_rate=Decimal("0.0004"),
    )
    assert result.state == "blocked"
    assert "cannot verify fill VWAP" in result.detail
    assert not gateway.calls


def test_calibration_loss_band_cancels_old_profit_sell_without_taking():
    class LossGateway(Gateway):
        def fetch_order_book(self, **_): return {"bids": [{"price": "0.70"}], "asks": [{"price": "0.71"}]}
    gateway = LossGateway()
    account = Account("0", [{"coin": "#11530", "side": "A", "oid": 9, "sz": "13", "limitPx": "0.85"}])
    account.get_spot_clearinghouse_state_sync = lambda _: {"balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}]}
    account.get_user_fills_sync = lambda _: [{"coin": "#11530", "side": "B", "px": "0.80", "sz": "13", "time": 1}]
    result = OutcomeMakerStateMachine(account=account, gateway=gateway, wallet="w").tick(
        market=market(), side_index=0, entry_permitted=False,
        minimum_return_pct=Decimal("0.05"), maker_close_fee_rate=Decimal("0.0004"), loss_reprice_pct=Decimal("0.05"),
    )
    assert result.state == "blocked"
    assert gateway.calls[0][0] == "cancel"
    assert gateway.calls[0][1]["order_id"] == "9"


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
