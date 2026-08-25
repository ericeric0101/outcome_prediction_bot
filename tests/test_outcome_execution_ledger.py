import sqlite3

from bot.outcome_execution_ledger import OutcomeExecutionLedger
from bot.outcome_maker_state_machine import MakerTickResult
from monitoring.trade_journal_db import TradeJournalDB


def test_ledger_records_ack_and_deduplicates_exchange_fills(tmp_path):
    journal = TradeJournalDB(tmp_path / "outcome.db")
    ledger = OutcomeExecutionLedger(journal, "run")
    ledger.record_transition(market_id=1153, coin="#11530", result=MakerTickResult("buy_placed", "resting", "7"))
    fill = {"coin": "#11530", "tid": "trade-1", "side": "B", "px": "0.77", "sz": "13", "time": 1, "fee": "0"}
    assert ledger.sync_fills(fills=[fill, fill], market_key="outcome:1153", period="15m") == 1
    with sqlite3.connect(journal.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM order_events").fetchone()[0] == 2
        payload = conn.execute("SELECT payload_json FROM order_events WHERE event_type='ORDER_FILLED'").fetchone()[0]
    assert '"actual_fill": true' in payload
    assert '"period": "15m"' in payload
