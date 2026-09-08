from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_entry_lifecycle import OutcomeEntryLifecycle, OutcomeEntryLifecycleStore
from bot.outcome_entry_requote import (
    EntryQuoteAction,
    EntryQuoteInput,
    EntryQuotePlan,
    OutcomeEntryQuotePlanner,
    OutcomeEntryQuotePlannerConfig,
    OutcomeEntryFastRiskTracker,
    OutcomeEntryRequoteController,
)
from bot.outcome_account_read_cache import OutcomeAccountReadCache
from monitoring.trade_journal_db import TradeJournalDB


def market():
    return OutcomeMarketSpec(1356, "@1356", "#13560", "#13561", 1, 2, "priceBinary", "BTC", "", 0, 0, Decimal("1"), "1d", "")


def test_entry_planner_only_cancels_after_age_and_fresh_contradiction():
    planner = OutcomeEntryQuotePlanner(OutcomeEntryQuotePlannerConfig(min_requote_interval_sec=300))
    young = EntryQuoteInput(1, Decimal("0.60"), None, None, "directional_confirmation_not_met", 299)
    assert planner.plan(young).action is EntryQuoteAction.KEEP
    stale_feed = EntryQuoteInput(1, Decimal("0.60"), None, None, "oi_observation_stale", 301)
    assert planner.plan(stale_feed).action is EntryQuoteAction.KEEP
    contradicted = EntryQuoteInput(1, Decimal("0.60"), None, None, "directional_confirmation_not_met", 301)
    assert planner.plan(contradicted) == EntryQuotePlan(EntryQuoteAction.CANCEL, "entry_signal_no_longer_confirmed")


def test_confirmed_fast_risk_cancels_without_lowering_normal_five_minute_lane():
    planner = OutcomeEntryQuotePlanner(OutcomeEntryQuotePlannerConfig(min_requote_interval_sec=300))
    pending = EntryQuoteInput(
        1, Decimal("0.60"), None, None, "directional_confirmation_not_met", 20,
        fast_risk_confirmed=False, fast_risk_reason="confirmed_signal_invalidation",
    )
    assert planner.plan(pending).reason == "entry_requote_interval_not_elapsed"
    confirmed = EntryQuoteInput(
        1, Decimal("0.60"), None, None, "directional_confirmation_not_met", 20,
        fast_risk_confirmed=True, fast_risk_reason="confirmed_signal_invalidation",
    )
    assert planner.plan(confirmed) == EntryQuotePlan(
        EntryQuoteAction.CANCEL, "entry_fast_risk_cancel:confirmed_signal_invalidation",
    )


def test_fast_risk_tracker_requires_three_samples_and_five_seconds_and_resets():
    tracker = OutcomeEntryFastRiskTracker()
    key = (1356, "#13561")
    assert not tracker.observe(key=key, reason="confirmed_side_flip", now=10).confirmed
    assert not tracker.observe(key=key, reason="confirmed_side_flip", now=12.5).confirmed
    assert tracker.observe(key=key, reason="confirmed_side_flip", now=15).confirmed
    assert tracker.observe(key=key, reason=None, now=16).observation_count == 0
    assert not tracker.observe(key=key, reason="confirmed_side_flip", now=17).confirmed


def test_entry_planner_cancels_side_or_first_level_change_with_hysteresis():
    planner = OutcomeEntryQuotePlanner(OutcomeEntryQuotePlannerConfig(min_requote_interval_sec=1, tick_size=Decimal("0.00001")))
    same = EntryQuoteInput(1, Decimal("0.60000"), 1, Decimal("0.60001"), "confirmed", 2)
    assert planner.plan(same).action is EntryQuoteAction.KEEP
    changed = EntryQuoteInput(1, Decimal("0.60000"), 1, Decimal("0.60100"), "confirmed", 2)
    assert planner.plan(changed).reason == "entry_first_level_bid_changed"
    flipped = EntryQuoteInput(1, Decimal("0.60000"), 0, Decimal("0.40000"), "confirmed", 2)
    assert planner.plan(flipped).reason == "entry_side_changed_after_fresh_confirmation"


def _audited_submit(journal, order_id="buy-1"):
    journal.log_order_event("run", "ORDER_SUBMIT", venue_order_id=order_id, side="BUY", status="RESTING", instrument_id="#13561", payload={
        "venue": "hyperliquid_outcome", "outcome_id": 1356, "coin": "#13561",
        "audit": {"entry_policy_schema_version": 1, "entry_policy_kind": "s0_oi_spot_mark_confirmation", "entry_bid_at_decision": "0.60"},
    })


