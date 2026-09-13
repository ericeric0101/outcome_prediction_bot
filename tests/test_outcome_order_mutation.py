from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_order_mutation import cancel_and_confirm


def _market():
    return OutcomeMarketSpec(
        1, "@1", "#10", "#11", 1, 2, "priceBinary", "BTC", "20260912-1400",
        1, 0, Decimal("1"), "1d", "",
    )


class Account:
    def __init__(self, orders):
        self.orders = list(orders)
        self.forced_reads = 0
        self.invalidations = 0

    def force_open_orders_reconciliation_sync(self, _wallet):
        self.forced_reads += 1
        return list(self.orders)

    def invalidate(self):
        self.invalidations += 1

    def get_open_orders_sync(self, _wallet):
        return list(self.orders)


class Gateway:
    def __init__(self, account):
        self.account = account
        self.cancels = 0

    def cancel_owned_order(self, **_kwargs):
        self.cancels += 1
        self.account.orders = []


def test_cancel_requires_forced_rest_before_mutation_and_fresh_read_after():
    account = Account([{"oid": "7"}])
    gateway = Gateway(account)
    result = cancel_and_confirm(
        account=account, gateway=gateway, wallet="w", market=_market(), side_index=0, order_id="7",
    )
    assert result.confirmed
    assert account.forced_reads == 1
    assert gateway.cancels == 1
    assert account.invalidations == 1


def test_cancel_does_not_mutate_when_preflight_rest_no_longer_sees_order():
    account = Account([])
    gateway = Gateway(account)
    result = cancel_and_confirm(
        account=account, gateway=gateway, wallet="w", market=_market(), side_index=0, order_id="7",
    )
    assert not result.confirmed
    assert result.reason == "old_order_not_open_before_cancel"
    assert gateway.cancels == 0
