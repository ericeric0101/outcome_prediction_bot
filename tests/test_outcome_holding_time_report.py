import sqlite3

from monitoring.trade_journal_db import TradeJournalDB
from bot.outcome_holding_time_report import OutcomeHoldingTimeAnalyzer


def _fill(db, *, ts, side, price, qty, fee=0):
    venue_order_id = f"{ts}-{side}"
    db.log_order_event("run", "ORDER_FILLED", venue_order_id=venue_order_id, side=side, price=price, qty=qty,
                       status="FILLED", commission_usdc=fee, instrument_id="#7", payload={"period": "1d"})
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("update order_events set ts=? where venue_order_id=?", (ts, venue_order_id))


def test_holding_time_report_fifo_matches_actual_fills(tmp_path):
    db = TradeJournalDB(tmp_path / "holding.db")
    _fill(db, ts="2026-01-01T00:00:00+00:00", side="BUY", price="0.50", qty="10")
    _fill(db, ts="2026-01-01T00:10:00+00:00", side="SELL", price="0.52", qty="10", fee="0.004")
    result = OutcomeHoldingTimeAnalyzer(db.db_path).report()
    assert result.completed_round_trips == 1
    assert result.holding_time_buckets["5m_to_30m"] == 1
    assert result.net_return_mean is not None and result.net_return_mean > 0
