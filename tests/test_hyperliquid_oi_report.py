import pytest

from monitoring.trade_journal_db import TradeJournalDB
from bot.hyperliquid_oi_report import hyperliquid_oi_comparison_report


def test_hyperliquid_oi_report_uses_causal_asof_pairs_and_not_raw_level_comparison(tmp_path):
    db = TradeJournalDB(tmp_path / "shadow.db")
    for timestamp, oi in ((1_000, "100"), (2_000, "110"), (3_000, "132"), (4_000, "145.2")):
        assert db.record_hyperliquid_perp_context(
            run_id="run", coin="BTC", local_received_at_ms=timestamp,
            context={"openInterest": oi, "markPx": "70000"},
        )
    for timestamp, oi in ((1_100, "1000"), (2_100, "1100"), (3_100, "1320"), (4_100, "1452")):
        assert db.record_binance_oi_observation(
            run_id="run", source="binance", endpoint="/oi", symbol="BTCUSDT",
            exchange_timestamp_ms=timestamp, local_received_at_ms=timestamp,
            request_latency_ms=1, open_interest=oi, raw_payload_hash=str(timestamp), raw_payload={},
        )
    report = hyperliquid_oi_comparison_report(tmp_path / "shadow.db", max_binance_age_ms=500)
    assert report.matched_asof_pairs == 4
    assert report.median_binance_age_ms == 100
    assert report.return_correlation == pytest.approx(1.0)
    assert "Levels are not compared" in report.note
