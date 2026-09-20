"""Regression coverage for the unified all-loss-exit operator switch."""
from decimal import Decimal
import sqlite3
from types import SimpleNamespace

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_exit_lifecycle import OutcomeExitLifecycle, OutcomeExitLifecycleStore
from bot.outcome_exit_quote_planner import OutcomeExitQuotePlanner
from bot.outcome_exit_requote_controller import OutcomeExitRequoteController
from bot.outcome_exit_requote_service import OutcomeExitRequoteService
from bot.outcome_p3_calibration import OutcomeP3CalibrationConfig
from bot.outcome_reversal import OutcomeReversalClassifier
from monitoring.trade_journal_db import TradeJournalDB


def _market():
    return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260920-1400", 1, 0, Decimal("1"), "1d", "")


class _Account:
    def __init__(self):
        self.orders = [{"oid": "loss-old", "coin": "#11530", "side": "A", "sz": "13"}]

    def get_open_orders_sync(self, _wallet):
        return list(self.orders)

    def get_spot_clearinghouse_state_sync(self, _wallet):
        return {"balances": [{"coin": "+11530", "total": "13"}]}


class _Gateway:
    def __init__(self, account):
        self.account = account
        self.calls = []

    def cancel_owned_order(self, **kwargs):
        self.calls.append("cancel")
        self.account.orders = [row for row in self.account.orders if str(row["oid"]) != str(kwargs["order_id"])]
        return {"ok": True}

    def fetch_order_book(self, **_kwargs):
        self.calls.append("book")
        return {"bids": [{"price": "0.70"}], "asks": [{"price": "0.71"}]}

    def place_alo(self, **kwargs):
        self.calls.append("place")
        self.account.orders.append({"oid": "tp-new", "coin": "#11530", "side": "A", "sz": str(kwargs["requested_shares"])})
        return {"orderId": "tp-new"}


class _Machine:
    @staticmethod
    def _fill_vwap_for_inventory(**_kwargs):
        return Decimal("0.80")


def test_loss_exit_disabled_migrates_verified_resting_loss_band_to_tp(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    store = OutcomeExitLifecycleStore(journal, "run")
    loss_lifecycle = OutcomeExitLifecycle(
        "wallet", 1153, "#11530", "loss-old", Decimal("13"), Decimal("0.76031"), 1, "LOSS_BAND_RESTING",
    )
    store.record(loss_lifecycle, reason="previous_loss_band")
    account = _Account()
    gateway = _Gateway(account)
    service = OutcomeExitRequoteService(
        recovery=SimpleNamespace(wallet="wallet"), machine=_Machine(), stream_health=lambda: None,
        store=store,
        controller=OutcomeExitRequoteController(account=account, gateway=gateway, store=store, wallet="wallet"),
        planner=OutcomeExitQuotePlanner(), holding_context={}, reversal_classifier=OutcomeReversalClassifier(),
        opposite_observation_counts={}, canary_eligible_order_ids=set(),
        fresh_book=lambda **_kwargs: {"bids": [{"price": "0.70"}], "asks": [{"price": "0.71"}]},
        top_of_book=lambda _book: (Decimal("0.70"), Decimal("0.71")),
        persisted_policy=lambda **_kwargs: OutcomeP3CalibrationConfig(),
        persisted_maker_fee=lambda **_kwargs: Decimal("0.0004"),
        strategy_exit_tier=lambda **_kwargs: None,
        enabled=lambda: True, canary_enabled=lambda: False, loss_exit_enabled=lambda: False,
    )

    result = service.maybe_requote(
        market=_market(), finding=SimpleNamespace(coin="#11530", inventory=Decimal("13"), sell_order_ids=("loss-old",)),
    )

    assert result.state == "sell_resting"
    assert gateway.calls == ["cancel", "book", "place"]
    recovered = store.recover(wallet="wallet", outcome_id=1153, coin="#11530")
    assert recovered is not None and recovered.order_id == "tp-new" and recovered.state == "SELL_RESTING"
    with sqlite3.connect(journal.db_path) as conn:
        events = conn.execute(
            "SELECT event_type FROM strategy_events WHERE event_type='OUTCOME_LOSS_BAND_DISABLE_MIGRATION'"
        ).fetchall()
    assert events == [("OUTCOME_LOSS_BAND_DISABLE_MIGRATION",)]
