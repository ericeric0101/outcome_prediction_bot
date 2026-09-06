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
    assert thin.allowed and thin.safe_max_shares == Decimal("4")


def test_bootstrap_gate_sets_a_bounded_submit_price_drift_ceiling(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    decision = OutcomeTierBExecutionGate(journal.db_path).evaluate(
        bid=Decimal("0.80000"), ask=Decimal("0.80200"),
        bid_levels=[{"size": "10"}, {"size": "10"}], requested_shares=Decimal("13"),
    )
    assert decision.allowed
    assert decision.policy.source.startswith("bootstrap")
    assert decision.max_submit_bid == Decimal("0.80200")


def test_capacity_is_partial_not_an_instruction_to_submit_the_full_desired_size(tmp_path):
    decision = OutcomeTierBExecutionGate(str(tmp_path / "journal.db")).evaluate(
        bid=Decimal("0.50"), ask=Decimal("0.501"),
        bid_levels=[{"size": "20"}, {"size": "18"}], requested_shares=Decimal("40"),
    )
    assert decision.allowed
    # 38 visible shares / 1.25 safety multiple = 30 whole safe shares.
    assert decision.safe_max_shares == Decimal("30")


def test_recent_public_trade_flow_can_only_tighten_l2_capacity(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    journal.log_strategy_event("run", "OUTCOME_WS_TRADES", {"raw": {"data": [
        {"coin": "#yes", "tid": "one", "sz": "80"},
        {"coin": "#yes", "tid": "two", "sz": "40"},
        {"coin": "#yes", "tid": "one", "sz": "80"},
    ]}})
    decision = OutcomeTierBExecutionGate(journal.db_path).evaluate(
        bid=Decimal("0.50"), ask=Decimal("0.501"), bid_levels=[{"size": "100"}],
        requested_shares=Decimal("40"), coin="#yes",
    )
    assert decision.recent_trade_shares == Decimal("120")
    assert decision.safe_max_shares == Decimal("30")
