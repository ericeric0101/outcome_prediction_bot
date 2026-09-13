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


def test_restart_reconciles_accepted_ioc_evidence_into_same_episode_budget(tmp_path):
    journal = TradeJournalDB(str(tmp_path / "journal.db"))
    store = OutcomeRiskEpisodeStore(journal, "run", max_attempts=1)
    episode = store.open_or_resume(wallet="w", outcome_id=1, coin="#1", trigger_family="fast", severity="-.1")
    assert episode is not None
    # Simulate the narrow crash window after controller durability but before
    # the episode attempt write completes.
    journal.log_durable_strategy_event("run", "OUTCOME_EXIT_LIFECYCLE", {
        "wallet": "w", "outcome_id": 1, "coin": "#1", "state": "EMERGENCY_EXIT_SUBMITTED",
    })
    restarted = OutcomeRiskEpisodeStore(journal, "after-restart", max_attempts=1)
    recovered = restarted.active(wallet="w", outcome_id=1, coin="#1")
    assert recovered is not None and recovered.attempts == 1 and restarted.exhausted(recovered)


def test_restart_reconciles_ambiguous_ioc_evidence_into_same_episode_budget(tmp_path):
    journal = TradeJournalDB(str(tmp_path / "journal.db"))
    store = OutcomeRiskEpisodeStore(journal, "run", max_attempts=1)
    assert store.open_or_resume(wallet="w", outcome_id=1, coin="#1", trigger_family="s3", severity="-.2")
    journal.log_durable_strategy_event("run", "OUTCOME_EXIT_ORDER_AMBIGUOUS_SUBMIT", {
        "wallet": "w", "outcome_id": 1, "coin": "#1", "order_kind": "emergency_ioc",
    })
    restarted = OutcomeRiskEpisodeStore(journal, "after-restart", max_attempts=1)
    recovered = restarted.active(wallet="w", outcome_id=1, coin="#1")
    assert recovered is not None and restarted.exhausted(recovered)


def test_episode_becomes_exhausted_after_configured_attempts(tmp_path):
    journal = TradeJournalDB(str(tmp_path / "journal.db"))
    store = OutcomeRiskEpisodeStore(journal, "run", max_attempts=2)
    episode = store.open_or_resume(wallet="w", outcome_id=1, coin="#1", trigger_family="fast", severity="-.1")
    assert episode is not None
    episode = store.record_attempt(episode, execution_state="emergency_exit_residual")
    assert episode is not None and not store.exhausted(episode)
    episode = store.record_attempt(episode, execution_state="emergency_exit_residual")
    assert episode is not None and store.exhausted(episode)


def test_attempt_write_failure_marks_current_process_budget_uncertain(tmp_path, monkeypatch):
    journal = TradeJournalDB(str(tmp_path / "journal.db"))
    store = OutcomeRiskEpisodeStore(journal, "run", max_attempts=2)
    episode = store.open_or_resume(wallet="w", outcome_id=1, coin="#1", trigger_family="fast", severity="-.1")
    assert episode is not None
    monkeypatch.setattr(journal, "log_durable_strategy_event", lambda *_args, **_kwargs: None)
    assert store.record_attempt(episode, execution_state="reconcile_required") is None
    uncertain = store.active(wallet="w", outcome_id=1, coin="#1")
    assert uncertain is not None and store.exhausted(uncertain)
