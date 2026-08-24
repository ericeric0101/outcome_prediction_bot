from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_live_execution_runtime import OutcomeLiveExecutionRuntime
from bot.outcome_stream_health import OutcomeStreamHealth


def market(): return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "15m", "")


class Account:
    def __init__(self, balances=None, orders=None): self.balances, self.orders = balances or [{"coin": "USDH", "total": "13", "hold": "0"}], orders or []
    def get_spot_clearinghouse_state_sync(self, _): return {"balances": self.balances}
    def get_open_orders_sync(self, _): return self.orders


class Gateway:
    def outcome_coin(self, _, i): return "#11530" if i == 0 else "#11531"
    def fetch_order_book(self, **_): return {"bids": [{"price": "0.77"}], "asks": [{"price": "0.78"}]}
    def place_alo(self, **_): return {"orderId": "1"}
    def cancel_owned_order(self, **_): return {}


def healthy_stream():
    health = OutcomeStreamHealth()
    health.configure_market(market()); health.on_lifecycle("connected"); health.mark_rest_resynced()
    health.on_l2_book("#11530"); health.on_l2_book("#11531")
    return health


def test_runtime_is_disabled_without_both_operator_gates(monkeypatch):
    monkeypatch.delenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("OUTCOME_SDK_EXECUTION_ENABLED", raising=False)
    runtime = OutcomeLiveExecutionRuntime(account=Account(), wallet="w", gateway=Gateway())
    assert runtime.tick(market=market(), side_index=0, entry_permitted=True).state == "disabled"


def test_runtime_blocks_cross_side_existing_exposure(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(account=Account(orders=[{"coin": "#11531", "side": "B", "oid": 7, "sz": "13"}]), wallet="w", gateway=Gateway(), stream_health=healthy_stream())
    assert runtime.tick(market=market(), side_index=0, entry_permitted=True).state == "blocked"


def test_runtime_reduce_only_cancels_owned_buys(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    calls = []
    class CancelGateway(Gateway):
        def cancel_owned_order(self, **kwargs): calls.append(kwargs); return {}
    runtime = OutcomeLiveExecutionRuntime(account=Account(orders=[{"coin": "#11530", "side": "B", "oid": 7, "sz": "13"}]), wallet="w", gateway=CancelGateway(), stream_health=healthy_stream())
    result = runtime.cancel_resting_buys(market=market())
    assert result.state == "cancelled"
    assert calls[0]["order_id"] == "7"


def test_tick_market_advances_existing_buy_without_needing_signal(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(account=Account(orders=[{"coin": "#11530", "side": "B", "oid": 7, "sz": "13"}]), wallet="w", gateway=Gateway(), stream_health=healthy_stream())
    assert runtime.tick_market(market=market(), entry_side_index=None).state == "buy_resting"


def test_tick_market_blocks_unfunded_entry_before_order_submission(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(account=Account(balances=[{"coin": "USDH", "total": "1", "hold": "0"}]), wallet="w", gateway=Gateway(), stream_health=healthy_stream())
    assert runtime.tick_market(market=market(), entry_side_index=0).detail == "risk gate: insufficient_available_collateral"


def test_runtime_blocks_new_entry_without_ws_health_even_with_execution_gates(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(account=Account(), wallet="w", gateway=Gateway())
    assert runtime.tick_market(market=market(), entry_side_index=0).detail == "market-data gate: ws_health_not_configured"


def test_runtime_can_cancel_existing_entry_when_ws_is_stale(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(account=Account(orders=[{"coin": "#11530", "side": "B", "oid": 7, "sz": "13"}]), wallet="w", gateway=Gateway())
    assert runtime.cancel_resting_buys(market=market()).state == "cancelled"
