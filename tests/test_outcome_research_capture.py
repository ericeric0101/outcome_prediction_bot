import json
import sqlite3
from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_research_capture import OutcomeResearchCapture
from monitoring.trade_journal_db import TradeJournalDB


def market():
    return OutcomeMarketSpec(516, "@516", "#5160", "#5161", 100005160, 100005161, "priceBinary", "BTC",
        "20260830-0000", 2_000_000_000, 1, Decimal("70000"), "1d", "raw")


def book(ts: int, bid="0.60", ask="0.61"):
    return {"time": ts, "levels": [[{"px": bid, "sz": "100"}], [{"px": ask, "sz": "100"}]]}


class Client:
    def get_user_fees_sync(self, _wallet):
        return {"userSpotAddRate": "0.0004", "userSpotCrossRate": "0.0007"}

    def get_user_fills_sync(self, _wallet):
        return [{"coin": "#5160", "tid": "fill-1", "side": "B", "px": "0.60", "sz": "10", "fee": "0", "time": 1, "crossed": False}]


def test_live_research_capture_writes_p2_p3_heartbeat_and_gap(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    capture = OutcomeResearchCapture(client=Client(), wallet_address="0x" + "a" * 40, journal=journal,
        interval_sec=1, heartbeat_sec=1, gap_alert_sec=3)
    assert capture.capture_if_due(market=market(), yes_book=book(2_000), no_book=book(2_000, "0.39", "0.40"),
        yes_local_received_at_ms=2_000, no_local_received_at_ms=2_000, capture_complete_at_ms=2_000).captured
    later = capture.capture_if_due(market=market(), yes_book=book(6_000), no_book=book(6_000, "0.39", "0.40"),
        yes_local_received_at_ms=6_000, no_local_received_at_ms=6_000, capture_complete_at_ms=6_000)
    assert later.captured
    with sqlite3.connect(journal.db_path) as conn:
        events = [row[0] for row in conn.execute("SELECT event_type FROM strategy_events")]
        fill_count = conn.execute("SELECT COUNT(*) FROM order_events WHERE event_type='ORDER_FILLED'").fetchone()[0]
        payload = json.loads(conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_P2_PARITY_SNAPSHOT' LIMIT 1").fetchone()[0])
    assert events.count("OUTCOME_P2_PARITY_SNAPSHOT") == 2
    assert "OUTCOME_RESEARCH_CAPTURE_HEARTBEAT" in events
    assert "OUTCOME_RESEARCH_CAPTURE_GAP_ALERT" in events
    assert fill_count == 1
    assert payload["expiry"] == "20260830-0000"
    assert "time_left_sec" in payload and payload["strike"] == "70000"


def test_duplicate_fill_repair_keeps_first_row_and_logs_audit(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    payload = {"venue": "hyperliquid_outcome", "trade_id": "same-trade"}
    journal.log_order_event("old", "ORDER_FILLED", payload=payload)
    journal.log_order_event("old", "ORDER_FILLED", payload=payload)
    assert journal.repair_duplicate_outcome_fills(run_id="repair", dry_run=True) == {"duplicate_count": 1, "removed_count": 0}
    assert journal.repair_duplicate_outcome_fills(run_id="repair", dry_run=False) == {"duplicate_count": 1, "removed_count": 1}
    with sqlite3.connect(journal.db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM order_events WHERE event_type='ORDER_FILLED'").fetchone()[0]
        audit = conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_FILL_DEDUPE_REPAIR' ORDER BY id DESC").fetchone()[0]
    assert rows == 1
    assert json.loads(audit)["removed_count"] == 1
