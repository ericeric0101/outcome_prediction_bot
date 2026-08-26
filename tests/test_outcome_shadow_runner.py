import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.outcome_shadow_runner import OutcomeShadowRunner
from bot.position_manager import PositionManager, PositionManagerConfig
from execution.exit_policy import ExitPolicy, ExitPolicyConfig
from monitoring.trade_journal_db import TradeJournalDB


class ReadOnlyClient:
    def __init__(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=12)
        self.description = (
            "class:priceBinary|underlying:BTC|expiry:"
            f"{expiry.strftime('%Y%m%d-%H%M')}|targetPrice:70000|period:1d"
        )

    def get_outcome_meta_sync(self, **_):
        return {"outcomes": [{"outcome": 516, "description": self.description}]}

    def get_spot_clearinghouse_state_sync(self, _):
        return {"balances": [{"coin": "+5160", "total": "25", "hold": "5", "entryNtl": "10"}]}

    def get_open_orders_sync(self, _):
        return []

    def get_user_fills_sync(self, _):
        return []

    def get_all_mids_sync(self, **_):
        return {"BTC": "71000"}

    def get_l2_book_sync(self, coin, **_):
        assert coin in {"#5160", "#5161"}
        return {"levels": [[{"px": "0.60", "sz": "25"}], [{"px": "0.61", "sz": "25"}]]}

    def post_exchange_sync(self, *_args, **_kwargs):
        raise AssertionError("shadow runtime must never submit /exchange")


def _runner(db):
    exit_engine = ExitPolicyEngine(ExitEngineConfig(
        hold_to_redeem_enabled=True, min_hold_sec=0, stop_loss_usdc=Decimal("0.5"),
        stop_loss_confirmations=2, stop_loss_requires_thesis_weakening=True,
        stop_loss_thesis_min_score_abs=Decimal("0.18"), stop_loss_hold_on_none_signal=True,
        conviction_band_min_price=Decimal("0.6"), hold_band_min_price=Decimal("0.68"),
        conviction_band_min_score_abs=Decimal("0.15"), hold_band_min_score_abs=Decimal("0.15"),
        hold_band_release_min_roi=Decimal("0.15"), conviction_stop_loss_multiplier=Decimal("1.75"),
        conviction_extra_confirmations=1, hold_band_requires_locked=True,
    ))
    manager = PositionManager(PositionManagerConfig(
        early_profit_hold_enabled=True, early_profit_hold_min_hold_sec=60,
        early_profit_hold_max_profit_ps=Decimal("0.08"), early_profit_hold_min_score_abs=Decimal("0.2"),
        profit_run_enabled=True, profit_run_min_hold_sec=60, profit_run_min_profit_ps=Decimal("0.04"),
        profit_run_min_score_abs=Decimal("0.2"), profit_run_trailing_drawdown_ps=Decimal("0.05"),
        profit_run_unlock_profit_ps=Decimal("0.18"), profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
        stop_loss_entry_protection_sec=30, continuation_entry_protection_sec=60,
        stop_loss_regime_min_sec=8, stop_loss_regime_confirmations=2,
        stop_loss_min_opposite_score_abs=Decimal("0.2"),
    ))
    return OutcomeShadowRunner(
        client=ReadOnlyClient(), wallet_address="0x" + "a" * 40, journal=TradeJournalDB(db),
        exit_policy=ExitPolicy(ExitPolicyConfig(aggressive_stage_sec=180, taker_stage_sec=75)),
        position_manager=manager, exit_engine=exit_engine,
    )


def test_shadow_cycle_writes_risk_telemetry_without_any_exchange_path(tmp_path):
    db = tmp_path / "shadow.db"
    result = _runner(db).cycle()
    assert result.market is not None
    assert result.account_balance_count == 1
    assert result.risk_decision_count == 2
    with sqlite3.connect(db) as conn:
        event_type, payload = conn.execute(
            "SELECT event_type, payload_json FROM strategy_events WHERE event_type='OUTCOME_SHADOW_CYCLE'"
        ).fetchone()
    assert event_type == "OUTCOME_SHADOW_CYCLE"
    assert '"read_only": true' in payload
    assert '"sellable_qty": 20.0' in payload
    telemetry = json.loads(payload)
    assert len(telemetry["market_snapshots"]) == 2
    assert telemetry["market_snapshots"][0]["best_bid"] == 0.6
    assert telemetry["market_snapshots"][0]["fair"] is not None
    assert telemetry["strategy_telemetry"]["forecast"]["reference_source"] == "hyperliquid_btc_mark"
    assert telemetry["strategy_telemetry"]["execution_blocked"] is True


def test_shadow_cycle_logs_transient_error_and_allows_the_next_cycle(tmp_path):
    runner = _runner(tmp_path / "shadow.db")
    calls = {"count": 0}
    original = runner.client.get_outcome_meta_sync

    def flaky_meta(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("HTTP 502")
        return original(**kwargs)

    runner.client.get_outcome_meta_sync = flaky_meta
    failed = runner.cycle()
    recovered = runner.cycle()
    assert failed.error == "RuntimeError: HTTP 502"
    assert recovered.error is None
    assert recovered.market is not None
