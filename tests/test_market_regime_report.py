import importlib.util
import json
import sqlite3
from pathlib import Path


def _load_report_module():
    path = Path(__file__).parents[1] / "scripts" / "market_regime_report.py"
    spec = importlib.util.spec_from_file_location("market_regime_report", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _event(conn, table, ts, event_type, payload, *, side=None):
    if table == "order_events":
        conn.execute(
            "INSERT INTO order_events (ts, run_id, event_type, side, payload_json) VALUES (?, 'run', ?, ?, ?)",
            (ts, event_type, side, json.dumps(payload)),
        )
    else:
        conn.execute(
            "INSERT INTO strategy_events (ts, run_id, event_type, payload_json) VALUES (?, 'run', ?, ?)",
            (ts, event_type, json.dumps(payload)),
        )


def test_report_excludes_legacy_markouts_and_requires_settlement(tmp_path, capsys, monkeypatch):
    module = _load_report_module()
    db_path = tmp_path / "journal.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE order_events (ts TEXT, run_id TEXT, event_type TEXT, side TEXT, payload_json TEXT)")
    conn.execute("CREATE TABLE strategy_events (ts TEXT, run_id TEXT, event_type TEXT, payload_json TEXT)")
    base = {
        "liquidity_class": "maker",
        "horizon_sec": 10,
        "signed_markout_ps": -0.02,
        "entry_is_weekend_utc": False,
    }
    _event(conn, "order_events", "2026-08-22T00:00:10+00:00", "FILL_MARKOUT", {**base, "slug": "legacy"}, side="BUY")
    _event(
        conn,
        "order_events",
        "2026-08-22T00:01:10+00:00",
        "FILL_MARKOUT",
        {**base, "slug": "settled", "markout_context_schema_version": 2},
        side="BUY",
    )
    _event(
        conn,
        "order_events",
        "2026-08-22T00:02:10+00:00",
        "FILL_MARKOUT",
        {**base, "slug": "open", "markout_context_schema_version": 2},
        side="BUY",
    )
    _event(conn, "strategy_events", "2026-08-22T00:15:00+00:00", "MARKET_SETTLEMENT", {"slug": "settled"})
    conn.commit()
    conn.close()

    monkeypatch.setattr("sys.argv", ["market_regime_report.py", "--db", str(db_path), "--min-samples", "2"])
    assert module.main() == 0
    report = json.loads(capsys.readouterr().out)

    summary = report["candidate_windows"]["12"]["10"]
    assert report["markout_context_schema_version"] == 2
    assert summary["markout_sample_count"] == 2
    assert summary["settled_sample_count"] == 1
    assert report["selection"]["reason"] == "insufficient_schema_v2_settled_samples"
