import sqlite3

import pytest

from bot.binance_oi import (
    BinanceOiCollector,
    BinanceOiObservation,
    build_current_context,
)
from bot.binance_oi_report import binance_oi_quality_report
from monitoring.trade_journal_db import TradeJournalDB


CURRENT_OI = {"symbol": "BTCUSDT", "openInterest": "12345.678", "time": 1_700_000_000_000}
HISTORICAL_OI = {
    "symbol": "BTCUSDT", "sumOpenInterest": "12000.25",
    "sumOpenInterestValue": "800000000.5", "timestamp": 1_699_999_900_000,
}
PREMIUM = {"symbol": "BTCUSDT", "markPrice": "70000.1", "indexPrice": "69999.9", "time": 1_700_000_000_000}
TRADES = (
    {"p": "70000", "q": "2", "m": False},
    {"p": "70010", "q": "1", "m": True},
)


class FakeBinanceClient:
    def current_open_interest(self, *, symbol):
        assert symbol == "BTCUSDT"
        return CURRENT_OI, 12.5, 1_700_000_000_111

    def historical_open_interest(self, *, symbol, period, limit):
        assert (symbol, period, limit) == ("BTCUSDT", "5m", 2)
        return (HISTORICAL_OI,), 20.0, 1_700_000_000_222

    def premium_index(self, *, symbol):
        assert symbol == "BTCUSDT"
        return PREMIUM, 8.0, 1_700_000_000_333

    def aggregate_trades(self, *, symbol):
        assert symbol == "BTCUSDT"
        return TRADES, 9.0, 1_700_000_000_444


def test_current_oi_contract_rejects_wrong_symbol():
    with pytest.raises(ValueError, match="BTCUSDT"):
        BinanceOiObservation.from_current_payload(
            {**CURRENT_OI, "symbol": "ETHUSDT"}, local_received_at_ms=1, request_latency_ms=1,
        )


def test_context_uses_aggressor_side_not_buyer_maker_side():
    context = build_current_context(PREMIUM, TRADES, premium_request_latency_ms=1, trades_request_latency_ms=1)
    assert context["taker_buy_notional"] == "140000"
    assert context["taker_sell_notional"] == "70010"
    assert context["taker_imbalance"] > 0


def test_collector_persists_deduped_live_and_backfilled_observations(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    collector = BinanceOiCollector(journal=db, run_id="oi-run", client=FakeBinanceClient())
    assert collector.backfill_5m(limit=2) == 1
    assert collector.backfill_5m(limit=2) == 0
    assert collector.collect_current() is True
    assert collector.collect_current() is False
    with sqlite3.connect(db.db_path) as conn:
        rows = conn.execute(
            "SELECT endpoint, backfilled, mark_price, taker_imbalance FROM binance_oi_observations ORDER BY exchange_timestamp_ms"
        ).fetchall()
    assert rows == [
        ("/futures/data/openInterestHist", 1, None, None),
        ("/fapi/v1/openInterest", 0, "70000.1", pytest.approx(0.3332698442931289)),
    ]
    report = binance_oi_quality_report(db.db_path)
    assert report.observation_count == 2
    assert report.live_count == 1
    assert report.backfilled_count == 1
    assert report.symbols == ("BTCUSDT",)


def test_collector_records_gap_alert_and_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("BINANCE_OI_HEARTBEAT_SEC", "1")
    monkeypatch.setenv("BINANCE_OI_GAP_ALERT_SEC", "1")
    db = TradeJournalDB(tmp_path / "journal.db")
    assert db.record_binance_oi_observation(
        run_id="old", source="old", endpoint="/old", symbol="BTCUSDT", exchange_timestamp_ms=1_699_999_000_000,
        local_received_at_ms=1_699_999_000_000, request_latency_ms=1, open_interest="1", raw_payload_hash="old", raw_payload={},
    )
    collector = BinanceOiCollector(journal=db, run_id="oi-run", client=FakeBinanceClient())
    collector.collect_current()
    with sqlite3.connect(db.db_path) as conn:
        events = [row[0] for row in conn.execute("SELECT event_type FROM strategy_events")]
    assert "BINANCE_OI_GAP_ALERT" in events
    assert "BINANCE_OI_HEARTBEAT" in events
