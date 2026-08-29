from decimal import Decimal

from bot.outcome_exit_lifecycle import OutcomeExitLifecycle, OutcomeExitLifecycleStore
from monitoring.trade_journal_db import TradeJournalDB


def _lifecycle(**changes):
    values = dict(wallet="0xwallet", outcome_id=1153, coin="#11530", order_id="sell-7", inventory=Decimal("13"),
                  target_price=Decimal("0.84034"), replacement_count=0, state="SELL_RESTING")
    values.update(changes)
    return OutcomeExitLifecycle(**values)


def test_lifecycle_is_durable_and_scoped_by_wallet_market_coin(tmp_path):
    store = OutcomeExitLifecycleStore(TradeJournalDB(tmp_path / "journal.db"), "run-a")
    store.record(_lifecycle(), reason="initial_protection")
    restored = store.recover(wallet="0xwallet", outcome_id=1153, coin="#11530")
    assert restored is not None and restored.order_id == "sell-7"
    assert store.recover(wallet="other", outcome_id=1153, coin="#11530") is None


def test_lifecycle_recovery_requires_exchange_order_and_inventory_match(tmp_path):
    store = OutcomeExitLifecycleStore(TradeJournalDB(tmp_path / "journal.db"), "run-a")
    store.record(_lifecycle(), reason="initial_protection")
    restored = store.reconcile_owned_sell(wallet="0xwallet", outcome_id=1153, coin="#11530", inventory=Decimal("13"),
        open_orders=[{"oid": "sell-7", "coin": "#11530", "side": "A", "sz": "13"}])
    assert restored is not None
    assert store.reconcile_owned_sell(wallet="0xwallet", outcome_id=1153, coin="#11530", inventory=Decimal("13"), open_orders=[]) is None
    latest = store.recover(wallet="0xwallet", outcome_id=1153, coin="#11530")
    assert latest is not None and latest.state == "RECONCILE_REQUIRED"


def test_unrecorded_open_order_is_never_owned(tmp_path):
    store = OutcomeExitLifecycleStore(TradeJournalDB(tmp_path / "journal.db"), "run-a")
    assert store.reconcile_owned_sell(wallet="0xwallet", outcome_id=1153, coin="#11530", inventory=Decimal("13"),
        open_orders=[{"oid": "manual", "coin": "#11530", "side": "A", "sz": "13"}]) is None


def test_flat_inventory_and_absent_owned_order_close_lifecycle(tmp_path):
    store = OutcomeExitLifecycleStore(TradeJournalDB(tmp_path / "journal.db"), "run-a")
    store.record(_lifecycle(), reason="initial_protection")
    assert store.reconcile_owned_sell(wallet="0xwallet", outcome_id=1153, coin="#11530", inventory=Decimal("0"), open_orders=[]) is None
    # CLOSED is terminal: a future restart cannot mistake it for a resting
    # sell or gain cancellation ownership from it.
    assert store.recover(wallet="0xwallet", outcome_id=1153, coin="#11530") is None
