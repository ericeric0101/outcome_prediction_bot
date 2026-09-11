from bot.outcome_account_read_cache import OutcomeAccountReadCache


class Account:
    def __init__(self): self.calls = 0
    def get_spot_clearinghouse_state_sync(self, _): self.calls += 1; return {"balances": []}


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
