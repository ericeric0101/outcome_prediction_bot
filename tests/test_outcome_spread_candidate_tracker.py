from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_spread_candidate_tracker import OutcomeWideSpreadCandidateTracker
from bot.outcome_spread_quality_report import report
from monitoring.trade_journal_db import TradeJournalDB


def _market():
    return OutcomeMarketSpec(1, "@1", "#10", "#11", 10, 11, "priceBinary", "BTC", "x", 2_000_000, 1, Decimal("70000"), "1d", "raw")


def test_wide_spread_candidate_is_sampled_and_gets_forward_bbo_paths(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    tracker = OutcomeWideSpreadCandidateTracker(journal=journal, run_id="run")
    market = _market()
    assert tracker.observe_rejection(
        market=market, coin="#10", observed_at_ms=1_000_000, bid=Decimal("0.60"), ask=Decimal("0.62"),
        spread_bps=Decimal("328"), entry_tier="tier_a_spot_mark_oi", time_left_sec=1000,
        regime={"state": "TREND", "reason": "confirmed"}, requested_shares=30,
        safe_max_shares=Decimal("100"), top3_depth_shares=Decimal("120"), recent_trade_shares_5m=Decimal("80"),
    )
    assert not tracker.observe_rejection(
        market=market, coin="#10", observed_at_ms=1_001_000, bid=Decimal("0.60"), ask=Decimal("0.62"),
        spread_bps=Decimal("328"), entry_tier="tier_a_spot_mark_oi", time_left_sec=999,
        regime={}, requested_shares=30, safe_max_shares=Decimal("100"), top3_depth_shares=Decimal("120"), recent_trade_shares_5m=Decimal("80"),
    )
    assert tracker.observe_book(market=market, coin="#10", observed_at_ms=1_300_000, best_bid=Decimal("0.61"), best_ask=Decimal("0.615")) == 1
    assert tracker.observe_book(market=market, coin="#10", observed_at_ms=1_900_000, best_bid=Decimal("0.63"), best_ask=Decimal("0.635")) == 1
    assert tracker.observe_book(market=market, coin="#10", observed_at_ms=2_800_000, best_bid=Decimal("0.64"), best_ask=Decimal("0.645")) == 1
    result = report(journal.db_path)
    assert result["wide_spread_candidate_by_bucket"] == [{
        "spread_bucket": "250_plus", "sampled_candidates": 1, "path_5m": 1, "path_15m": 1, "path_30m": 1,
    }]
    assert set(result["wide_spread_candidates"][0]["forward_bbo"]) == {"300", "900", "1800"}
