from decimal import Decimal
from datetime import datetime, timezone

from bot.outcome_portfolio_guard import OutcomePortfolioGuard
from monitoring.trade_journal_db import TradeJournalDB


def test_guard_is_dormant_until_both_existing_notional_limits_reach_20(tmp_path):
    decision = OutcomePortfolioGuard(str(tmp_path / "missing.db")).evaluate(
        outcome_id=1, prospective_notional=Decimal("11"),
        phase_entry_cap=Decimal("11"), phase_exposure_cap=Decimal("11"),
    )
    assert decision.allowed and not decision.enabled


def test_20_canary_guard_blocks_market_session_gross_cap(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    journal.log_order_event("run", "ORDER_FILLED", side="BUY", price=19, qty=10, payload={
        "venue": "hyperliquid_outcome", "actual_fill": True,
    })
    decision = OutcomePortfolioGuard(journal.db_path).evaluate(
        outcome_id=1, prospective_notional=Decimal("20"),
        phase_entry_cap=Decimal("20"), phase_exposure_cap=Decimal("20"),
    )
    assert decision.enabled and not decision.allowed
    assert decision.reason == "portfolio_market_session_gross_entry_cap"
    assert decision.market_session_gross_entry_limit_usdc == Decimal("200")
    assert decision.market_session_realized_loss_limit_usdc == Decimal("-8")


def test_guard_limits_scale_with_the_existing_entry_notional_cap(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    decision = OutcomePortfolioGuard(journal.db_path).evaluate(
        outcome_id=1, prospective_notional=Decimal("50"),
        phase_entry_cap=Decimal("50"), phase_exposure_cap=Decimal("50"),
    )
    assert decision.allowed and decision.enabled
    assert decision.market_session_gross_entry_limit_usdc == Decimal("500")
    assert decision.market_session_realized_loss_limit_usdc == Decimal("-20")


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
    assert not decision.allowed and decision.reason == "portfolio_market_session_market_loss_stop"


def test_rollover_settlement_grace_does_not_charge_new_market_session(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    with journal._connect() as conn:
        conn.execute(
            """INSERT INTO outcome_realized_pnl_lots
               (close_trade_id, open_trade_id, outcome_id, side_index, close_kind, quantity, cost_usdc, proceeds_usdc, realized_net_usdc, source_json, recorded_at)
               VALUES ('settlement-old', 'open-old', 1993, 1, 'settlement', '27', '19.5', '0', '-19.5', '{}', '2026-09-09T06:00:20+00:00')"""
        )
        conn.commit()
    decision = OutcomePortfolioGuard(journal.db_path).evaluate(
        outcome_id=2086, prospective_notional=Decimal("20"),
        phase_entry_cap=Decimal("20"), phase_exposure_cap=Decimal("20"),
        now=datetime(2026, 9, 9, 7, 0, tzinfo=timezone.utc),
    )
    assert decision.allowed
    assert decision.market_session_realized_net_usdc == Decimal("0")
    assert decision.rolling_24h_realized_net_usdc == Decimal("-19.5")
    assert decision.rolling_24h_realized_loss_limit_usdc == Decimal("-40")


def test_wider_rolling_account_backstop_still_blocks_two_full_loss_tails(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    with journal._connect() as conn:
        for index, value in enumerate(("-19.5", "-21")):
            conn.execute(
                """INSERT INTO outcome_realized_pnl_lots
                   (close_trade_id, open_trade_id, outcome_id, side_index, close_kind, quantity, cost_usdc, proceeds_usdc, realized_net_usdc, source_json, recorded_at)
                   VALUES (?, ?, ?, 1, 'settlement', '1', '1', '0', ?, '{}', '2026-09-09T01:00:00+00:00')""",
                (f"close-{index}", f"open-{index}", 100 + index, value),
            )
        conn.commit()
    decision = OutcomePortfolioGuard(journal.db_path).evaluate(
        outcome_id=2086, prospective_notional=Decimal("20"),
        phase_entry_cap=Decimal("20"), phase_exposure_cap=Decimal("20"),
        now=datetime(2026, 9, 9, 7, 0, tzinfo=timezone.utc),
    )
    assert not decision.allowed
    assert decision.reason == "portfolio_rolling_24h_realized_loss_cap"
