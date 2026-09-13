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


def test_ambiguous_exit_adopts_only_exact_matching_resting_sell(tmp_path):
    store = OutcomeExitLifecycleStore(TradeJournalDB(tmp_path / "journal.db"), "run-a")
    intent = store.record_submit_intent(
        wallet="0xwallet", outcome_id=1153, coin="#11530",
        order_kind="exit_replacement_alo", price=Decimal("0.76"), shares=Decimal("13"),
        old_order_id="old-7", replacement_count=1, intended_state="LOSS_BAND_RESTING",
    )
    assert intent is not None
    intent_id, intent_event_id = intent
    assert store.record_ambiguous_submit(
        wallet="0xwallet", outcome_id=1153, coin="#11530", intent_id=intent_id,
        intent_event_id=intent_event_id, order_kind="exit_replacement_alo",
        price=Decimal("0.76"), shares=Decimal("13"), old_order_id="old-7",
        replacement_count=1, intended_state="LOSS_BAND_RESTING",
        sidecar_request_id="request-1", command="place_limit_order", detail="timeout",
    ) is not None
    status, lifecycle = store.reconcile_ambiguous_submit(
        wallet="0xwallet", outcome_id=1153, coin="#11530", inventory=Decimal("13"),
        open_orders=[{"oid": "new-9", "coin": "#11530", "side": "A", "sz": "13", "limitPx": "0.76"}],
    )
    assert status == "adopted"
    assert lifecycle is not None and lifecycle.order_id == "new-9"
    assert store.pending_ambiguous_submit(wallet="0xwallet", outcome_id=1153, coin="#11530") is None


def test_ambiguous_exit_refuses_unrelated_sell_and_fences_visibility(tmp_path):
    store = OutcomeExitLifecycleStore(TradeJournalDB(tmp_path / "journal.db"), "run-a")
    intent = store.record_submit_intent(
        wallet="0xwallet", outcome_id=1153, coin="#11530",
        order_kind="initial_protective_alo", price=Decimal("0.76"), shares=Decimal("13"),
        old_order_id=None, replacement_count=0, intended_state="SELL_RESTING",
    )
    assert intent is not None
    intent_id, intent_event_id = intent
    store.record_ambiguous_submit(
        wallet="0xwallet", outcome_id=1153, coin="#11530", intent_id=intent_id,
        intent_event_id=intent_event_id, order_kind="initial_protective_alo",
        price=Decimal("0.76"), shares=Decimal("13"), old_order_id=None,
        replacement_count=0, intended_state="SELL_RESTING",
        sidecar_request_id="request-2", command="place_limit_order", detail="timeout",
    )
    pending = store.pending_ambiguous_submit(wallet="0xwallet", outcome_id=1153, coin="#11530")
    assert pending is not None
    status, lifecycle = store.reconcile_ambiguous_submit(
        wallet="0xwallet", outcome_id=1153, coin="#11530", inventory=Decimal("13"),
        open_orders=[{"oid": "manual", "coin": "#11530", "side": "A", "sz": "13", "limitPx": "0.77"}],
        now=float(pending["ambiguity_recorded_at_ts"]) + 60,
    )
    assert status == "pending" and lifecycle is None


def test_unfinalized_sell_intent_alone_is_a_crash_recovery_fence(tmp_path):
    store = OutcomeExitLifecycleStore(TradeJournalDB(tmp_path / "journal.db"), "run-a")
    intent = store.record_submit_intent(
        wallet="0xwallet", outcome_id=1153, coin="#11530",
        order_kind="initial_protective_alo", price=Decimal("0.76"), shares=Decimal("13"),
        old_order_id=None, replacement_count=0, intended_state="SELL_RESTING",
    )
    assert intent is not None
    pending = store.pending_ambiguous_submit(wallet="0xwallet", outcome_id=1153, coin="#11530")
    assert pending is not None and pending["state"] == "UNACKNOWLEDGED_INTENT"
    assert store.finalize_submit_intent(intent_id=intent[0], order_id=None, reason="safe_rejection")
    assert store.pending_ambiguous_submit(wallet="0xwallet", outcome_id=1153, coin="#11530") is None
