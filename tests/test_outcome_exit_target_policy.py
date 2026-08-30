from decimal import Decimal

from bot.outcome_exit_target_policy import OutcomeExitTargetPolicy
from monitoring.trade_journal_db import TradeJournalDB


def _snapshot(db, *, timestamp, bid, ask):
    book = {"levels": [[{"px": str(bid), "sz": "10"}], [{"px": str(ask), "sz": "10"}]]}
    db.log_strategy_event("run", "OUTCOME_P2_PARITY_SNAPSHOT", {
        "outcome_id": 7, "snapshot_timestamp_ms": timestamp,
        "capture_quality": {"status": "accepted"}, "yes_l2": book, "no_l2": book,
    })


def test_target_policy_clamps_rolling_executable_move_to_one_percent_floor(tmp_path):
    db = TradeJournalDB(tmp_path / "policy.db")
    # 13 independent five-minute moves of 0.2%; the net target may not fall
    # below 1%, regardless of an apparently quiet tape.
    for i in range(14):
        midpoint = Decimal("0.60") * (Decimal("1.002") ** i)
        _snapshot(db, timestamp=i * 300_000, bid=midpoint - Decimal("0.0001"), ask=midpoint + Decimal("0.0001"))
    decision = OutcomeExitTargetPolicy(db.db_path).decide(outcome_id=7, side_index=0)
    assert decision.target_return_pct == Decimal("0.01")
    assert decision.sample_count >= 12


def test_target_policy_uses_three_percent_fallback_without_sufficient_tape(tmp_path):
    db = TradeJournalDB(tmp_path / "policy.db")
    _snapshot(db, timestamp=1, bid="0.6", ask="0.61")
    decision = OutcomeExitTargetPolicy(db.db_path).decide(outcome_id=7, side_index=0)
    assert decision.target_return_pct == Decimal("0.03")
    assert decision.source == "fallback_insufficient_l2_returns"
