import sqlite3

import pytest

from bot.outcome_canary_gate import OutcomeCanaryDisabled, OutcomeCanaryGate, OutcomeCanaryReadiness
from monitoring.trade_journal_db import TradeJournalDB


def test_canary_is_hard_disabled_even_if_evidence_thresholds_are_met(tmp_path):
    db = tmp_path / "canary.db"
    journal = TradeJournalDB(db)
    for _ in range(20):
        journal.log_strategy_event("run", "OUTCOME_RESOLUTION_CONFIRMED", {})
    journal.log_strategy_event("run", "OUTCOME_WS_REST_RESYNC", {})
    journal.log_strategy_event("run", "OUTCOME_P2_PARITY_SNAPSHOT", {})
    for _ in range(30):
        journal.log_order_event("run", "ORDER_FILLED", payload={"venue": "hyperliquid_outcome"})
    readiness = OutcomeCanaryReadiness.from_journal(str(db))
    assert readiness.ready_for_live is False
    gate = OutcomeCanaryGate(journal, "canary")
    with pytest.raises(OutcomeCanaryDisabled):
        gate.authorize_live_submission(readiness)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategy_events WHERE event_type='OUTCOME_CANARY_BLOCKED'").fetchone()[0] == 1
