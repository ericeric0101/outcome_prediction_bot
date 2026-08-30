import sqlite3

from bot.outcome_loss_reentry import OutcomeLossReentryGate
from monitoring.trade_journal_db import TradeJournalDB


def test_reentry_gate_requires_official_sell_fill_and_blocks_same_market(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    gate = OutcomeLossReentryGate(journal, "run")
    assert gate.record_confirmed_loss_exit(outcome_id=1, period="1d", coin="#10", order_id="sell") is False
    journal.log_order_event("run", "ORDER_FILLED", venue_order_id="sell", side="SELL", status="FILLED", instrument_id="#10", payload={
        "venue": "hyperliquid_outcome", "actual_fill": True, "fill_provenance": "hyperliquid_userFills",
    })
    assert gate.record_confirmed_loss_exit(outcome_id=1, period="1d", coin="#10", order_id="sell") is True
    assert gate.evaluate(outcome_id=1).allowed is False
    assert gate.evaluate(outcome_id=2).allowed is True
    with sqlite3.connect(journal.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategy_events WHERE event_type='OUTCOME_LOSS_EXIT_CONFIRMED'").fetchone()[0] == 1
