import json

from bot.outcome_exit_requote_replay import replay_exit_quotes
from monitoring.trade_journal_db import TradeJournalDB


def _snapshot(timestamp, bid="0.79", ask="0.80"):
    book = {"levels": [[{"px": bid, "sz": "20"}], [{"px": ask, "sz": "20"}]]}
    return {"venue": "hyperliquid_outcome", "period": "1d", "snapshot_timestamp_ms": timestamp,
            "outcome_id": 1153, "yes_coin": "#11530", "no_coin": "#11531", "yes_l2": book, "no_l2": book}


def test_replay_is_journal_only_and_uses_future_snapshots(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    journal.log_strategy_event("capture", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(1_000))
    journal.log_strategy_event("capture", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(2_000, "0.70", "0.71"))
    journal.log_order_event("fill", "ORDER_FILLED", side="BUY", price=0.80, qty=13, instrument_id="#11530", payload={
        "venue": "hyperliquid_outcome", "period": "1d", "coin": "#11530", "timestamp_ms": 1_500,
    })
    report = replay_exit_quotes(db_path=journal.db_path, run_id="replay")
    assert report.snapshots_considered == 1
    assert report.plans_written == 1
    import sqlite3
    with sqlite3.connect(journal.db_path) as conn:
        raw = conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_EXIT_REQUOTE_REPLAY'").fetchone()[0]
    payload = json.loads(raw)
    assert payload["read_only"] is True and payload["execution_submitted"] is False
    assert payload["plan"]["action"] == "CANCEL_REPLACE"


def test_replay_ignores_other_period_and_never_needs_gateway(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    journal.log_strategy_event("capture", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(2_000))
    journal.log_order_event("fill", "ORDER_FILLED", side="BUY", price=0.8, qty=13, payload={
        "venue": "hyperliquid_outcome", "period": "15m", "coin": "#11530", "timestamp_ms": 1_000,
    })
    report = replay_exit_quotes(db_path=journal.db_path, run_id="replay")
    assert report.plans_written == 0
