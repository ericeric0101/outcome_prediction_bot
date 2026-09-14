from bot.outcome_deribit_features import DERIBIT_FEATURE_SCHEMA_VERSION
from bot.outcome_deribit_walkforward import deribit_walk_forward_report
from monitoring.trade_journal_db import TradeJournalDB


def _features(signal: float) -> dict[str, float]:
    return {
        "yes_bid": 0.59, "yes_ask": 0.60, "yes_bid_size": 100.0, "yes_ask_size": 100.0,
        "time_left_sec": 40_000.0, "deribit_mid_return_5s_bps": signal,
        "deribit_index_return_5s_bps": signal * 0.5, "deribit_spread_bps": 1.0,
        "deribit_top_imbalance": signal / 10.0, "deribit_funding_8h": 0.0001,
        "deribit_trade_flow_imbalance_1s": signal / 100.0, "deribit_trade_flow_present_1s": 1.0,
    }


def test_d3_compares_same_rows_by_full_daily_market_walk_forward(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    rows = []
    event_id = 1
    for offset in range(5):
        market = 100 + offset
        base = 2_000_000_000_000 + offset * 90_000_000
        for index in range(120):
            signal = float((index % 15) - 7)
            rows.append({
                "feature_schema_version": DERIBIT_FEATURE_SCHEMA_VERSION, "outcome_snapshot_event_id": event_id,
                "outcome_id": market, "period": "1d", "snapshot_timestamp_ms": base + index * 5_000,
                "deribit_snapshot_event_id": event_id, "deribit_source_timestamp_ms": base + index * 5_000 - 100,
                "deribit_local_received_at_ms": base + index * 5_000 - 1, "deribit_age_ms": 1,
                "deribit_join_direction": "as_of_local_received_at", "deribit_valid": True,
                "features": _features(signal),
                "labels": {"future_300s": {"available": True, "yes_long_markout_ps": signal / 10_000.0}},
                "market_context": {"market_instance": str(market)},
            })
            event_id += 1
    journal.bulk_upsert_outcome_deribit_feature_rows(rows, batch_size=50)
    report = deribit_walk_forward_report(journal.db_path)
    assert report.market_instances == 5
    assert len(report.folds) == 3
    assert report.oos_rows >= 200
    assert report.deribit_rmse is not None and report.baseline_rmse is not None
    assert report.deribit_rmse < report.baseline_rmse
    assert report.ready_for_live is False
