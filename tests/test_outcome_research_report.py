import json
import sqlite3

from bot.outcome_research_report import p2_report, p3_report


def _snapshot(period, *, fee="unverified_excluded", evidence="unverified_conversion_cost_excluded"):
    return {"venue": "hyperliquid_outcome", "period": period, "buy_complete_set_cost": 9.9, "buy_complete_set_edge": 0.1, "sell_complete_set_proceeds": 10.1, "fee_status": fee, "fee_evidence": evidence}


def test_p2_report_is_period_isolated_and_blocks_unknown_conversion_cost(tmp_path):
    db = tmp_path / "journal.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE strategy_events (event_type TEXT, payload_json TEXT)")
        conn.execute("INSERT INTO strategy_events VALUES (?, ?)", ("OUTCOME_P2_PARITY_SNAPSHOT", json.dumps(_snapshot("1d"))))
    reports = p2_report(db, periods=("15m", "1d"), min_snapshots=1)
    assert reports[0].snapshot_count == 0
    assert reports[1].positive_buy_edge_count == 1
    assert not reports[1].ready
    assert "fee_or_conversion_cost_evidence_incomplete" in reports[1].blockers


def test_p2_report_never_reuses_1d_snapshots_for_15m(tmp_path):
    db = tmp_path / "journal.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE strategy_events (event_type TEXT, payload_json TEXT)")
        payload = _snapshot("1d", fee="verified_included", evidence="official")
        conn.execute("INSERT INTO strategy_events VALUES (?, ?)", ("OUTCOME_P2_PARITY_SNAPSHOT", json.dumps(payload)))
    report = p2_report(db, periods=("15m",), min_snapshots=1)[0]
    assert report.snapshot_count == 0 and not report.ready


def test_p3_report_requires_actual_maker_fills_and_positive_fee_adjusted_lcb(tmp_path):
    db = tmp_path / "journal.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE order_events (event_type TEXT, payload_json TEXT)")
        for _ in range(30):
            conn.execute("INSERT INTO order_events VALUES (?, ?)", ("FILL_MARKOUT", json.dumps({"actual_fill": True, "executable_quote": True, "counterfactual": False, "period": "1d", "horizon_sec": 10, "entry_regime_bucket": "bucket", "signed_markout_ps": 0.02, "fee_per_share": 0.001})))
    report = p3_report(db, periods=("1d",), min_actual_fills=30)[0]
    assert report.ready and report.actual_maker_fill_count == 30 and report.fee_adjusted_lcb95_per_share > 0


def test_p3_report_excludes_counterfactual_and_does_not_cross_periods(tmp_path):
    db = tmp_path / "journal.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE order_events (event_type TEXT, payload_json TEXT)")
        conn.execute("INSERT INTO order_events VALUES (?, ?)", ("FILL_MARKOUT", json.dumps({"actual_fill": False, "counterfactual": True, "period": "1d", "horizon_sec": 10})))
    report = p3_report(db, periods=("15m",), min_actual_fills=1)[0]
    assert report.period == "15m" and not report.ready and report.actual_maker_fill_count == 0
