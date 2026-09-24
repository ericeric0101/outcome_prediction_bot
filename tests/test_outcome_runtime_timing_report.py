import json
import sqlite3

from bot.outcome_runtime_timing_report import report
from monitoring.trade_journal_db import TradeJournalDB


def _timing(conn, payload):
    conn.execute(
        "INSERT INTO strategy_events (ts,run_id,event_type,payload_json) VALUES ('2026-09-22T00:00:00+00:00','run','OUTCOME_RUNTIME_TIMING',?)",
        (json.dumps(payload),),
    )


def test_runtime_timing_report_summarises_stages_account_and_sdk_in_bounded_window(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    with sqlite3.connect(journal.db_path) as conn:
        _timing(conn, {
            "total_ms": 1000, "fill_sync_ms": 400,
            "account_read_timing": {"get_user_fills_sync": {"elapsed_ms": 390}},
            "sdk_requests": [{
                "command": "fetch_order_book", "python_round_trip_ms": 200,
                "sidecar_step_timing": {
                    "market_side_lookup_ms": 12.5, "market_side_cache_hit": False,
                    "alo_book_check_ms": 200,
                },
            }],
        })
        _timing(conn, {
            "total_ms": 3000, "fill_sync_ms": 1400,
            "account_read_timing": {"get_user_fills_sync": {"elapsed_ms": 1390}},
            "sdk_requests": [{"command": "cancel_order", "python_round_trip_ms": 1800}],
        })
        conn.commit()

    result = report(journal.db_path, recent_event_limit=10)
    assert result["rows_scanned"] == 2
    assert result["stages_ms"]["total_ms"] == {
        "count": 2, "sum_ms": 4000.0, "median_ms": 2000.0, "p90_ms": 1000.0, "max_ms": 3000.0,
    }
    assert result["account_endpoints_ms"]["get_user_fills_sync"]["sum_ms"] == 1780.0
    assert result["sdk_commands_ms"]["fetch_order_book"]["max_ms"] == 200.0
    assert result["sdk_commands_ms"]["cancel_order"]["max_ms"] == 1800.0
    assert result["sdk_command_steps_ms"]["fetch_order_book"]["market_side_lookup_ms"]["max_ms"] == 12.5
    assert result["sdk_command_steps_ms"]["fetch_order_book"]["alo_book_check_ms"]["sum_ms"] == 200.0
    assert "market_side_cache_hit" not in result["sdk_command_steps_ms"]["fetch_order_book"]


def test_runtime_timing_report_handles_missing_journal_and_rejects_unbounded_limit(tmp_path):
    assert report(tmp_path / "missing.db")["blockers"] == ["journal_missing"]
    journal = TradeJournalDB(tmp_path / "journal.db")
    try:
        report(journal.db_path, recent_event_limit=0)
    except ValueError as exc:
        assert str(exc) == "recent_event_limit must be positive"
    else:
        raise AssertionError("expected positive recent_event_limit validation")
