from pathlib import Path

from bot.outcome_runtime_safety import OutcomeRuntimeSafety, SafetyComponent, safe_config_fingerprint
from monitoring.trade_journal_db import TradeJournalDB


def components():
    return (SafetyComponent("protective_sell", True, "safety_critical", "v1", "ready", True, True),)


def test_safe_config_fingerprint_excludes_secrets():
    first = safe_config_fingerprint({"OUTCOME_EXIT_REQUOTE_ENABLED": "1", "OUTCOME_PRIVATE_KEY": "one"})
    second = safe_config_fingerprint({"OUTCOME_EXIT_REQUOTE_ENABLED": "1", "OUTCOME_PRIVATE_KEY": "two"})
    assert first == second


def test_manifest_and_transition_audit_are_durable_and_no_spam(tmp_path):
    journal = TradeJournalDB(str(tmp_path / "journal.db"))
    safety = OutcomeRuntimeSafety(journal=journal, run_id="run", repo_root=Path.cwd())
    assert safety.write_startup_manifest(components()) is True
    safety.audit_gate(component="s3", eligible=False, reason="age", outcome_id=1, lifecycle_id="x",
                      position_age_sec=1, current_executable_pnl="-0.1", reversal_state="weak",
                      independent_confirmation_count=0, loss_band_state="SELL_RESTING", book_state="fresh")
    safety.audit_gate(component="s3", eligible=False, reason="age", outcome_id=1, lifecycle_id="x",
                      position_age_sec=2, current_executable_pnl="-0.2", reversal_state="weak",
                      independent_confirmation_count=0, loss_band_state="SELL_RESTING", book_state="fresh")
    safety.audit_gate(component="s3", eligible=True, reason="execute", outcome_id=1, lifecycle_id="x",
                      position_age_sec=3, current_executable_pnl="-0.2", reversal_state="confirmed",
                      independent_confirmation_count=3, loss_band_state="LOSS_BAND_UNFILLED", book_state="fresh")
    with journal._connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM strategy_events WHERE event_type='OUTCOME_EXIT_SAFETY_GATE_DECISION'").fetchone()[0]
    assert count == 2
