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
        conn.execute("""CREATE TABLE outcome_realized_pnl_lots (
            open_trade_id TEXT, realized_net_usdc TEXT, recorded_at TEXT)""")
        _event(conn, "OUTCOME_ENTRY_QUALITY_SHADOW", {
            "period": "1d", "order_id": "o1", "signal_state": "SIGNAL_DECAY",
            "stale_cancel_shadow": {"action": "CANCEL_STALE_SHADOW"},
        })
        _event(conn, "OUTCOME_POST_FILL_QUALITY_SHADOW", {
            "period": "1d", "fill_trade_id": "t1",
            "post_fill_scratch_shadow": {"action": "SCRATCH_IOC_COUNTERFACTUAL"},
        })
        conn.execute("INSERT INTO order_events(event_type,payload_json) VALUES (?,?)", (
            "FILL_MARKOUT", json.dumps({"fill_id": "t1", "horizon_sec": 30, "signed_markout_ps": -0.01}),
        ))
        _event(conn, "OUTCOME_HOLDING_PATH_OBSERVATION", {
            "entry_trade_id": "t1", "marketable_net_exit_vs_entry_pct": "0.02",
        })
        conn.execute("UPDATE strategy_events SET ts='2026-09-15T00:00:10+00:00' WHERE event_type='OUTCOME_HOLDING_PATH_OBSERVATION'")
        conn.execute("INSERT INTO outcome_realized_pnl_lots VALUES ('t1','0.2','2026-09-15T00:01:00+00:00')")
    result = report(db)
    assert result["prefill_observation_count"] == 1
    assert result["stale_cancel_shadow_actions"] == {"CANCEL_STALE_SHADOW": 1}
    assert result["first_postfill_watch_by_trade"][0]["p3_signed_markout_ps"] == {"30": -0.01}
    assert result["postfill_scratch_p3_comparison"]["ever_scratch_candidate"]["30"] == {
        "n": 1, "mean": -0.01, "median": -0.01, "negative_rate": 1.0,
    }
    outcome = result["postfill_scratch_lifecycle_outcomes"][0]
    assert outcome["classification"] == "realized_profit_recovery"
    assert outcome["post_scratch_executable_path"]["30"]["recovered_to_nonnegative_at"] is not None


def test_report_aligns_scratch_only_to_its_exact_90s_market_risk_path(tmp_path):
    db = tmp_path / "alignment.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE strategy_events (id INTEGER PRIMARY KEY,ts TEXT,run_id TEXT,event_type TEXT,payload_json TEXT)")
        conn.execute("CREATE TABLE order_events (id INTEGER PRIMARY KEY,payload_json TEXT,event_type TEXT)")
        _event(conn, "OUTCOME_POST_FILL_QUALITY_SHADOW", {
            "period": "1d", "outcome_id": 7, "order_id": "o1", "fill_trade_id": "chop",
            "executable_return_pct": "-0.03",
            "post_fill_scratch_shadow": {"action": "SCRATCH_IOC_COUNTERFACTUAL"},
        })
        _event(conn, "OUTCOME_POST_FILL_QUALITY_SHADOW", {
            "period": "1d", "outcome_id": 8, "order_id": "o2", "fill_trade_id": "other",
            "executable_return_pct": "-0.03",
            "post_fill_scratch_shadow": {"action": "SCRATCH_IOC_COUNTERFACTUAL"},
        })
        for seconds, bid, ask, depth in ((10, "0.60", "0.62", "100"), (20, "0.55", "0.572", "50"), (30, "0.57", "0.5814", "75")):
            conn.execute(
                "INSERT INTO strategy_events(ts,run_id,event_type,payload_json) VALUES (?,?,?,?)",
                (f"2026-09-15T00:00:{seconds:02d}+00:00", "r", "OUTCOME_MARKET_RISK_MONITOR_SHADOW", json.dumps({
                    "entry_lifecycle_id": "official_buy:o1:chop", "best_bid": bid, "best_ask": ask, "top3_depth": depth,
                })),
            )
    result = report(db)
    rows = {row["fill_trade_id"]: row for row in result["postfill_scratch_lifecycle_outcomes"]}
    assert rows["chop"]["market_risk_90s"]["classification"] == "SCRATCH_CHOP_RECOVERY_RESEARCH"
    assert rows["other"]["market_risk_90s"] == {
        "classification": "SCRATCH_UNRESOLVED_RESEARCH", "missing_data_reason": "fewer_than_three_monitor_samples",
        "sample_count": 0, "bid_path_efficiency": None, "bid_direction_flips": 0,
        "depth_refill_ratio": None, "spread_convergence_ratio": None,
    }
