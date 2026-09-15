import json
import sqlite3

from bot.outcome_entry_quality_report import report


def _event(conn, event_type, payload):
    conn.execute(
        "INSERT INTO strategy_events(ts,run_id,event_type,payload_json) VALUES ('2026-09-15T00:00:00+00:00','r',?,?)",
        (event_type, json.dumps(payload)),
    )


def test_report_groups_shadow_candidates_and_only_joins_p3_by_trade_id(tmp_path):
    db = tmp_path / "journal.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE strategy_events (id INTEGER PRIMARY KEY,ts TEXT,run_id TEXT,event_type TEXT,payload_json TEXT)")
        conn.execute("CREATE TABLE order_events (id INTEGER PRIMARY KEY,payload_json TEXT,event_type TEXT)")
        _event(conn, "OUTCOME_ENTRY_QUALITY_SHADOW", {
            "period": "1d", "order_id": "o1", "signal_state": "SIGNAL_DECAY",
            "stale_cancel_shadow": {"action": "CANCEL_STALE_SHADOW"},
        })
        _event(conn, "OUTCOME_POST_FILL_QUALITY_SHADOW", {"period": "1d", "fill_trade_id": "t1"})
        conn.execute("INSERT INTO order_events(event_type,payload_json) VALUES (?,?)", (
            "FILL_MARKOUT", json.dumps({"fill_id": "t1", "horizon_sec": 30, "signed_markout_ps": -0.01}),
        ))
    result = report(db)
    assert result["prefill_observation_count"] == 1
    assert result["stale_cancel_shadow_actions"] == {"CANCEL_STALE_SHADOW": 1}
    assert result["first_postfill_watch_by_trade"][0]["p3_signed_markout_ps"] == {"30": -0.01}
