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
