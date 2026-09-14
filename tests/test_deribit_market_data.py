import json
import sqlite3

from bot.deribit_feature_report import deribit_feature_quality_report
from bot.deribit_market_data import DeribitMarketDataWorker
from monitoring.trade_journal_db import TradeJournalDB


def _message(channel, data):
    return {"jsonrpc": "2.0", "method": "subscription", "params": {"channel": channel, "data": data}}


def test_public_deribit_worker_derives_bounded_perpetual_features(tmp_path):
    db = TradeJournalDB(tmp_path / "deribit.db")
    worker = DeribitMarketDataWorker(journal=db, run_id="deribit", max_age_sec=3)
    worker.on_message(_message("book.BTC-PERPETUAL.100ms", {
        "type": "snapshot", "change_id": 10, "timestamp": 1_000,
        "bids": [["new", "100", "2"]], "asks": [["new", "101", "3"]],
    }), received_at_ms=1_100)
    worker.on_message(_message("ticker.BTC-PERPETUAL.100ms", {
        "best_bid_price": 100, "best_ask_price": 101, "mark_price": 100.4,
        "index_price": 100.2, "open_interest": 1234, "funding_8h": 0.0001,
    }), received_at_ms=1_120)
    worker.on_message(_message("deribit_price_index.btc_usd", {"price": 100.3}), received_at_ms=1_130)
    worker.on_message(_message("trades.BTC-PERPETUAL.100ms", [
        {"direction": "buy", "price": 100, "amount": 2, "timestamp": 1_150},
        {"direction": "sell", "price": 101, "amount": 1, "timestamp": 1_160},
    ]), received_at_ms=1_170)

    snap = worker.record_snapshot(now_ms=1_200)
    assert snap.valid is True
    payload = snap.payload()
    assert payload["source"] == "deribit_public_ws"
    assert payload["live_authority"] is False
    assert payload["best_bid"] == 100.0
    assert payload["best_ask"] == 101.0
    assert payload["index_price"] == 100.3
    assert payload["trade_buy_notional_1s"] == 200.0
    assert payload["trade_sell_notional_1s"] == 101.0
    assert "raw" not in payload

    with sqlite3.connect(db.db_path) as conn:
        event = conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='DERIBIT_FEATURE_SNAPSHOT'").fetchone()[0]
    assert json.loads(event)["recording_scope"] == "derived_perpetual_features_only_v1"


def test_book_sequence_gap_clears_features_fail_closed(tmp_path):
    worker = DeribitMarketDataWorker(journal=TradeJournalDB(tmp_path / "deribit.db"), run_id="deribit", max_age_sec=3)
    worker.on_message(_message("book.BTC-PERPETUAL.100ms", {
        "type": "snapshot", "change_id": 10, "timestamp": 1_000,
        "bids": [["new", "100", "2"]], "asks": [["new", "101", "3"]],
    }), received_at_ms=1_100)
    worker.on_message(_message("book.BTC-PERPETUAL.100ms", {
        "type": "change", "prev_change_id": 8, "change_id": 11, "timestamp": 1_200,
        "bids": [["change", "100", "1"]], "asks": [],
    }), received_at_ms=1_210)
    snapshot = worker.snapshot(now_ms=1_220)
    assert snapshot.valid is False
    assert snapshot.unavailable_reason == "book_not_ready"
    assert snapshot.best_bid is None


def test_stale_book_is_not_reused_as_a_feature(tmp_path):
    worker = DeribitMarketDataWorker(journal=TradeJournalDB(tmp_path / "deribit.db"), run_id="deribit", max_age_sec=1)
    worker.on_message(_message("book.BTC-PERPETUAL.100ms", {
        "type": "snapshot", "change_id": 10, "timestamp": 1_000,
        "bids": [["new", "100", "2"]], "asks": [["new", "101", "3"]],
    }), received_at_ms=1_000)
    snapshot = worker.snapshot(now_ms=2_001)
    assert snapshot.valid is False
    assert snapshot.unavailable_reason == "book_stale"
    assert snapshot.best_ask is None


def test_deribit_quality_report_counts_validity(tmp_path):
    db_path = tmp_path / "deribit.db"
    db = TradeJournalDB(db_path)
    db.log_strategy_event("run", "DERIBIT_FEATURE_SNAPSHOT", {"valid": True})
    db.log_strategy_event("run", "DERIBIT_FEATURE_SNAPSHOT", {"valid": False})
    db.log_strategy_event("run", "DERIBIT_RESEARCH_STATUS", {"state": "ready"})
    report = deribit_feature_quality_report(db_path)
    assert report.snapshots == 2
    assert report.valid_snapshots == 1
    assert report.unavailable_snapshots == 1
    assert report.status_events == 1
