import json
import sqlite3

from bot.outcome_exit_latency_report import report
from monitoring.trade_journal_db import TradeJournalDB


def _event(conn, ts, payload):
    conn.execute(
        "INSERT INTO strategy_events (ts, run_id, event_type, payload_json) VALUES (?, 'run', ?, ?)",
        (ts, "OUTCOME_EXIT_LIFECYCLE", json.dumps(payload)),
    )


def test_latency_report_uses_direct_decision_to_ack_and_actual_inventory_reduction(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    with sqlite3.connect(journal.db_path) as conn:
        # One executable chain reduces inventory.  The timestamps deliberately
        # do not depend on adjacent lifecycle event placement.
        conn.execute(
            "INSERT INTO strategy_events (ts, run_id, event_type, payload_json) VALUES (?, 'run', ?, ?)",
            ("2026-09-14T00:00:00+00:00", "OUTCOME_FAST_FAILURE_EXIT_DECISION",
             json.dumps({"outcome_id": 1, "coin": "#1", "action": "EXECUTE"})),
        )
        _event(conn, "2026-09-14T00:00:02+00:00", {
            "outcome_id": 1, "coin": "#1", "state": "EMERGENCY_EXIT_SUBMITTED",
            "execution_timing": {"risk_detected_ts": 1760000000.0, "ack_ts": 1760000002.0},
        })
        _event(conn, "2026-09-14T00:00:03+00:00", {
            "outcome_id": 1, "coin": "#1", "state": "CLOSED",
            "inventory_reduced": True, "inventory_flat": True,
            "execution_timing": {"risk_detected_ts": 1760000000.0, "inventory_confirmed_ts": 1760000003.0,
                                  "complete_fill_ts": 1760000003.0},
        })
        # An unchanged residual can be reconciled, but must never inflate the
        # "confirmed reduction" latency KPI.
        conn.execute(
            "INSERT INTO strategy_events (ts, run_id, event_type, payload_json) VALUES (?, 'run', ?, ?)",
            ("2026-09-14T01:00:00+00:00", "OUTCOME_EMERGENCY_EXIT_DECISION",
             json.dumps({"outcome_id": 2, "coin": "#2", "action": "EXECUTE"})),
        )
        _event(conn, "2026-09-14T01:00:01+00:00", {
            "outcome_id": 2, "coin": "#2", "state": "EMERGENCY_RESIDUAL",
            "inventory_reduced": False,
            "execution_timing": {"risk_detected_ts": 1760000100.0, "ack_ts": 1760000100.5,
                                  "inventory_confirmed_ts": 1760000101.0},
        })
        conn.commit()

    result = report(journal.db_path)
    assert result["decision_to_exchange_ack_ms"]["count"] == 2
    assert result["decision_to_confirmed_inventory_reduction_ms"] == {
        "count": 1, "median": 3000.0, "p75": 3000.0, "p90": 3000.0, "p95": 3000.0, "max": 3000.0,
    }
    assert result["decision_to_confirmed_flat_ms"] == {
        "count": 1, "median": 3000.0, "p75": 3000.0, "p90": 3000.0, "p95": 3000.0, "max": 3000.0,
    }
    assert result["rows"][0]["inventory_reduced"] is True
    assert result["rows"][0]["inventory_flat"] is True
    assert result["rows"][1]["inventory_reduced"] is False
    assert result["rows"][1]["inventory_flat"] is False
