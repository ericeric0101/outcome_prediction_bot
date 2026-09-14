from decimal import Decimal
from types import SimpleNamespace

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_exit_lifecycle import OutcomeExitLifecycleStore
from bot.outcome_exit_recovery_service import OutcomeExitRecoveryService
from monitoring.trade_journal_db import TradeJournalDB


def market() -> OutcomeMarketSpec:
    return OutcomeMarketSpec(
        1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC",
        "20260824-1400", 1, 0, Decimal("1"), "1d", "",
    )


class Account:
    def __init__(self, orders):
        self.orders = orders
        self.force_calls = 0

    def get_open_orders_sync(self, _wallet):
        raise AssertionError("ambiguous exit recovery must force a fresh open-orders read")

    def force_open_orders_reconciliation_sync(self, _wallet):
        self.force_calls += 1
        return list(self.orders)


def test_runtime_recovery_adopts_ambiguous_sell_then_releases_barrier(tmp_path):
    """Pin the live service → store recovery path against future refactors."""
    journal = TradeJournalDB(tmp_path / "recovery.db")
    store = OutcomeExitLifecycleStore(journal, "run")
    intent = store.record_submit_intent(
        wallet="w", outcome_id=1153, coin="#11530", order_kind="exit_replacement_alo",
        price=Decimal("0.76"), shares=Decimal("13"), old_order_id="old-sell",
        replacement_count=1, intended_state="LOSS_BAND_RESTING",
    )
    assert intent is not None
    assert store.record_ambiguous_submit(
        wallet="w", outcome_id=1153, coin="#11530", intent_id=intent[0], intent_event_id=intent[1],
        order_kind="exit_replacement_alo", price=Decimal("0.76"), shares=Decimal("13"),
        old_order_id="old-sell", replacement_count=1, intended_state="LOSS_BAND_RESTING",
        sidecar_request_id="request-1", command="place_limit_order", detail="timeout",
    ) is not None
    account = Account([{"oid": "replacement", "coin": "#11530", "side": "A", "sz": "13", "limitPx": "0.76"}])
    recovery = SimpleNamespace(wallet="w", account=account)
    service = OutcomeExitRecoveryService(recovery=recovery, store=store, loss_reentry_gate=None)
    report = SimpleNamespace(findings=(
        SimpleNamespace(market_id=1153, coin="#11530", inventory=Decimal("13")),
        SimpleNamespace(market_id=1153, coin="#11531", inventory=Decimal("0")),
    ))

    service.reconcile(market=market(), report=report)

    adopted = store.recover(wallet="w", outcome_id=1153, coin="#11530")
    assert account.force_calls == 1
    assert adopted is not None and adopted.order_id == "replacement"
    assert service.ambiguity_barrier(market=market()) is None
