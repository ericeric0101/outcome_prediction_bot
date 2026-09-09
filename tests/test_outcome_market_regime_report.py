from bot.outcome_market_regime_report import report
from monitoring.trade_journal_db import TradeJournalDB


def test_report_links_entry_only_to_prior_same_market_shadow_state(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    journal.log_strategy_event("run", "OUTCOME_MARKET_REGIME_SHADOW", {
        "outcome_id": 7, "period": "1d", "state": "TRANSITION", "reason": "multihorizon_direction_conflict",
        "read_only": True, "execution_submitted": False,
    })
    journal.log_strategy_event("run", "OUTCOME_LIVE_STRATEGY_ENTRY_PLACED", {
        "outcome_id": 7, "period": "1d", "coin": "#70", "entry_tier": "tier_b_spot_mark",
        "entry_reason": "down_spot_mark_confirmed",
    })
    journal.log_strategy_event("run", "OUTCOME_TOXIC_FILL_SHADOW", {
        "outcome_id": 7, "period": "1d", "coin": "#70", "state": "TOXIC_FILL",
        "reason": "toxic_fill_shadow_confirmed", "read_only": True, "execution_submitted": False,
    })
    result = report(journal.db_path)
    assert result["regime_state_counts"] == {"TRANSITION": 1}
    assert result["toxic_fill_state_counts"] == {"TOXIC_FILL": 1}
    assert result["entries"][0]["shadow_regime_at_or_before_entry"] == "TRANSITION"
