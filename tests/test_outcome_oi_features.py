import json
import sqlite3
import pytest

from bot.outcome_oi_features import FEATURE_SCHEMA_VERSION, OutcomeOiFeaturePipeline
from bot.outcome_p2_quality import P2_SCHEMA_VERSION
from monitoring.trade_journal_db import TradeJournalDB


def _book(bid: str, ask: str):
    return {"time": 1, "levels": [[{"px": bid, "sz": "10"}], [{"px": ask, "sz": "10"}]]}


def _snapshot(timestamp: int, *, outcome: int = 9, yes_bid="0.40", yes_ask="0.42"):
    return {"period": "1d", "p2_schema_version": P2_SCHEMA_VERSION, "snapshot_timestamp_ms": timestamp,
            "outcome_id": outcome, "yes_coin": "#90", "no_coin": "#91", "yes_l2": _book(yes_bid, yes_ask),
            "no_l2": _book("0.58", "0.60"), "fee_evidence": {"status": "unverified"},
            "capture_quality": {"status": "accepted"}}


def _oi(journal, *, exchange: int, local: int, oi: str, backfilled=False, mark="70000"):
    return journal.record_binance_oi_observation(run_id="oi", source="test", endpoint="/test", symbol="BTCUSDT",
        exchange_timestamp_ms=exchange, local_received_at_ms=local, request_latency_ms=1, open_interest=oi,
        open_interest_value=None, mark_price=mark, index_price=mark, taker_buy_notional="1", taker_sell_notional="1",
        taker_imbalance=0, backfilled=backfilled, raw_payload_hash=f"{exchange}-{local}-{backfilled}", raw_payload={}, context={})


