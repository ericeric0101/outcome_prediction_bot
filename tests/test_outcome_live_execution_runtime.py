from decimal import Decimal
import time

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_live_execution_runtime import OutcomeLiveExecutionRuntime
from bot.outcome_stream_health import OutcomeStreamHealth
from bot.outcome_execution_ledger import OutcomeExecutionLedger
from bot.outcome_exit_lifecycle import OutcomeExitLifecycle, OutcomeExitLifecycleStore
from bot.outcome_exit_quote_planner import OutcomeExitQuotePlanner, OutcomeExitQuotePlannerConfig
from bot.outcome_exit_requote_controller import ExitRequoteResult
from monitoring.trade_journal_db import TradeJournalDB


def market(): return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "15m", "")


class Account:
    def __init__(self, balances=None, orders=None): self.balances, self.orders = balances or [{"coin": "USDH", "total": "13", "hold": "0"}], orders or []
    def get_spot_clearinghouse_state_sync(self, _): return {"balances": self.balances}
    def get_open_orders_sync(self, _): return self.orders
    def get_user_fills_sync(self, _): return []


class CalibrationAccount(Account):
    def get_user_fees_sync(self, _): return {"userSpotAddRate": "0.0004", "userSpotCrossRate": "0.0007"}


class Gateway:
    def outcome_coin(self, _, i): return "#11530" if i == 0 else "#11531"
    def fetch_order_book(self, **_): return {"bids": [{"price": "0.77"}], "asks": [{"price": "0.78"}]}
    def place_alo(self, **_): return {"orderId": "1"}
    def cancel_owned_order(self, **_): return {}


def healthy_stream():
    health = OutcomeStreamHealth()
    health.configure_market(market()); health.on_lifecycle("connected"); health.mark_rest_resynced()
    health.on_l2_book("#11530"); health.on_l2_book("#11531")
    return health


def test_runtime_is_disabled_without_both_operator_gates(monkeypatch):
    monkeypatch.delenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("OUTCOME_SDK_EXECUTION_ENABLED", raising=False)
    runtime = OutcomeLiveExecutionRuntime(account=Account(), wallet="w", gateway=Gateway())
    assert runtime.tick(market=market(), side_index=0, entry_permitted=True).state == "disabled"


