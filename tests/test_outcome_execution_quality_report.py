import json
import sqlite3

from bot.outcome_execution_quality_report import report
from monitoring.trade_journal_db import TradeJournalDB


def _order(conn, *, ts, event_type, oid, side, instrument="#11", price=None, qty=None, payload=None):
    conn.execute(
        """INSERT INTO order_events
           (ts, run_id, event_type, venue_order_id, side, price, qty, status, instrument_id, payload_json)
           VALUES (?, 'run', ?, ?, ?, ?, ?, 'ok', ?, ?)""",
        (ts, event_type, oid, side, price, qty, instrument, json.dumps(payload or {})),
    )


def test_f5_report_links_capacity_submit_fill_protection_exit_and_canonical_pnl(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    audit = {"audit": {
        "entry_capacity_canary_enabled": True, "entry_tier": "tier_a_spot_mark_oi",
        "entry_requested_shares": 22, "entry_safe_max_shares": 40,
        "entry_submitted_shares": 22, "entry_submit_bid": "0.90",
        "entry_spread_bps": "100", "entry_recent_trade_shares_5m": "80",
        "entry_top3_depth_shares": "120", "entry_time_left_sec": 3600,
        "entry_regime_state": "TREND", "entry_regime_reason": "confirmed",
    }}
    with sqlite3.connect(journal.db_path) as conn:
        _order(conn, ts="2026-09-07T00:00:00+00:00", event_type="ORDER_SUBMIT", oid="buy", side="BUY", payload=audit)
        _order(conn, ts="2026-09-07T00:00:05+00:00", event_type="ORDER_FILLED", oid="buy", side="BUY", price=0.9, qty=21, payload={"trade_id": "t1"})
        _order(conn, ts="2026-09-07T00:00:08+00:00", event_type="ORDER_FILLED", oid="buy", side="BUY", price=0.9, qty=1, payload={"trade_id": "t2"})
        _order(conn, ts="2026-09-07T00:00:13+00:00", event_type="FILL_MARKOUT", oid="buy", side="BUY", payload={"fill_id": "t1", "horizon_sec": 5, "signed_markout_ps": "0.01", "p3_markout_schema_version": 2})
        _order(conn, ts="2026-09-07T00:00:10+00:00", event_type="ORDER_SUBMIT", oid="sell1", side="SELL")
        _order(conn, ts="2026-09-07T00:01:00+00:00", event_type="ORDER_SUBMIT", oid="sell2", side="SELL")
        _order(conn, ts="2026-09-07T00:02:00+00:00", event_type="ORDER_FILLED", oid="sell2", side="SELL", price=0.92, qty=22)
        conn.execute(
            """INSERT INTO outcome_realized_pnl_lots
               (close_trade_id, open_trade_id, outcome_id, side_index, close_kind, quantity, cost_usdc, proceeds_usdc, realized_net_usdc, source_json, recorded_at)
               VALUES ('close', 't1', 1, 1, 'sell', '21', '18.9', '19.32', '0.42', '{}', '2026-09-07T00:02:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO outcome_realized_pnl_lots
               (close_trade_id, open_trade_id, outcome_id, side_index, close_kind, quantity, cost_usdc, proceeds_usdc, realized_net_usdc, source_json, recorded_at)
               VALUES ('close', 't2', 1, 1, 'sell', '1', '0.9', '0.92', '0.02', '{}', '2026-09-07T00:02:00+00:00')"""
        )
        conn.commit()
    journal.log_strategy_event("run", "OUTCOME_EXIT_LIFECYCLE", {
        "coin": "#11", "state": "SELL_RESTING", "reason": "target_reprice",
    })

    result = report(journal.db_path)
    assert result["entry_count"] == 1
    row = result["entries"][0]
    assert row["lifecycle_state"] == "closed"
    assert row["filled_shares"] == "22"
    assert row["fill_count"] == 2
    assert row["protective_sell_delay_sec"] == 2.0
    assert row["target_replacement_count"] == 1
    assert row["holding_sec"] == 112.0
    assert row["canonical_realized_net_usdc"] == "0.44"
    assert row["entry_time_left_sec"] == 3600
    assert row["entry_regime_state"] == "TREND"
    assert row["top3_depth_shares"] == "120"
    assert row["p3_fee_adjusted_markout_per_share"] == {"t1": {"5": "0.01"}}
    assert result["p3_fee_adjusted_markout_observations"] == {
        "total": 1, "by_horizon_sec": {"5": 1}, "current_5_10_30sec_schema_v2": {"5": 1},
    }
    assert result["size_buckets"][0]["size_bucket"] == "up_to_20"


def test_f5_report_keeps_unfilled_capacity_order_without_numeric_type_error(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    audit = {"audit": {
        "entry_capacity_canary_enabled": True, "entry_requested_shares": 22,
        "entry_safe_max_shares": 30, "entry_submitted_shares": 22,
        "entry_submit_bid": "0.90",
    }}
    with sqlite3.connect(journal.db_path) as conn:
        _order(conn, ts="2026-09-07T00:00:00+00:00", event_type="ORDER_SUBMIT", oid="buy", side="BUY", payload=audit)
        _order(conn, ts="2026-09-07T00:00:10+00:00", event_type="ORDER_CANCEL", oid="buy", side="BUY")
        conn.commit()
    row = report(journal.db_path)["entries"][0]
    assert row["lifecycle_state"] == "cancelled_unfilled"
    assert row["fill_notional"] == "0"
