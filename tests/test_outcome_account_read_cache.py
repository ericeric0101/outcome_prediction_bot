from bot.outcome_account_read_cache import OutcomeAccountReadCache
from bot.outcome_open_orders_stream import OutcomeOpenOrdersStreamCache


class Account:
    def __init__(self): self.calls = 0
    def get_spot_clearinghouse_state_sync(self, _): self.calls += 1; return {"balances": []}


class OrderAccount:
    def __init__(self):
        self.calls = 0
        self.orders = [{"oid": 7, "coin": "#1", "side": "B", "sz": "10", "limitPx": "0.6"}]

    def get_open_orders_sync(self, _user):
        self.calls += 1
        return [dict(row) for row in self.orders]


def test_account_read_cache_shares_only_one_tick_and_invalidates():
    account = Account()
    cached = OutcomeAccountReadCache(account)
    assert cached.get_spot_clearinghouse_state_sync("w") == {"balances": []}
    assert cached.get_spot_clearinghouse_state_sync("w") == {"balances": []}
    assert account.calls == 1
    cached.invalidate()
    cached.get_spot_clearinghouse_state_sync("w")
    assert account.calls == 2
    cached.begin_tick()
    cached.get_spot_clearinghouse_state_sync("w")
    assert account.calls == 3


def test_account_read_cache_reports_only_real_endpoint_costs_per_tick():
    account = Account()
    cached = OutcomeAccountReadCache(account)
    cached.get_spot_clearinghouse_state_sync("w")
    cached.get_spot_clearinghouse_state_sync("w")
    summary = cached.timing_summary()["get_spot_clearinghouse_state_sync"]
    assert summary["calls"] == 2
    assert summary["cache_hits"] == 1
    assert summary["network_calls"] == 1
    assert summary["elapsed_ms"] >= 0
    cached.begin_tick()
    assert cached.timing_summary() == {}


def test_open_orders_switches_to_ws_only_after_rest_and_returns_to_rest_after_mutation():
    wallet = "0x" + "a" * 40
    account = OrderAccount()
    stream = OutcomeOpenOrdersStreamCache(wallet)
    stream.on_lifecycle("connected")
    stream.on_message({"data": {"user": wallet, "orders": account.orders}})
    cached = OutcomeAccountReadCache(account)
    cached.set_open_orders_stream(stream)

    assert cached.get_open_orders_sync(wallet) == account.orders
    assert account.calls == 1
    cached.begin_tick()
    assert cached.get_open_orders_sync(wallet) == account.orders
    assert account.calls == 1
    summary = cached.timing_summary()["get_open_orders_sync"]
    assert summary["ws_hits"] == 1
    assert summary["network_calls"] == 0

    cached.invalidate()
    cached.begin_tick()
    assert cached.get_open_orders_sync(wallet) == account.orders
    assert account.calls == 2


def test_forced_open_order_reconciliation_always_bypasses_tick_and_ws_cache():
    wallet = "0x" + "a" * 40
    account = OrderAccount()
    stream = OutcomeOpenOrdersStreamCache(wallet)
    stream.on_lifecycle("connected")
    stream.on_message({"data": {"user": wallet, "orders": account.orders}})
    cached = OutcomeAccountReadCache(account)
    cached.set_open_orders_stream(stream)
    cached.get_open_orders_sync(wallet)
    assert account.calls == 1
    cached.force_open_orders_reconciliation_sync(wallet)
    assert account.calls == 2
    assert cached.timing_summary()["get_open_orders_sync"]["network_calls"] == 2
