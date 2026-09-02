from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_entry_lifecycle import OutcomeEntryLifecycle, OutcomeEntryLifecycleStore
from bot.outcome_entry_requote import (
    EntryQuoteAction,
    EntryQuoteInput,
    EntryQuotePlan,
    OutcomeEntryQuotePlanner,
    OutcomeEntryQuotePlannerConfig,
    OutcomeEntryRequoteController,
)
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
