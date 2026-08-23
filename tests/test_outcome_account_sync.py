from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import parse_outcome_market_spec
from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.models import ExitDecisionType, MarketSnapshot, SignalDecision
from bot.outcome_account_sync import OutcomeAccountSynchronizer
from bot.position_manager import PositionManager, PositionManagerConfig, PositionLifecycle


class ReadOnlyOutcomeClient:
    """Fixture client: exposes only account reads, never an exchange method."""

    def __init__(self) -> None:
        self.calls = []

    def get_spot_clearinghouse_state_sync(self, user):
        self.calls.append(("balances", user))
        return {
            "balances": [
                {"coin": "USDC", "total": "100", "hold": "0", "entryNtl": "0"},
                {"coin": "+5160", "total": "25", "hold": "5", "entryNtl": "10"},
                {"coin": "+5161", "total": "3", "hold": "0", "entryNtl": "2.4"},
            ]
        }

    def get_open_orders_sync(self, user):
        self.calls.append(("orders", user))
        return [
            {"coin": "#5160", "oid": 12, "cloid": "order-a", "side": "A", "limitPx": "0.44", "sz": "5"},
            {"coin": "BTC", "oid": 99, "side": "B", "limitPx": "1", "sz": "1"},
        ]

    def get_user_fills_sync(self, user):
        self.calls.append(("fills", user))
        return [
            {"coin": "#5160", "oid": 12, "tid": 1, "side": "B", "px": "0.4", "sz": "25", "fee": "0.01", "feeToken": "USDC", "time": 123, "crossed": False},
            {"coin": "#5160", "oid": 13, "tid": 2, "side": "B", "px": "1", "sz": "25", "fee": "0", "time": 124, "dir": "settlement"},
            {"coin": "BTC", "oid": 14, "tid": 3, "side": "B", "px": "1", "sz": "1", "fee": "0", "time": 125},
        ]


def _market():
    market = parse_outcome_market_spec(
        {"outcome": 516, "description": "class:priceBinary|underlying:BTC|expiry:20260823-1015|targetPrice:78213|period:15m"}
    )
    assert market is not None
    return market


def _position_manager() -> PositionManager:
    return PositionManager(PositionManagerConfig(
        early_profit_hold_enabled=True, early_profit_hold_min_hold_sec=60,
        early_profit_hold_max_profit_ps=Decimal("0.08"), early_profit_hold_min_score_abs=Decimal("0.2"),
        profit_run_enabled=True, profit_run_min_hold_sec=60, profit_run_min_profit_ps=Decimal("0.04"),
        profit_run_min_score_abs=Decimal("0.2"), profit_run_trailing_drawdown_ps=Decimal("0.05"),
        profit_run_unlock_profit_ps=Decimal("0.18"), profit_run_unlock_trailing_drawdown_ps=Decimal("0.02"),
        stop_loss_entry_protection_sec=30, continuation_entry_protection_sec=15,
        stop_loss_regime_min_sec=30, stop_loss_regime_confirmations=2,
        stop_loss_min_opposite_score_abs=Decimal("0.2"),
    ))


def test_account_sync_normalizes_outcome_balances_orders_and_fills_without_settlement_inference():
    client = ReadOnlyOutcomeClient()
    snapshot = OutcomeAccountSynchronizer(client, "0x" + "a" * 40).fetch_snapshot()

    up = snapshot.balance_for(516, 0)
    assert up is not None
    assert up.total_qty == Decimal("25")
    assert up.available_qty == Decimal("20")
    assert up.avg_entry_price == Decimal("0.4")
    assert len(snapshot.open_orders) == 1
    assert snapshot.open_orders[0].side == "SELL"
    assert len(snapshot.fills) == 1
    assert snapshot.fills[0].side == "BUY"
    assert len(snapshot.ignored_settlement_fills) == 1
    assert [name for name, _ in client.calls] == ["balances", "orders", "fills"]


def test_account_snapshot_feeds_existing_position_state_and_position_manager():
    snapshot = OutcomeAccountSynchronizer(ReadOnlyOutcomeClient(), "0x" + "b" * 40).fetch_snapshot()
    market = _market()
    position = snapshot.position_state_for(market, 0, hold_sec=75)
    assert position.instrument_id == "#5160"
    assert position.qty == Decimal("25")
    assert position.sellable_qty == Decimal("20")
    assert position.avg_entry_price == Decimal("0.4")

    manager = _position_manager()
    state = snapshot.sync_position_manager(manager, market, 0, opened_ts=1_000, now_ts=1_010)
    assert state.lifecycle == PositionLifecycle.ENTRY_PROTECTED
    assert state.thesis_side == "UP"

    # The same account-derived PositionState reaches the unchanged exit engine.
    engine = ExitPolicyEngine(ExitEngineConfig(
        hold_to_redeem_enabled=True, min_hold_sec=0, stop_loss_usdc=Decimal("0.5"),
        stop_loss_confirmations=2, stop_loss_requires_thesis_weakening=True,
        stop_loss_thesis_min_score_abs=Decimal("0.18"), stop_loss_hold_on_none_signal=True,
        conviction_band_min_price=Decimal("0.6"), hold_band_min_price=Decimal("0.68"),
        conviction_band_min_score_abs=Decimal("0.15"), hold_band_min_score_abs=Decimal("0.15"),
        hold_band_release_min_roi=Decimal("0.15"), conviction_stop_loss_multiplier=Decimal("1.75"),
        conviction_extra_confirmations=1, hold_band_requires_locked=True,
    ))
    market_snapshot = MarketSnapshot(
        instrument_id=position.instrument_id, phase="ACTIVE", time_left_sec=300, best_bid=Decimal("0.70"),
        best_ask=Decimal("0.71"), fee_rate=Decimal("0"), spread=Decimal("0.01"),
        spread_pct=Decimal("0.014"), slippage_buffer_pct=Decimal("0"), exit_stage="PASSIVE",
        in_reduce_only_tail=False, stop_loss_disabled_in_tail=False, fair=Decimal("0.72"),
        fair_edge_ps=Decimal("0.02"), spot_minus_strike_bps=Decimal("5"),
    )
    decision = engine.evaluate(market_snapshot, position, SignalDecision(
        active_side="UP", score=Decimal("0.45"), locked=True, reason="supported", matches_position=True,
    ))
    assert decision.decision_type == ExitDecisionType.HOLD_TO_REDEEM
