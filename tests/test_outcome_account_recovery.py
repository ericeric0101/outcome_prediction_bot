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


def test_recovery_normalizes_plus_prefixed_outcome_inventory():
    account = Account([{"coin": "+11530", "total": "13"}], [])
    report = OutcomeAccountRecovery(account=account, wallet="w").reconcile([market()])
    assert not report.safe_for_new_entry
    assert report.findings[0].coin == "#11530"
    assert report.findings[0].state == "unprotected_inventory"


def test_recovery_blocks_orphan_sell():
    account = Account([], [{"coin": "#11530", "side": "A", "oid": 8, "sz": "13"}])
    report = OutcomeAccountRecovery(account=account, wallet="w").reconcile([market()])
    assert not report.safe_for_new_entry
    assert report.findings[0].state == "orphan_sell"


def test_recovery_blocks_multiple_same_coin_protective_sells():
    account = Account(
        [{"coin": "#11530", "total": "13"}],
        [
            {"coin": "#11530", "side": "A", "oid": "bot", "sz": "13"},
            {"coin": "#11530", "side": "A", "oid": "manual", "sz": "13"},
        ],
    )
    report = OutcomeAccountRecovery(account=account, wallet="w").reconcile([market()])
    assert not report.safe_for_new_entry
    assert report.findings[0].state == "ambiguous_protective_sells"
    assert "multiple resting sells" in report.reason


def test_same_underlying_different_outcome_id_cannot_be_selected_by_symbol():
    """BTC labels are not identity: the encoded coin/outcome id must remain distinct."""
    other_btc_market = OutcomeMarketSpec(
        1154, "@1154", "#11540", "#11541", 1, 2, "priceBinary", "BTC",
        "20260825-1400", 1, 0, Decimal("1"), "15m", "",
    )
    account = Account(
        [{"coin": "+11540", "total": "13"}],
        [{"coin": "#11540", "side": "A", "oid": "other-market-sell", "sz": "13"}],
    )
    # Looking only at the first BTC market must not conflate #11540 with
    # #11530; it becomes explicitly unmanaged exposure instead.
    first_only = OutcomeAccountRecovery(account=account, wallet="w").reconcile([market()])
    assert not first_only.safe_for_new_entry
    assert "#11540" in first_only.reason

    both = OutcomeAccountRecovery(account=account, wallet="w").reconcile([market(), other_btc_market])
    assert both.safe_for_new_entry
    assert both.findings[0].coin == "#11530" and both.findings[0].state == "flat"
    assert both.findings[2].coin == "#11540" and both.findings[2].state == "protected_inventory"
