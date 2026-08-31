import time
from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_emergency_exit import (
    EmergencyExitAction,
    OutcomeEmergencyExitController,
    OutcomeEmergencyExitInput,
    OutcomeEmergencyExitPolicy,
    parse_bid_levels,
)
from bot.outcome_exit_lifecycle import OutcomeExitLifecycle, OutcomeExitLifecycleStore
from monitoring.trade_journal_db import TradeJournalDB


def market():
    return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "1d", "")


def item(**changes):
    data = {
        "inventory": Decimal("13"), "fill_vwap": Decimal("0.80"),
        "taker_close_fee_rate": Decimal("0.0007"),
        "bids": ((Decimal("0.730"), Decimal("20")),), "book_age_sec": 1.0,
        "holding_age_sec": 7200.0, "loss_band_unfilled_sec": 1200.0,
        "reversal_independent_observations": 3, "reversal_duration_sec": 120.0,
    }
    data.update(changes)
    return OutcomeEmergencyExitInput(**data)


def test_policy_requires_all_gates_and_full_l2_depth_within_fee_inclusive_cap():
    policy = OutcomeEmergencyExitPolicy()
    allowed = policy.plan(item())
    assert allowed.action is EmergencyExitAction.EXECUTE
    assert allowed.limit_price == Decimal("0.70450")
    assert allowed.executable_vwap == Decimal("0.730")
    assert allowed.net_return_pct < Decimal("-0.08")
    assert allowed.net_return_pct >= Decimal("-0.12")

    assert policy.plan(item(holding_age_sec=7199)).reason == "minimum_holding_time_not_reached"
    assert policy.plan(item(loss_band_unfilled_sec=1199)).reason == "passive_loss_band_wait_not_elapsed"
    assert policy.plan(item(reversal_independent_observations=2)).reason == "independent_reversal_observations_not_met"
    assert policy.plan(item(reversal_duration_sec=119)).reason == "reversal_duration_not_met"
    assert policy.plan(item(book_age_sec=16)).reason == "stale_or_missing_rest_l2"
    assert policy.plan(item(bids=((Decimal("0.730"), Decimal("12")),))).reason == "insufficient_full_inventory_depth_at_loss_cap"
    assert policy.plan(item(bids=((Decimal("0.700"), Decimal("20")),))).reason == "insufficient_full_inventory_depth_at_loss_cap"


def test_policy_does_not_take_small_adverse_move_or_repeat_an_attempt():
    policy = OutcomeEmergencyExitPolicy()
    assert policy.plan(item(bids=((Decimal("0.75"), Decimal("20")),))).reason == "emergency_loss_trigger_not_reached"
    assert policy.plan(item(already_attempted=True)).reason == "emergency_exit_already_attempted"


def test_raw_sdk_book_levels_must_be_positive_and_descending():
    assert parse_bid_levels({"bids": [{"price": "0.73", "size": "13"}]}) == ((Decimal("0.73"), Decimal("13")),)
    assert parse_bid_levels({"bids": [{"price": "0.70", "size": "13"}, {"price": "0.71", "size": "1"}]}) is None
    assert parse_bid_levels({"bids": [{"price": "0.70"}]}) is None


class Account:
    def __init__(self):
        self.orders = [{"oid": "old-7", "coin": "#11530", "side": "A", "sz": "13"}]
        self.total = "13"

    def get_open_orders_sync(self, _): return list(self.orders)
    def get_spot_clearinghouse_state_sync(self, _): return {"balances": [{"coin": "+11530", "total": self.total}]}


class Gateway:
    def __init__(self, account):
        self.account, self.calls = account, []
        self.book = {"timestamp": int(time.time() * 1000), "bids": [{"price": "0.730", "size": "20"}], "asks": [{"price": "0.731", "size": "20"}]}

    def cancel_owned_order(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        self.account.orders = []
        return {"ok": True}

    def fetch_order_book(self, **kwargs):
        self.calls.append(("book", kwargs)); return self.book

    def place_price_protected_ioc_exit(self, **kwargs):
        self.calls.append(("ioc", kwargs)); return {"orderId": "emergency-8", "status": "filled"}


def test_controller_cancels_confirms_rechecks_depth_then_submits_one_ioc(tmp_path):
    account, gateway = Account(), Gateway(None)
    gateway.account = account
    store = OutcomeExitLifecycleStore(TradeJournalDB(tmp_path / "journal.db"), "run")
    lifecycle = OutcomeExitLifecycle("w", 1153, "#11530", "old-7", Decimal("13"), Decimal("0.76"), 0, "LOSS_BAND_UNFILLED")
    store.record(lifecycle, reason="loss_band")
    controller = OutcomeEmergencyExitController(account=account, gateway=gateway, store=store, wallet="w", policy=OutcomeEmergencyExitPolicy())
    plan = OutcomeEmergencyExitPolicy().plan(item())

    result = controller.execute(market=market(), side_index=0, lifecycle=lifecycle, item=item(), plan=plan)

    assert result.state == "emergency_exit_submitted"
    assert [name for name, _ in gateway.calls] == ["cancel", "book", "ioc"]
    assert gateway.calls[-1][1]["limit_price"] == Decimal("0.70450")
    recovered = store.recover(wallet="w", outcome_id=1153, coin="#11530")
    assert recovered is not None and recovered.state == "EMERGENCY_EXIT_SUBMITTED"


def test_controller_never_iocs_before_cancel_confirmation(tmp_path):
    account, gateway = Account(), Gateway(None)
    gateway.account = account
    def no_cancel(**kwargs):
        gateway.calls.append(("cancel", kwargs)); return {"ok": True}
    gateway.cancel_owned_order = no_cancel
    store = OutcomeExitLifecycleStore(TradeJournalDB(tmp_path / "journal.db"), "run")
    lifecycle = OutcomeExitLifecycle("w", 1153, "#11530", "old-7", Decimal("13"), Decimal("0.76"), 0, "LOSS_BAND_UNFILLED")
    store.record(lifecycle, reason="loss_band")
    controller = OutcomeEmergencyExitController(account=account, gateway=gateway, store=store, wallet="w", policy=OutcomeEmergencyExitPolicy())
    result = controller.execute(market=market(), side_index=0, lifecycle=lifecycle, item=item(), plan=OutcomeEmergencyExitPolicy().plan(item()))

    assert result.detail == "emergency_old_sell_still_open"
    assert [name for name, _ in gateway.calls] == ["cancel"]
