from datetime import datetime, timedelta, timezone

from bot.outcome_exit_decision_replay_report import (
    _control_label,
    _recovery_probe_case,
    _trend_efficiency,
    report,
)
from monitoring.trade_journal_db import TradeJournalDB


def test_replay_joins_existing_hard_exit_reentry_and_monitor_facts(tmp_path):
    journal = TradeJournalDB(tmp_path / "replay.db")
    for velocity, depth, spread in ((10, 100, 500), (-20, 60, 400), (20, 100, 300)):
        journal.log_strategy_event("run", "OUTCOME_MARKET_RISK_MONITOR_SHADOW", {
            "outcome_id": 1, "period": "1d", "coin": "#10",
            "state": "RISK_COMPRESSION_SHADOW", "reversal_state": "WEAKENING",
            "bid_velocity_bps": {"30": velocity}, "top3_depth": depth, "spread_bps": spread,
        })
    journal.log_order_event("run", "ORDER_FILLED", venue_order_id="buy", side="BUY", price=0.80, qty=10,
                            status="FILLED", instrument_id="#10", payload={"venue": "hyperliquid_outcome"})
    journal.log_order_event("run", "ORDER_SUBMIT", venue_order_id="ioc", side="SELL", status="IOC_SUBMITTED",
                            instrument_id="#10", payload={"venue": "hyperliquid_outcome", "outcome_id": 1,
                                                            "execution_type": "narrow_hard_failure_price_protected_fak_ioc",
                                                            "planned_net_return_pct": "-0.12"})
    journal.log_order_event("run", "ORDER_FILLED", venue_order_id="ioc", side="SELL", price=0.70, qty=10,
                            status="FILLED", instrument_id="#10", payload={"venue": "hyperliquid_outcome"})
    journal.log_order_event("run", "ORDER_FILLED", venue_order_id="rebuy", side="BUY", price=0.73, qty=10,
                            status="FILLED", instrument_id="#10", payload={"venue": "hyperliquid_outcome"})

    output = report(journal.db_path)

    assert output["live_authority"] is False
    assert output["episode_count"] == 1
    episode = output["episodes"][0]
    assert episode["entry_price"] == 0.80
    assert episode["exit_fill_price"] == 0.70
    assert episode["first_reentry"]["fill_price"] == 0.73
    assert episode["pre_exit_monitor"]["replay_label"] == "chop_depth_recovery_candidate"


def test_control_label_requires_joint_chop_evidence_not_a_single_bid_flip():
    assert _trend_efficiency([1.0, 0.9, 1.0]) == 0.0
    assert _control_label(
        bids=[1.0, 0.9, 1.0], depths=[100, 50, 100], spreads=[500, 400, 300], thesis_state="intact",
    ) == "chop_recovery_candidate"
    assert _control_label(
        bids=[1.0, 0.9, 0.8], depths=[100, 50, 55], spreads=[500, 400, 450], thesis_state="intact",
    ) == "persistent_or_unresolved_deterioration"


def _risk_row(*, net: str, hard: bool, hold: bool, efficiency: str, flips: int,
              depth: str, spread: str) -> dict:
    return {
        "full_depth_net_return_pct": net,
        "recovery_shape": {
            "classification": "chop_recovery_candidate" if hold else "persistent_or_unresolved_deterioration",
            "bid_trend_efficiency": efficiency,
            "bid_direction_flips": flips,
            "depth_recovery_ratio": depth,
            "spread_contraction_ratio": spread,
        },
        "counterfactual_actions": {
            "hard_ioc": {"eligible": hard},
            "hold_for_recovery": {"eligible": hold},
        },
    }


def test_recovery_probe_waits_only_when_initial_recovery_is_already_building():
    base = datetime(2026, 9, 22, tzinfo=timezone.utc)
    rows = [
        # Matches the important distinction: early flip/depth/spread recovery
        # is visible, while efficiency is still too high for full HOLD.
        (base, _risk_row(net="-0.101", hard=True, hold=False, efficiency="0.88", flips=2,
                         depth="2.0", spread="0.67")),
        (base + timedelta(seconds=63), _risk_row(net="-0.129", hard=False, hold=True, efficiency="0.34", flips=5,
                                                  depth="6.1", spread="0.37")),
    ]
    result = _recovery_probe_case("official_buy:1:2", rows, final_pnl=0.22)

    assert result is not None
    assert result["recovery_building_at_initial_hard"] is True
    assert result["proposed_60s_action"] == "HOLD_FOR_RECOVERY_AFTER_60S_COUNTERFACTUAL"
    assert result["checkpoints"]["60"]["delay_sec"] == 63.0
    assert result["final_label"] == "profit"


def test_recovery_probe_keeps_immediate_ioc_for_one_way_tail_shape():
    base = datetime(2026, 9, 22, tzinfo=timezone.utc)
    rows = [
        (base, _risk_row(net="-0.12", hard=True, hold=False, efficiency="1", flips=0,
                         depth="1.7", spread="0.84")),
    ]
    result = _recovery_probe_case("official_buy:1:2", rows, final_pnl=-1.18)

    assert result is not None
    assert result["recovery_building_at_initial_hard"] is False
    assert result["proposed_60s_action"] == "IMMEDIATE_HARD_IOC_COUNTERFACTUAL"
    assert result["final_label"] == "loss"
