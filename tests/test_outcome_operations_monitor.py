import sqlite3
from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_operations_monitor import OutcomeOperationsMonitor
from bot.outcome_stream_health import OutcomeStreamHealth
from monitoring.trade_journal_db import TradeJournalDB


def market(): return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "1d", "")


def test_operations_monitor_journals_state_changes_only(tmp_path):
    journal = TradeJournalDB(tmp_path / "ops.db")
    monitor = OutcomeOperationsMonitor(journal, "run")
    health = OutcomeStreamHealth()
    status = monitor.observe(market=market(), fallback_used=True, stream_health=health, automated_execution_enabled=False)
    assert status.ws_reason == "ws_disconnected"
    monitor.observe(market=market(), fallback_used=True, stream_health=health, automated_execution_enabled=False)
    with sqlite3.connect(journal.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategy_events").fetchone()[0] == 1