def test_entry_store_adopts_only_exact_audited_s0_buy(tmp_path):
    journal = TradeJournalDB(tmp_path / "entry.db")
    store = OutcomeEntryLifecycleStore(journal, "run")
    _audited_submit(journal)
    lifecycle = store.recover_or_adopt_audited_submit(wallet="w", outcome_id=1356, coin="#13561", open_orders=[
        {"coin": "#13561", "side": "B", "oid": "buy-1", "limitPx": "0.60"},
    ])
    assert lifecycle is not None and lifecycle.order_id == "buy-1"
    assert store.recover(wallet="w", outcome_id=1356, coin="#13561") is not None
    manual_journal = TradeJournalDB(tmp_path / "manual.db")
    manual_store = OutcomeEntryLifecycleStore(manual_journal, "run")
    assert manual_store.recover_or_adopt_audited_submit(wallet="w", outcome_id=1356, coin="#13561", open_orders=[
        {"coin": "#13561", "side": "B", "oid": "manual", "limitPx": "0.60"},
    ]) is None


def test_fast_cancel_cooldown_is_durable(tmp_path):
    journal = TradeJournalDB(tmp_path / "cooldown.db")
    store = OutcomeEntryLifecycleStore(journal, "run")
    lifecycle = OutcomeEntryLifecycle("w", 1356, "#13561", "buy-1", Decimal("0.60"), 0, "BUY_RESTING")
    store.record(lifecycle, reason="entry_fast_risk_cancel:confirmed_side_flip", extra={"state": "CANCELLED"})
    assert store.fast_rebook_cooldown_remaining(
        wallet="w", outcome_id=1356, coin="#13561", cooldown_sec=30,
    ) > 0


class Account:
    def __init__(self):
        self.orders = [{"coin": "#13561", "side": "B", "oid": "buy-1", "limitPx": "0.60", "sz": "18"}]

    def get_open_orders_sync(self, _): return list(self.orders)
    def get_spot_clearinghouse_state_sync(self, _): return {"balances": []}


class Gateway:
    def __init__(self, account): self.account, self.calls = account, []
    def cancel_owned_order(self, **kwargs):
        self.calls.append(kwargs)
        self.account.orders = []
        return {}


def test_entry_cancel_requires_truth_then_waits_for_next_tick_to_rebook(tmp_path):
    journal = TradeJournalDB(tmp_path / "entry.db")
    store = OutcomeEntryLifecycleStore(journal, "run")
    _audited_submit(journal)
    account = Account(); gateway = Gateway(account)
    lifecycle = store.recover_or_adopt_audited_submit(wallet="w", outcome_id=1356, coin="#13561", open_orders=account.orders)
    controller = OutcomeEntryRequoteController(account=account, gateway=gateway, store=store, wallet="w")
    result = controller.execute_cancel(market=market(), side_index=1, lifecycle=lifecycle,
                                       plan=EntryQuotePlan(EntryQuoteAction.CANCEL, "entry_first_level_bid_changed"))
    assert result.state == "cancelled"
    assert gateway.calls[0]["order_id"] == "buy-1"
    assert store.recover(wallet="w", outcome_id=1356, coin="#13561") is None


def test_entry_cancel_invalidates_shared_account_cache_before_confirmation(tmp_path):
    """A cancel confirmation must never read the stale pre-cancel order cache."""
    journal = TradeJournalDB(tmp_path / "entry_cached.db")
    store = OutcomeEntryLifecycleStore(journal, "run")
    _audited_submit(journal)
    raw_account = Account(); gateway = Gateway(raw_account)
    lifecycle = store.recover_or_adopt_audited_submit(
        wallet="w", outcome_id=1356, coin="#13561", open_orders=raw_account.orders,
    )
    cached_account = OutcomeAccountReadCache(raw_account)
    # Populate the same per-tick cache that the controller will use.
    assert cached_account.get_open_orders_sync("w")
    controller = OutcomeEntryRequoteController(account=cached_account, gateway=gateway, store=store, wallet="w")
    result = controller.execute_cancel(
        market=market(), side_index=1, lifecycle=lifecycle,
        plan=EntryQuotePlan(EntryQuoteAction.CANCEL, "entry_first_level_bid_changed"),
    )
    assert result.state == "cancelled"


def test_filled_entry_cancel_confirms_remainder_is_gone_before_protective_exit(tmp_path):
    journal = TradeJournalDB(tmp_path / "filled_entry.db")
    store = OutcomeEntryLifecycleStore(journal, "run")
    _audited_submit(journal)

    class FilledAccount(Account):
        def get_spot_clearinghouse_state_sync(self, _):
            return {"balances": [{"coin": "+13561", "total": "7"}]}

    account = FilledAccount(); gateway = Gateway(account)
    lifecycle = store.recover_or_adopt_audited_submit(
        wallet="w", outcome_id=1356, coin="#13561", open_orders=account.orders,
    )
    controller = OutcomeEntryRequoteController(account=account, gateway=gateway, store=store, wallet="w")
    result = controller.execute_cancel_after_fill(market=market(), side_index=1, lifecycle=lifecycle)
    assert result.state == "cancelled_after_fill"
    assert gateway.calls == [{"market": market(), "side_index": 1, "order_id": "buy-1"}]
    assert store.recover(wallet="w", outcome_id=1356, coin="#13561") is None
