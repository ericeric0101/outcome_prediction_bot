from decimal import Decimal

from bot.outcome_portfolio_guard import OutcomePortfolioGuard
from monitoring.trade_journal_db import TradeJournalDB


def test_guard_is_dormant_until_both_existing_notional_limits_reach_20(tmp_path):
    decision = OutcomePortfolioGuard(str(tmp_path / "missing.db")).evaluate(
        outcome_id=1, prospective_notional=Decimal("11"),
        phase_entry_cap=Decimal("11"), phase_exposure_cap=Decimal("11"),
    )
    assert decision.allowed and not decision.enabled


def test_20_canary_guard_blocks_daily_gross_cap(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    journal.log_order_event("run", "ORDER_FILLED", side="BUY", price=19, qty=10, payload={
        "venue": "hyperliquid_outcome", "actual_fill": True,
    })
    decision = OutcomePortfolioGuard(journal.db_path).evaluate(
        outcome_id=1, prospective_notional=Decimal("20"),
        phase_entry_cap=Decimal("20"), phase_exposure_cap=Decimal("20"),
    )
    assert decision.enabled and not decision.allowed
    assert decision.reason == "portfolio_daily_gross_entry_cap"
    assert decision.daily_gross_entry_limit_usdc == Decimal("200")
    assert decision.daily_realized_loss_limit_usdc == Decimal("-8")


def test_guard_limits_scale_with_the_existing_entry_notional_cap(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    decision = OutcomePortfolioGuard(journal.db_path).evaluate(
        outcome_id=1, prospective_notional=Decimal("50"),
        phase_entry_cap=Decimal("50"), phase_exposure_cap=Decimal("50"),
    )
    assert decision.allowed and decision.enabled
    assert decision.daily_gross_entry_limit_usdc == Decimal("500")
    assert decision.daily_realized_loss_limit_usdc == Decimal("-20")


def test_20_canary_guard_stops_market_only_after_third_confirmed_loss(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    for index in range(3):
        journal.record_outcome_realized_pnl_lot(
            close_trade_id=f"close-{index}", open_trade_id=f"open-{index}", outcome_id=7,
            side_index=0, close_kind="sell", quantity=Decimal("1"), cost_usdc=Decimal("2"),
            proceeds_usdc=Decimal("1"), source={},
        )
    decision = OutcomePortfolioGuard(journal.db_path).evaluate(
        outcome_id=7, prospective_notional=Decimal("20"),
        phase_entry_cap=Decimal("20"), phase_exposure_cap=Decimal("20"),
    )
    assert not decision.allowed and decision.reason == "portfolio_daily_market_loss_stop"
