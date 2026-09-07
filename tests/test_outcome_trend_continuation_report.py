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


def test_terminal_bbo_observation_marks_two_hour_episode_complete_and_prevents_restart(tmp_path):
    db = TradeJournalDB(tmp_path / "strategy.db")
    recorder = OutcomeTrendContinuationRecorder(db, "trend")
    candidate = {"eligible": True, "side_index": 1, "episode_id": "1:terminal"}
    recorder.observe(outcome_id=1, period="1d", candidate=candidate, bbo_by_side={1: ("0.60", "0.61")})
    key, path = next(iter(recorder._paths.items()))
    path.started_at -= recorder.HORIZON_SEC + 1
    recorder.observe(outcome_id=1, period="1d", candidate=candidate, bbo_by_side={1: ("0.61", "0.62")})

    result = report(db.db_path)
    assert result["coverage"] == {"observed_2h": 1}
    assert result["episodes"][0]["terminal_observation"] is True
    assert result["episodes"][0]["observed_horizon_sec"] >= recorder.HORIZON_SEC
    assert recorder._paths == {}

    # The same continuous signal must not create a second record after the
    # original bounded counterfactual is closed.
    recorder.observe(outcome_id=1, period="1d", candidate=candidate, bbo_by_side={1: ("0.62", "0.63")})
    assert recorder._paths == {}
