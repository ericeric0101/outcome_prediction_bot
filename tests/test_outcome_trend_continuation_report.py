from bot.outcome_trend_continuation import OutcomeTrendContinuationRecorder
from bot.outcome_trend_continuation_report import report
from monitoring.trade_journal_db import TradeJournalDB


def test_candidate_path_report_is_explicitly_counterfactual(tmp_path):
    db = TradeJournalDB(tmp_path / "strategy.db")
    recorder = OutcomeTrendContinuationRecorder(db, "trend")
    candidate = {"eligible": True, "side_index": 1, "episode_id": "1:100"}
    recorder.observe(outcome_id=1, period="1d", candidate=candidate, bbo_by_side={1: ("0.60", "0.61")})
    key, path = next(iter(recorder._paths.items()))
    path.started_at -= 31
    recorder._last_recorded_at[key] -= 31
    recorder.observe(outcome_id=1, period="1d", candidate=None, bbo_by_side={1: ("0.612", "0.62")})
    result = report(db.db_path)
    assert result["episode_count"] == 1
    assert result["episodes"][0]["hit_upside"]["1%"] is True
    assert result["ready_for_policy_promotion"] is False
    assert "maker-fill" in result["limits"][0]
