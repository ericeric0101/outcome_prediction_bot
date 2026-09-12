import sqlite3
from datetime import datetime, timezone

import pytest

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_runtime_journal_view import OutcomeRuntimeJournalView
from monitoring.trade_journal_db import TradeJournalDB


def _market() -> OutcomeMarketSpec:
    return OutcomeMarketSpec(
        7, "@7", "#70", "#71", 1, 2, "priceBinary", "BTC",
        "20260913-1400", 1, 0, 1, "1d", "",
    )


def test_journal_view_memoizes_same_fact_for_one_tick(monkeypatch, tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    journal.log_strategy_event("run", "OUTCOME_LIVE_STRATEGY_ENTRY_PLACED", {
        "outcome_id": 7, "coin": "#70", "entry_evidence": {"mark_return_bps": "12"},
    })
    view = OutcomeRuntimeJournalView(journal.db_path)
    original = view._connect
    calls = 0

    def counted_connect():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(view, "_connect", counted_connect)
    first = view.latest_strategy_entry(_market(), "#70")
    assert first is not None
    assert view.live_entry_age_sec(_market(), "#70") is not None
    assert calls == 1
    view.begin_tick()
    assert view.latest_strategy_entry(_market(), "#70") is not None
    assert calls == 2


def test_journal_view_connection_is_read_only(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    view = OutcomeRuntimeJournalView(journal.db_path)
    with view._connect() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO strategy_events(ts, run_id, event_type, payload_json) VALUES (?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), "run", "forbidden", "{}"),
            )