def test_x3_uses_only_locally_known_live_oi_and_executable_labels(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    base = 2_000_000_000_000
    journal.log_strategy_event("shadow", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(base))
    journal.log_strategy_event("shadow", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(base + 60_000, yes_bid="0.50", yes_ask="0.52"))
    # Newer exchange time but it was not locally received until after the decision: forbidden.
    _oi(journal, exchange=base - 20_000, local=base - 10_000, oi="100")
    _oi(journal, exchange=base + 1, local=base + 10_000, oi="999")
    _oi(journal, exchange=base - 500_000, local=base - 500_000, oi="90")
    result = OutcomeOiFeaturePipeline(journal).build()
    assert result.eligible_snapshots == 2 and result.oi_joined == 2
    with sqlite3.connect(journal.db_path) as conn:
        row = conn.execute("SELECT oi_local_received_at_ms,features_json,labels_json FROM outcome_oi_feature_rows WHERE outcome_snapshot_event_id=1").fetchone()
    assert row[0] == base - 10_000
    features, labels = json.loads(row[1]), json.loads(row[2])
    assert features["open_interest"] == 100.0
    assert labels["future_60s"]["available"] is True
    assert labels["future_60s"]["yes_long_markout_ps"] == pytest.approx(0.08)  # future bid - current ask, not mid.


def test_x3_excludes_backfill_and_never_crosses_market_instances(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    base = 2_100_000_000_000
    journal.log_strategy_event("shadow", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(base, outcome=9))
    journal.log_strategy_event("shadow", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(base + 60_000, outcome=10, yes_bid="0.90", yes_ask="0.92"))
    _oi(journal, exchange=base - 1, local=base - 1, oi="100", backfilled=True)
    result = OutcomeOiFeaturePipeline(journal).build()
    assert result.oi_joined == 0
    assert result.labels_available[60] == 0
    # Explicit research override keeps provenance visible but does not make it a live join.
    OutcomeOiFeaturePipeline(journal, include_backfilled=True).build()
    with sqlite3.connect(journal.db_path) as conn:
        row = conn.execute("SELECT oi_backfilled,labels_json FROM outcome_oi_feature_rows WHERE feature_schema_version=? AND outcome_snapshot_event_id=1", (FEATURE_SCHEMA_VERSION,)).fetchone()
    assert row[0] == 1
    assert json.loads(row[1])["future_60s"]["available"] is False


def test_x3_fill_overlay_uses_only_actual_maker_markout(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    base = 2_200_000_000_000
    _oi(journal, exchange=base - 10, local=base - 5, oi="100")
    journal.log_order_event("live", "ORDER_FILLED", side="BUY", payload={"actual_fill": True, "period": "1d",
        "outcome_id": 9, "trade_id": "fill-1", "timestamp_ms": base, "liquidity_class": "maker", "price": "0.4", "quantity": "10"})
    journal.log_order_event("p3", "FILL_MARKOUT", side="BUY", payload={"actual_fill": True, "p3_markout_schema_version": 2, "fill_context_status": "asof_or_before_fill", "fill_id": "fill-1",
        "horizon_sec": 10, "horizon_tolerance_ms": 2500, "target_lag_ms": 0, "actual_elapsed_ms": 10000,
        "signed_markout_ps": "0.02", "fee_per_share": "0.001", "executable_quote": True})
    result = OutcomeOiFeaturePipeline(journal).build()
    assert result.maker_fill_rows == 1
    with sqlite3.connect(journal.db_path) as conn:
        features, marks = conn.execute("SELECT features_json,actual_markouts_json FROM outcome_oi_fill_feature_rows").fetchone()
    assert json.loads(features)["actual_fill"] is True
    assert json.loads(marks)["10"]["signed_markout_ps"] == "0.02"


def test_x3_resume_checkpoints_batches_and_only_refreshes_the_open_label_window(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    base = 2_300_000_000_000
    # The first snapshot is outside the final 60m+label-tolerance refresh
    # window once the third arrives; a resumed build must not redo it.
    for timestamp in (base, base + 60_000, base + 3_800_000):
        journal.log_strategy_event("shadow", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(timestamp))
    progress: list[tuple[int, int]] = []
    first = OutcomeOiFeaturePipeline(journal).build(
        batch_size=1, progress=lambda completed, total: progress.append((completed, total)),
    )
    assert first.rows_written == 3
    assert progress == [(1, 3), (2, 3), (3, 3)]

    resumed = OutcomeOiFeaturePipeline(journal).build(batch_size=10)
    assert resumed.rows_written == 1  # newest row may still gain a 60m label
    with sqlite3.connect(journal.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM outcome_oi_feature_rows WHERE feature_schema_version=?",
            (FEATURE_SCHEMA_VERSION,),
        ).fetchone()[0] == 3


def _feature_rows(journal):
    with sqlite3.connect(journal.db_path) as conn:
        return conn.execute(
            """SELECT outcome_snapshot_event_id,outcome_id,period,snapshot_timestamp_ms,
                      oi_observation_id,oi_exchange_timestamp_ms,oi_local_received_at_ms,
                      oi_age_ms,oi_join_direction,oi_backfilled,features_json,labels_json,
                      market_context_json
               FROM outcome_oi_feature_rows
               WHERE feature_schema_version=? ORDER BY outcome_snapshot_event_id""",
            (FEATURE_SCHEMA_VERSION,),
        ).fetchall()


def test_x3_incremental_window_matches_full_rebuild_without_reparsing_old_history(tmp_path):
    base = 2_400_000_000_000
    incremental = TradeJournalDB(tmp_path / "incremental.db")
    full = TradeJournalDB(tmp_path / "full.db")
    initial_times = [base + minute * 60_000 for minute in range(70)]
    appended_times = [base + minute * 60_000 for minute in (70, 71)]
    for timestamp in initial_times:
        incremental.log_strategy_event("shadow", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(timestamp))
    OutcomeOiFeaturePipeline(incremental).build(batch_size=10)
    for timestamp in appended_times:
        incremental.log_strategy_event("shadow", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(timestamp))
    resumed = OutcomeOiFeaturePipeline(incremental).build(batch_size=10)

    for timestamp in (*initial_times, *appended_times):
        full.log_strategy_event("shadow", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(timestamp))
    OutcomeOiFeaturePipeline(full).build(batch_size=10)

    # The 62-minute label tail is refreshed; the eight older rows are neither
    # parsed nor rewritten on the resumed run.
    assert resumed.rows_written == 63
    assert resumed.eligible_snapshots == 72
    assert _feature_rows(incremental) == _feature_rows(full)


def test_x3_late_snapshot_falls_back_to_full_rebuild_for_label_correctness(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    base = 2_500_000_000_000
    for timestamp in (base, base + 60_000, base + 90_000, base + 3_800_000):
        journal.log_strategy_event("shadow", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(timestamp))
    OutcomeOiFeaturePipeline(journal).build(batch_size=10)

    # This record is appended later in journal-id order but belongs before the
    # retained 62-minute tail.  A narrow scan would miss label changes, so
    # the pipeline must choose the safe full reconstruction path.
    journal.log_strategy_event("shadow", "OUTCOME_P2_PARITY_SNAPSHOT", _snapshot(base + 30_000))
    resumed = OutcomeOiFeaturePipeline(journal).build(batch_size=10)

    assert resumed.rows_written == 5
    with sqlite3.connect(journal.db_path) as conn:
        labels = json.loads(conn.execute(
            "SELECT labels_json FROM outcome_oi_feature_rows WHERE outcome_snapshot_event_id=5"
        ).fetchone()[0])
    assert labels["future_60s"]["available"] is True
