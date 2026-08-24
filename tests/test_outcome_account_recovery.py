from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_account_recovery import OutcomeAccountRecovery


def market(): return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "15m", "")


class Account:
    def __init__(self, balances, orders): self.balances, self.orders = balances, orders
    def get_spot_clearinghouse_state_sync(self, _): return {"balances": self.balances}
    def get_open_orders_sync(self, _): return self.orders


def test_recovery_allows_flat_or_protected_inventory():
    account = Account([{"coin": "#11530", "total": "13"}], [{"coin": "#11530", "side": "A", "oid": 9, "sz": "13"}])
    report = OutcomeAccountRecovery(account=account, wallet="w").reconcile([market()])
    assert report.safe_for_new_entry
    assert report.findings[0].state == "protected_inventory"


def test_recovery_blocks_unprotected_or_unknown_exposure():
    account = Account([{"coin": "#9990", "total": "2"}, {"coin": "#11530", "total": "1"}], [])
    report = OutcomeAccountRecovery(account=account, wallet="w").reconcile([market()])
    assert not report.safe_for_new_entry
    assert "unmanaged" in report.reason
    assert "no covering sell" in report.reason


def test_recovery_blocks_orphan_sell():
    account = Account([], [{"coin": "#11530", "side": "A", "oid": 8, "sz": "13"}])
    report = OutcomeAccountRecovery(account=account, wallet="w").reconcile([market()])
    assert not report.safe_for_new_entry
    assert report.findings[0].state == "orphan_sell"
