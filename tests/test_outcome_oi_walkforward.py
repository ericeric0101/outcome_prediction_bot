from __future__ import annotations

from bot.outcome_oi_features import FEATURE_SCHEMA_VERSION
from bot.outcome_oi_walkforward import x4_intraday_report, x4_walk_forward_report
from monitoring.trade_journal_db import TradeJournalDB


def _features(*, signal: float, time_left: float = 40_000.0) -> dict[str, float]:
    return {
        "yes_bid": 0.59, "yes_ask": 0.60, "yes_bid_size": 100.0, "yes_ask_size": 100.0,
        "time_left_sec": time_left,
        "btc_mark_return_300s_bps": signal, "btc_mark_return_900s_bps": signal * 0.5,
        "btc_mark_return_3600s_bps": signal * 0.25, "oi_return_300s_bps": signal,
        "oi_return_900s_bps": signal * 0.5, "oi_return_3600s_bps": signal * 0.25,
        "oi_acceleration_5m_vs_15m_bps": signal * 0.5, "price_oi_divergence_5m_bps": 0.0,
        "oi_zscore_1h": signal / 10.0, "taker_imbalance": signal / 100.0,
    }


def _write(journal: TradeJournalDB, *, event_id: int, market: int, timestamp: int, signal: float) -> None:
    journal.upsert_outcome_oi_feature_row(
        feature_schema_version=FEATURE_SCHEMA_VERSION, outcome_snapshot_event_id=event_id,
        outcome_id=market, period="1d", snapshot_timestamp_ms=timestamp, oi_observation_id=event_id,
        oi_exchange_timestamp_ms=timestamp - 1, oi_local_received_at_ms=timestamp - 1, oi_age_ms=1,
        oi_join_direction="as_of_local_received_at", oi_backfilled=False, features=_features(signal=signal),
        labels={
            "future_300s": {"available": True, "yes_long_markout_ps": signal / 10_000.0},
            "future_900s": {"available": True, "yes_long_markout_ps": signal / 10_000.0},
        },
        market_context={"market_instance": str(market)},
    )


def test_x4_uses_full_market_walk_forward_and_purges_trailing_labels(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    event_id = 1
    # Five distinct full daily instances; each covers >300 seconds, so the
    # purge removes only its terminal label-overlap region.
    for market_offset in range(5):
        market = 100 + market_offset
        base = 2_000_000_000_000 + market_offset * 90_000_000
        for index in range(120):
            signal = float((index % 15) - 7)
            _write(journal, event_id=event_id, market=market, timestamp=base + index * 5_000, signal=signal)
            event_id += 1
    report = x4_walk_forward_report(journal.db_path)
    assert report.market_instances == 5
    assert len(report.folds) == 3
    assert report.oos_rows >= 200
    assert all(fold.purged_train_rows > 0 for fold in report.folds)
    assert report.oi_extended_rmse is not None and report.baseline_rmse is not None
    assert report.oi_extended_rmse < report.baseline_rmse
    assert report.incremental_evidence
    # X4 evidence is never an execution authorization by itself.
    assert report.ready_for_x5 is False


def test_x4_excludes_rows_without_decision_time_expiry_or_full_oi_horizons(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    bad = _features(signal=1.0, time_left=-1.0)
    journal.upsert_outcome_oi_feature_row(
        feature_schema_version=FEATURE_SCHEMA_VERSION, outcome_snapshot_event_id=1,
        outcome_id=100, period="1d", snapshot_timestamp_ms=2_000_000_000_000, oi_observation_id=1,
        oi_exchange_timestamp_ms=1, oi_local_received_at_ms=1, oi_age_ms=1,
        oi_join_direction="as_of_local_received_at", oi_backfilled=False, features=bad,
        labels={"future_300s": {"available": True, "yes_long_markout_ps": 0.01}}, market_context={},
    )
    report = x4_walk_forward_report(journal.db_path)
    assert report.eligible_rows == 0
    assert "insufficient_independent_daily_market_instances" in report.blockers


def test_x4a_intraday_uses_non_overlapping_5m_and_15m_labels_but_never_authorizes_live(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    event_id = 1
    # One active daily contract is enough for provisional rolling research;
    # 5-minute / 15-minute samples are deliberately not called independent
    # daily instances.
    for index in range(300):
        _write(journal, event_id=event_id, market=100, timestamp=2_000_000_000_000 + index * 5_000,
               signal=float((index % 15) - 7))
        event_id += 1
    report = x4_intraday_report(journal.db_path)
    five_minute, fifteen_minute = report.horizons
    assert report.provisional_only and report.ready_for_x5 is False
    assert five_minute.sampled_rows == 5
    assert fifteen_minute.sampled_rows == 2
    assert "insufficient_intraday_oos_rows" in five_minute.blockers
