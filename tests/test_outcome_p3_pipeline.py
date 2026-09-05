from decimal import Decimal
import json
import sqlite3

from bot.outcome_event_bridge import OutcomeFillEvent
from bot.outcome_markout import OutcomeQuote
from bot.outcome_p3_pipeline import OutcomeP3Pipeline
from monitoring.trade_journal_db import TradeJournalDB


def fill():
    return OutcomeFillEvent(1153, 0, "#11530", "c", "1", "tid-1", "BUY", Decimal("0.7"), Decimal("13"), Decimal("0"), "USDH", 1_000, True, {})


def _context(*, time_left=700, spread="0.01", depth="100"):
    return {"#11530": {"time_left_sec": time_left, "spread": spread, "depth": depth, "volatility_regime": "quiet"}}


def _snapshot(pipeline, *, event_id, timestamp_ms, bid, ask):
    return pipeline.record_quote_snapshot(
        snapshot_event_id=event_id,
        outcome_id=1153,
        period="1d",
        snapshot_timestamp_ms=timestamp_ms,
        quotes=(OutcomeQuote("#11530", timestamp_ms, Decimal(bid), Decimal(ask), event_id),),
        quote_contexts=_context(),
    )


def test_p3_pipeline_uses_compact_quote_window_and_fill_time_context(tmp_path):
    pipeline = OutcomeP3Pipeline(TradeJournalDB(tmp_path / "p3.db"), "run")
    assert _snapshot(pipeline, event_id=1, timestamp_ms=1_000, bid="0.69", ask="0.70") == 0
    assert pipeline.record_actual_fill(fill(), period="1d", observed_at_ms=1_000)
    assert _snapshot(pipeline, event_id=2, timestamp_ms=6_000, bid="0.71", ask="0.72") == 1
    assert _snapshot(pipeline, event_id=3, timestamp_ms=11_000, bid="0.70", ask="0.71") == 1
    assert _snapshot(pipeline, event_id=4, timestamp_ms=31_000, bid="0.69", ask="0.70") == 1
    assert _snapshot(pipeline, event_id=5, timestamp_ms=34_000, bid="0.68", ask="0.69") == 0
    with sqlite3.connect(pipeline.journal.db_path) as conn:
        rows = conn.execute("SELECT payload_json FROM order_events WHERE event_type='FILL_MARKOUT' ORDER BY id").fetchall()
        pending = conn.execute("SELECT COUNT(*) FROM outcome_p3_pending_fills").fetchone()[0]
        quotes = conn.execute("SELECT COUNT(*) FROM outcome_p3_quote_index").fetchone()[0]
    assert len(rows) == 3
    payload = json.loads(rows[0][0])
    assert payload["p3_markout_schema_version"] == 2
    assert payload["actual_elapsed_ms"] == 5_000
    assert payload["fill_context_status"] == "asof_or_before_fill"
    assert payload["fill_context_timestamp_ms"] == 1_000
    assert payload["entry_regime_bucket"] == "600_plus|buy|tight|deep|quiet"
    assert pending == 0
    assert quotes == 5


def test_p3_pipeline_does_not_requeue_old_user_fill_after_restart_window(tmp_path):
    pipeline = OutcomeP3Pipeline(TradeJournalDB(tmp_path / "p3.db"), "run")
    assert _snapshot(pipeline, event_id=1, timestamp_ms=1_000, bid="0.69", ask="0.70") == 0
    assert pipeline.record_actual_fill(fill(), period="1d", observed_at_ms=40_000)
    assert _snapshot(pipeline, event_id=2, timestamp_ms=6_000, bid="0.71", ask="0.72") == 0
    with sqlite3.connect(pipeline.journal.db_path) as conn:
        raw_fills = conn.execute("SELECT COUNT(*) FROM order_events WHERE event_type='ORDER_FILLED'").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM outcome_p3_pending_fills").fetchone()[0]
        markouts = conn.execute("SELECT COUNT(*) FROM order_events WHERE event_type='FILL_MARKOUT'").fetchone()[0]
    assert raw_fills == 1
    assert pending == 0
    assert markouts == 0


def test_p3_pipeline_never_treats_nonmaker_or_unknown_period_as_calibration_sample(tmp_path):
    pipeline = OutcomeP3Pipeline(TradeJournalDB(tmp_path / "p3.db"), "run")
    taker = OutcomeFillEvent(1153, 0, "#11530", None, "1", "tid-t", "BUY", Decimal("0.7"), Decimal("13"), Decimal("0"), "USDH", 1_000, False, {})
    pipeline.record_actual_fill(taker, period="unknown", observed_at_ms=1_000)
    assert _snapshot(pipeline, event_id=1, timestamp_ms=6_000, bid="0.71", ask="0.72") == 0
