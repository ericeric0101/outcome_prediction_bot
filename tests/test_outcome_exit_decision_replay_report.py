from bot.outcome_exit_decision_replay_report import _control_label, _trend_efficiency, report
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
