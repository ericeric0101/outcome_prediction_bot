import json
import sqlite3
from decimal import Decimal

from bot.pricing.outcome_pricing import OutcomePricingState
from bot.outcome_ws_recorder import OutcomeWebSocketRecorder
from monitoring.trade_journal_db import TradeJournalDB


class CallbackClient:
    def __init__(self):
        self.callbacks = {}

    def register_callback(self, channel, callback):
        self.callbacks.setdefault(channel, []).append(callback)

    def unregister_callback(self, channel, callback):
        self.callbacks[channel] = [item for item in self.callbacks.get(channel, []) if item != callback]


def test_ws_recorder_persists_raw_messages_and_lifecycle_resync(tmp_path):
    client = CallbackClient()
    db = tmp_path / "stream.db"
    recorder = OutcomeWebSocketRecorder(client, TradeJournalDB(db), "stream-run")
    recorder._market_id = 1145
    recorder._on_lifecycle({"event": "connected", "received_at_ms": 123})
    recorder._on_l2({"channel": "l2Book", "data": {"time": 456, "coin": "#11450"}})
    recorder._on_mids({"channel": "allMids", "data": {"mids": {"#11450": "0.6"}}})
    recorder._on_trades({"channel": "trades", "data": [{"timestamp": 789, "coin": "#11450"}]})

    assert recorder.resync_required.is_set()
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT event_type, payload_json FROM strategy_events ORDER BY id").fetchall()
    assert [row[0] for row in rows] == [
        "OUTCOME_WS_LIFECYCLE", "OUTCOME_WS_L2_BOOK", "OUTCOME_WS_ALL_MIDS", "OUTCOME_WS_TRADES",
    ]
    l2 = json.loads(rows[1][1])
    assert l2["outcome_id"] == 1145
    assert l2["server_timestamp_ms"] == 456
    assert l2["sequence_available"] is False
    trades = json.loads(rows[3][1])
    assert trades["server_timestamp_ms"] == 789


def test_ws_recorder_unregisters_callbacks_when_stopped():
    client = CallbackClient()
    recorder = OutcomeWebSocketRecorder(client, journal=None, run_id="stream-run")
    recorder._register_callbacks()
    recorder._unregister_callbacks()
    assert all(not callbacks for callbacks in client.callbacks.values())


def test_l2_callback_keeps_terminal_pricing_cache_fresh(tmp_path):
    pricing = OutcomePricingState(stale_timeout_sec=5)
    recorder = OutcomeWebSocketRecorder(
        CallbackClient(), TradeJournalDB(tmp_path / "stream.db"), "stream-run", pricing_state=pricing,
    )
    recorder._on_l2({"channel": "l2Book", "data": {
        "coin": "#11450", "time": 456,
        "levels": [[{"px": "0.60", "sz": "10"}], [{"px": "0.61", "sz": "11"}]],
    }})
    assert pricing.get_best_bid_ask("#11450") == (Decimal("0.60"), Decimal("0.61"))
