from monitoring.trade_journal_db import TradeJournalDB
from bot.outcome_risk_episode import OutcomeRiskEpisodeStore


def test_recovery_closes_episode_and_new_crash_gets_fresh_budget(tmp_path):
    journal = TradeJournalDB(str(tmp_path / "journal.db"))
    store = OutcomeRiskEpisodeStore(journal, "run", max_attempts=2)
    first = store.open_or_resume(wallet="w", outcome_id=1, coin="#1", trigger_family="fast", severity="-.1")
    assert first is not None
    first = store.record_attempt(first, execution_state="reconcile_required")
    assert first is not None and first.attempts == 1
    assert store.close_recovered(wallet="w", outcome_id=1, coin="#1", reason="recovered")
    second = store.open_or_resume(wallet="w", outcome_id=1, coin="#1", trigger_family="s3", severity="-.2")
    assert second is not None and second.episode_id != first.episode_id and not store.exhausted(second)
