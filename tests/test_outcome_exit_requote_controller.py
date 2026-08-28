from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_exit_lifecycle import OutcomeExitLifecycle, OutcomeExitLifecycleStore
from bot.outcome_exit_quote_planner import ExitQuoteAction, ExitQuotePlan
from bot.outcome_exit_requote_controller import OutcomeExitRequoteController
from monitoring.trade_journal_db import TradeJournalDB


def market(): return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "1d", "")


class Account:
    def __init__(self, orders=None, total="13"): self.orders, self.total = orders or [], total
    def get_open_orders_sync(self, _): return list(self.orders)
    def get_spot_clearinghouse_state_sync(self, _): return {"balances": [{"coin": "+11530", "total": self.total}]}


class Gateway:
    def __init__(self, account, *, cancel_removes=True, book=None, place_raises=False):
        self.account, self.cancel_removes, self.book, self.place_raises = account, cancel_removes, book or {"bids": [{"price": "0.70"}], "asks": [{"price": "0.71"}]}, place_raises
        self.calls = []
    def cancel_owned_order(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        if self.cancel_removes: self.account.orders = [o for o in self.account.orders if str(o["oid"]) != str(kwargs["order_id"])]
        return {"ok": True}
    def fetch_order_book(self, **kwargs): self.calls.append(("book", kwargs)); return self.book
    def place_alo(self, **kwargs):
        self.calls.append(("place", kwargs))
        if self.place_raises: raise RuntimeError("rejected")
        return {"orderId": "new-9"}


def _setup(tmp_path, **gateway_kwargs):
    account = Account([{"oid": "old-7", "coin": "#11530", "side": "A", "sz": "13"}])
    store = OutcomeExitLifecycleStore(TradeJournalDB(tmp_path / "journal.db"), "run")
    lifecycle = OutcomeExitLifecycle("w", 1153, "#11530", "old-7", Decimal("13"), Decimal("0.85"), 0, "SELL_RESTING")
    store.record(lifecycle, reason="initial")
    gateway = Gateway(account, **gateway_kwargs)
    return OutcomeExitRequoteController(account=account, gateway=gateway, store=store, wallet="w"), lifecycle, gateway, store, account


def _plan(): return ExitQuotePlan(ExitQuoteAction.CANCEL_REPLACE, "loss_band_reprice", Decimal("0.76031"), Decimal("0.76031"), Decimal("13"), "loss_band")


def test_controller_cancels_confirms_rebooks_then_posts_alo(tmp_path):
    controller, lifecycle, gateway, store, _ = _setup(tmp_path)
    result = controller.execute(market=market(), side_index=0, lifecycle=lifecycle, plan=_plan())
    assert result.state == "sell_resting" and result.new_order_id == "new-9"
    assert [name for name, _ in gateway.calls] == ["cancel", "book", "place"]
    assert gateway.calls[-1][1]["price"] > Decimal("0.70")
    assert store.recover(wallet="w", outcome_id=1153, coin="#11530").order_id == "new-9"


def test_controller_never_places_before_cancel_confirmation(tmp_path):
    controller, lifecycle, gateway, _, _ = _setup(tmp_path, cancel_removes=False)
    result = controller.execute(market=market(), side_index=0, lifecycle=lifecycle, plan=_plan())
    assert result.state == "reconcile_required"
    assert [name for name, _ in gateway.calls] == ["cancel"]


def test_controller_blocks_partial_fill_race_and_replacement_reject(tmp_path):
    controller, lifecycle, gateway, _, account = _setup(tmp_path)
    original = gateway.cancel_owned_order
    def cancel(**kwargs):
        result = original(**kwargs); account.total = "12"; return result
    gateway.cancel_owned_order = cancel
    result = controller.execute(market=market(), side_index=0, lifecycle=lifecycle, plan=_plan())
    assert result.detail == "inventory_changed_during_cancel"
    assert [name for name, _ in gateway.calls] == ["cancel"]
    controller, lifecycle, gateway, _, _ = _setup(tmp_path / "second", place_raises=True)
    result = controller.execute(market=market(), side_index=0, lifecycle=lifecycle, plan=_plan())
    assert result.detail == "replacement_submission_failed"


def test_controller_fails_closed_on_unusable_rebook(tmp_path):
    controller, lifecycle, gateway, _, _ = _setup(tmp_path, book={"bids": [], "asks": []})
    result = controller.execute(market=market(), side_index=0, lifecycle=lifecycle, plan=_plan())
    assert result.detail == "book_unusable_after_cancel"
    assert [name for name, _ in gateway.calls] == ["cancel", "book"]