def test_runtime_blocks_cross_side_existing_exposure(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(account=Account(orders=[{"coin": "#11531", "side": "B", "oid": 7, "sz": "13"}]), wallet="w", gateway=Gateway(), stream_health=healthy_stream())
    assert runtime.tick(market=market(), side_index=0, entry_permitted=True).state == "blocked"


def test_runtime_reduce_only_cancels_owned_buys(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    calls = []
    class CancelGateway(Gateway):
        def cancel_owned_order(self, **kwargs): calls.append(kwargs); return {}
    runtime = OutcomeLiveExecutionRuntime(account=Account(orders=[{"coin": "#11530", "side": "B", "oid": 7, "sz": "13"}]), wallet="w", gateway=CancelGateway(), stream_health=healthy_stream())
    result = runtime.cancel_resting_buys(market=market())
    assert result.state == "cancelled"
    assert calls[0]["order_id"] == "7"


def test_tick_market_advances_existing_buy_without_needing_signal(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(account=Account(orders=[{"coin": "#11530", "side": "B", "oid": 7, "sz": "13"}]), wallet="w", gateway=Gateway(), stream_health=healthy_stream())
    assert runtime.tick_market(market=market(), entry_side_index=None).state == "buy_resting"


def test_tick_market_blocks_generic_entry_before_order_submission(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(account=Account(balances=[{"coin": "USDH", "total": "1", "hold": "0"}]), wallet="w", gateway=Gateway(), stream_health=healthy_stream())
    assert "no explicit verified exit policy" in runtime.tick_market(market=market(), entry_side_index=0).detail


def test_tick_market_refuses_generic_entry_without_exit_policy(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(account=Account(), wallet="w", gateway=Gateway(), stream_health=healthy_stream())
    result = runtime.tick_market(market=market(), entry_side_index=0)
    assert result.state == "blocked"
    assert "no explicit verified exit policy" in result.detail


def test_runtime_blocks_generic_entry_without_exit_policy_even_without_ws_health(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(account=Account(), wallet="w", gateway=Gateway())
    assert "no explicit verified exit policy" in runtime.tick_market(market=market(), entry_side_index=0).detail


def test_runtime_can_cancel_existing_entry_when_ws_is_stale(monkeypatch):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(account=Account(orders=[{"coin": "#11530", "side": "B", "oid": 7, "sz": "13"}]), wallet="w", gateway=Gateway())
    assert runtime.cancel_resting_buys(market=market()).state == "cancelled"


def test_p3_calibration_requires_its_own_explicit_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    runtime = OutcomeLiveExecutionRuntime(
        account=CalibrationAccount(), wallet="w", gateway=Gateway(), stream_health=healthy_stream(),
        ledger=OutcomeExecutionLedger(TradeJournalDB(tmp_path / "calibration.db"), "run"),
    )
    assert runtime.tick_p3_calibration(market=market()).state == "disabled"


def test_p3_calibration_places_one_balanced_post_only_entry_and_logs_it(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_P3_CALIBRATION_ENABLED", "1")
    journal = TradeJournalDB(tmp_path / "calibration.db")
    class TrackingGateway(Gateway):
        def __init__(self): self.calls = []
        def place_alo(self, **kwargs):
            self.calls.append(kwargs)
            return {"orderId": "1"}
    gateway = TrackingGateway()
    runtime = OutcomeLiveExecutionRuntime(
        account=CalibrationAccount(), wallet="w", gateway=gateway, stream_health=healthy_stream(),
        ledger=OutcomeExecutionLedger(journal, "run"),
    )
    result = runtime.tick_p3_calibration(market=market())
    assert result.state == "buy_placed"
    assert gateway.calls[0]["is_buy"] is True


def test_s0_live_strategy_logs_explicit_oi_policy_entry(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_LIVE_STRATEGY_ENABLED", "1")
    journal = TradeJournalDB(tmp_path / "strategy.db")
    class TrackingGateway(Gateway):
        def __init__(self): self.calls = []
        def place_alo(self, **kwargs): self.calls.append(kwargs); return {"orderId": "strategy-buy"}
    gateway = TrackingGateway()
    runtime = OutcomeLiveExecutionRuntime(
        account=CalibrationAccount(), wallet="w", gateway=gateway, stream_health=healthy_stream(),
        ledger=OutcomeExecutionLedger(journal, "run"),
    )
    result = runtime.tick_live_strategy(market=market(), entry_side_index=1,
        entry_reason="down_spot_mark_oi_confirmed", entry_evidence={"oi_age_ms": 10})
    assert result.state == "buy_placed"
    assert gateway.calls[0]["is_buy"] is True
    import sqlite3
    with sqlite3.connect(journal.db_path) as conn:
        payload = conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED'").fetchone()[0]
    assert '"sampling_policy": "oi_spot_mark_confirmation"' in payload


def test_s0_live_strategy_blocks_selected_bid_in_50_to_55_no_trade_band(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_LIVE_STRATEGY_ENABLED", "1")
    journal = TradeJournalDB(tmp_path / "band.db")
    class CenterGateway(Gateway):
        def __init__(self): self.calls = []
        def fetch_order_book(self, **_): return {"bids": [{"price": "0.50"}], "asks": [{"price": "0.501"}]}
        def place_alo(self, **kwargs): self.calls.append(kwargs); return {"orderId": "must-not-place"}
    gateway = CenterGateway()
    runtime = OutcomeLiveExecutionRuntime(
        account=CalibrationAccount(), wallet="w", gateway=gateway, stream_health=healthy_stream(),
        ledger=OutcomeExecutionLedger(journal, "run"),
    )
    result = runtime.tick_live_strategy(market=market(), entry_side_index=0, entry_reason="confirmed", entry_evidence={})
    assert result.state == "flat"
    assert "no-trade band" in result.detail
    assert gateway.calls == []


def test_s0_live_strategy_blocks_new_market_while_known_retiring_market_has_resting_order(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_LIVE_STRATEGY_ENABLED", "1")
    retiring = OutcomeMarketSpec(1152, "@1152", "#11520", "#11521", 1, 2, "priceBinary", "BTC", "", 0, 1, Decimal("1"), "1d", "")
    runtime = OutcomeLiveExecutionRuntime(
        account=CalibrationAccount(orders=[{"coin": "#11520", "side": "B", "oid": 7, "sz": "13"}]),
        wallet="w", gateway=Gateway(), stream_health=healthy_stream(),
        ledger=OutcomeExecutionLedger(TradeJournalDB(tmp_path / "rollover.db"), "run"),
    )
    result = runtime.tick_live_strategy(
        market=market(), entry_side_index=0, entry_reason="confirmed", entry_evidence={},
        retiring_markets=(retiring,),
    )
    assert result.state == "blocked"
    assert "rollover pending" in result.detail


def test_s0_exit_tiers_are_anchored_to_entry_not_replacement(monkeypatch, tmp_path):
    journal = TradeJournalDB(tmp_path / "tiers.db")
    journal.log_strategy_event("run", "OUTCOME_LIVE_STRATEGY_ENTRY_PLACED", {
        "outcome_id": 1153, "coin": "#11530", "target_return_pct": "0.05", "loss_reprice_pct": "0",
        "maker_close_fee_rate": "0.0004", "narrow_after_sec": 10, "narrow_return_pct": "0.03",
        "floor_after_sec": 20, "floor_return_pct": "0.02",
    })
    runtime = OutcomeLiveExecutionRuntime(account=Account(), wallet="w", gateway=Gateway(), ledger=OutcomeExecutionLedger(journal, "run"))
    import bot.outcome_live_execution_runtime as runtime_module
    base = runtime_module.time.time()
    monkeypatch.setattr(runtime_module.time, "time", lambda: base + 11)
    assert runtime._strategy_exit_tier(market=market(), coin="#11530") == (Decimal("0.03"), None)
    monkeypatch.setattr(runtime_module.time, "time", lambda: base + 21)
    assert runtime._strategy_exit_tier(market=market(), coin="#11530") == (Decimal("0.02"), Decimal("0.05"))


def test_s0_initial_protective_sell_keeps_five_percent_target_with_e4_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_EXIT_REQUOTE_ENABLED", "1")
    journal = TradeJournalDB(tmp_path / "s0_initial_exit.db")
    journal.log_strategy_event("run", "OUTCOME_LIVE_STRATEGY_ENTRY_PLACED", {
        "outcome_id": 1153, "coin": "#11530", "target_return_pct": "0.05", "loss_reprice_pct": "0",
        "maker_close_fee_rate": "0.0004", "narrow_after_sec": 3600, "narrow_return_pct": "0.03",
        "floor_after_sec": 7200, "floor_return_pct": "0.02",
    })
    class FilledAccount(Account):
        def get_user_fills_sync(self, _):
            return [{"coin": "#11530", "side": "B", "px": "0.74868", "sz": "14", "time": 1}]
    class TrackingGateway(Gateway):
        def __init__(self): self.calls = []
        def fetch_order_book(self, **_): return {"bids": [{"price": "0.72"}], "asks": [{"price": "0.73"}]}
        def place_alo(self, **kwargs): self.calls.append(kwargs); return {"orderId": "sell-1"}
    gateway = TrackingGateway()
    runtime = OutcomeLiveExecutionRuntime(
        account=FilledAccount(balances=[{"coin": "+11530", "total": "14", "entryNtl": "10.48152"}]),
        wallet="w", gateway=gateway, stream_health=healthy_stream(),
        ledger=OutcomeExecutionLedger(journal, "run"),
    )
    finding = type("Finding", (), {"coin": "#11530"})()
    result = runtime._advance_persisted_p3_exit(market=market(), finding=finding)
    assert result is not None and result.state == "sell_placed"
    assert gateway.calls[0]["price"] == Decimal("0.74868") * Decimal("1.05") / Decimal("0.9996")


def test_generic_recovery_restores_persisted_p3_exit_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    journal = TradeJournalDB(tmp_path / "calibration.db")
    journal.log_strategy_event("run", "OUTCOME_P3_CALIBRATION_ENTRY_PLACED", {
        "outcome_id": 1153, "coin": "#11530", "target_return_pct": "0.05",
        "loss_reprice_pct": "0.05", "maker_close_fee_rate": "0.0004", "order_id": "buy-1",
    })
    class FilledAccount(Account):
        def get_user_fills_sync(self, _):
            return [{"coin": "#11530", "side": "B", "px": "0.67329", "sz": "15", "time": 1}]
    class TrackingGateway(Gateway):
        def __init__(self): self.calls = []
        def fetch_order_book(self, **_): return {"bids": [{"price": "0.67"}], "asks": [{"price": "0.68"}]}
        def place_alo(self, **kwargs): self.calls.append(kwargs); return {"orderId": "sell-1"}
    gateway = TrackingGateway()
    account = FilledAccount(balances=[{"coin": "+11530", "total": "15", "entryNtl": "10.09935"}])
    runtime = OutcomeLiveExecutionRuntime(
        account=account, wallet="w", gateway=gateway, stream_health=healthy_stream(),
        ledger=OutcomeExecutionLedger(journal, "restarted-run"),
    )
    result = runtime.tick_market(market=market(), entry_side_index=None)
    assert result.state == "sell_placed"
    assert gateway.calls[0]["is_buy"] is False
    assert gateway.calls[0]["price"] == Decimal("0.67329") * Decimal("1.05") / Decimal("0.9996")


def _managed_exit_runtime(tmp_path, monkeypatch, *, enabled: bool):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_EXIT_REQUOTE_ENABLED", "1" if enabled else "0")
    journal = TradeJournalDB(tmp_path / "requote.db")
    journal.log_strategy_event("run", "OUTCOME_P3_CALIBRATION_ENTRY_PLACED", {
        "outcome_id": 1153, "coin": "#11530", "target_return_pct": "0.05",
        "loss_reprice_pct": "0.05", "maker_close_fee_rate": "0.0004", "order_id": "buy-1",
    })
    ledger = OutcomeExecutionLedger(journal, "run")
    store = OutcomeExitLifecycleStore(journal, "run")
    lifecycle = OutcomeExitLifecycle("w", 1153, "#11530", "old-sell", Decimal("13"), Decimal("0.85"), 0, "SELL_RESTING")
    store.record(lifecycle, reason="fixture")
    class FilledAccount(Account):
        def get_user_fills_sync(self, _): return [{"coin": "#11530", "side": "B", "px": "0.80", "sz": "13", "time": 1}]
    class LossGateway(Gateway):
        def __init__(self): self.calls = []
        def fetch_order_book(self, **_): self.calls.append("book"); return {"bids": [{"price": "0.70"}], "asks": [{"price": "0.71"}]}
        def cancel_owned_order(self, **_): self.calls.append("cancel"); return {}
    class SpyController:
        def __init__(self): self.calls = 0; self.plans = []
        def execute(self, **kwargs):
            self.calls += 1; self.plans.append(kwargs["plan"])
            return ExitRequoteResult("sell_resting", "fake replacement", "old-sell", "new-sell")
    gateway, controller = LossGateway(), SpyController()
    runtime = OutcomeLiveExecutionRuntime(
        account=FilledAccount(balances=[{"coin": "+11530", "total": "13", "entryNtl": "10.4"}], orders=[{"coin": "#11530", "side": "A", "oid": "old-sell", "sz": "13"}]),
        wallet="w", gateway=gateway, ledger=ledger, exit_lifecycle_store=store, exit_requote_controller=controller,
        exit_planner=OutcomeExitQuotePlanner(OutcomeExitQuotePlannerConfig(min_requote_interval_sec=0)),
    )
    return runtime, controller, gateway, store


def test_e4_flag_off_never_calls_cancel_replace_controller(monkeypatch, tmp_path):
    runtime, controller, gateway, _ = _managed_exit_runtime(tmp_path, monkeypatch, enabled=False)
    result = runtime.tick_market(market=market(), entry_side_index=None)
    assert controller.calls == 0
    assert "inventory is protected" in result.detail
    # S2-0 captures the holding path from a fresh BBO before the existing
    # protective sell is advanced, then the normal state machine reads again.
    assert gateway.calls == ["book", "book"]


def test_e4_flag_on_requires_managed_lifecycle_then_calls_controller(monkeypatch, tmp_path):
    runtime, controller, _, _ = _managed_exit_runtime(tmp_path, monkeypatch, enabled=True)
    result = runtime.tick_market(market=market(), entry_side_index=None)
    assert controller.calls == 1
    assert result.order_id == "new-sell"


def test_e4_new_sell_is_written_as_durable_owned_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    journal = TradeJournalDB(tmp_path / "runtime.db")
    journal.log_strategy_event("run", "OUTCOME_P3_CALIBRATION_ENTRY_PLACED", {
        "outcome_id": 1153, "coin": "#11530", "target_return_pct": "0.05", "loss_reprice_pct": "0.05", "maker_close_fee_rate": "0.0004",
    })
    class FilledAccount(Account):
        def get_user_fills_sync(self, _): return [{"coin": "#11530", "side": "B", "px": "0.80", "sz": "13", "time": 1}]
    runtime = OutcomeLiveExecutionRuntime(account=FilledAccount(balances=[{"coin": "+11530", "total": "13", "entryNtl": "10.4"}]), wallet="w", gateway=Gateway(), ledger=OutcomeExecutionLedger(journal, "run"))
    assert runtime.tick_market(market=market(), entry_side_index=None).state == "sell_placed"
    restored = runtime.exit_lifecycle_store.recover(wallet="w", outcome_id=1153, coin="#11530")
    assert restored is not None and restored.order_id == "1"


def test_e4_does_not_block_first_protective_sell_for_newly_filled_inventory(monkeypatch, tmp_path):
    """E4/E5 can manage only an existing sell, never prevent creating one."""
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_EXIT_REQUOTE_ENABLED", "1")
    journal = TradeJournalDB(tmp_path / "first-sell.db")
    journal.log_strategy_event("run", "OUTCOME_P3_CALIBRATION_ENTRY_PLACED", {
        "outcome_id": 1153, "coin": "#11530", "target_return_pct": "0.05",
        "loss_reprice_pct": "0.05", "maker_close_fee_rate": "0.0004", "order_id": "buy-1",
    })
    class FilledAccount(Account):
        def get_user_fills_sync(self, _):
            return [{"coin": "#11530", "side": "B", "px": "0.80", "sz": "13", "time": 1}]
    class TrackingGateway(Gateway):
        def __init__(self): self.calls = []
        def place_alo(self, **kwargs): self.calls.append(kwargs); return {"orderId": "first-sell"}
    gateway = TrackingGateway()
    runtime = OutcomeLiveExecutionRuntime(
        account=FilledAccount(balances=[{"coin": "+11530", "total": "13", "entryNtl": "10.4"}]),
        wallet="w", gateway=gateway, ledger=OutcomeExecutionLedger(journal, "run"),
    )
    result = runtime.tick_market(market=market(), entry_side_index=None)
    assert result.state == "sell_placed"
    assert gateway.calls[0]["is_buy"] is False
    assert runtime.exit_lifecycle_store.recover(wallet="w", outcome_id=1153, coin="#11530").order_id == "first-sell"


def test_e5_canary_is_one_time_upward_replacement_only_after_explicit_second_gate(monkeypatch, tmp_path):
    runtime, controller, gateway, _ = _managed_exit_runtime(tmp_path, monkeypatch, enabled=True)
    # Without the E5 gate, a matching target simply remains resting.
    gateway.fetch_order_book = lambda **_: {"bids": [{"price": "0.84"}], "asks": [{"price": "0.85"}]}
    assert runtime.tick_market(market=market(), entry_side_index=None).state == "sell_resting"
    assert controller.calls == 0
    monkeypatch.setenv("OUTCOME_EXIT_REQUOTE_CANARY_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_EXIT_REQUOTE_CANARY_MIN_AGE_SEC", "1")
    runtime._e5_canary_eligible_order_ids.add("old-sell")
    original_time = time.time
    monkeypatch.setattr("bot.outcome_live_execution_runtime.time.time", lambda: original_time() + 2)
    result = runtime.tick_market(market=market(), entry_side_index=None)
    assert result.order_id == "new-sell" and controller.calls == 1
    plan = controller.plans[0]
    assert plan.reason == "e5_one_tick_upward_canary"
    assert plan.target_price == Decimal("0.85001")
    # The order id is removed from the in-memory eligibility set even if the
    # controller later reports a problem; a run never retries the canary.
    assert "old-sell" not in runtime._e5_canary_eligible_order_ids


def test_runtime_closes_stale_lifecycle_after_exchange_fill_reconciliation(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTCOME_AUTOMATED_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OUTCOME_SDK_EXECUTION_ENABLED", "1")
    journal = TradeJournalDB(tmp_path / "closed.db")
    ledger = OutcomeExecutionLedger(journal, "run")
    store = OutcomeExitLifecycleStore(journal, "run")
    store.record(OutcomeExitLifecycle("w", 1153, "#11530", "filled-sell", Decimal("13"), Decimal("0.85"), 0, "SELL_RESTING"), reason="fixture")
    runtime = OutcomeLiveExecutionRuntime(account=Account(), wallet="w", gateway=Gateway(), ledger=ledger, exit_lifecycle_store=store)
    runtime.tick_market(market=market(), entry_side_index=None)
    assert store.recover(wallet="w", outcome_id=1153, coin="#11530") is None
