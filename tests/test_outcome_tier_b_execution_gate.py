from decimal import Decimal

from bot.outcome_tier_b_execution_gate import OutcomeTierBExecutionGate
from monitoring.trade_journal_db import TradeJournalDB


def test_bootstrap_gate_rejects_thin_or_wide_tier_b_book(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    gate = OutcomeTierBExecutionGate(journal.db_path)
    wide = gate.evaluate(
        bid=Decimal("0.70"), ask=Decimal("0.72"),
        bid_levels=[{"size": "100"}], requested_shares=Decimal("13"),
    )
    assert not wide.allowed and wide.reason == "tier_b_spread_exceeds_calibrated_ceiling"
    thin = gate.evaluate(
        bid=Decimal("0.70"), ask=Decimal("0.701"),
        bid_levels=[{"size": "2"}, {"size": "3"}], requested_shares=Decimal("13"),
    )
    assert not thin.allowed and thin.reason == "tier_b_top_depth_below_calibrated_floor"


def test_bootstrap_gate_sets_a_bounded_submit_price_drift_ceiling(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    decision = OutcomeTierBExecutionGate(journal.db_path).evaluate(
        bid=Decimal("0.80000"), ask=Decimal("0.80200"),
        bid_levels=[{"size": "10"}, {"size": "10"}], requested_shares=Decimal("13"),
    )
    assert decision.allowed
    assert decision.policy.source.startswith("bootstrap")
    assert decision.max_submit_bid == Decimal("0.80200")
