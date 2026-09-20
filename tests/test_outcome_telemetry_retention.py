from datetime import datetime, timedelta, timezone
import sqlite3

from bot.outcome_telemetry_retention import (
    ALL_MIDS_COMPACT_SCOPE,
    PRUNABLE_RULES,
    RetentionRule,
    audit_database,
    prune_database,
    retention_enabled,
    utc_cutoff_iso,
)
from monitoring.trade_journal_db import TradeJournalDB


NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


def _event(db, event_type, payload, *, age_days=0):
    event_id = db.log_strategy_event("run", event_type, payload)
    ts = (NOW - timedelta(days=age_days)).isoformat()
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE strategy_events SET ts=? WHERE id=?", (ts, event_id))
        conn.commit()
    return event_id


def _events(db):
    with sqlite3.connect(db.db_path) as conn:
        return [row[0] for row in conn.execute("SELECT event_type FROM strategy_events ORDER BY id")]


def test_unknown_canonical_and_forward_shadow_events_are_never_allowlisted(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    _event(db, "UNKNOWN_FUTURE_EVENT", {}, age_days=90)
    _event(db, "OUTCOME_ENTRY_READINESS_SHADOW", {}, age_days=90)
    _event(db, "OUTCOME_STRUCTURAL_COLLAPSE_SHADOW", {}, age_days=90)
    _event(db, "OUTCOME_ORDER_INTENT", {}, age_days=90)
    db.log_order_event("run", "FILL_MARKOUT", payload={"horizon_sec": 30})
    result = prune_database(db.db_path, apply=True, enabled=True, now=NOW)
    assert result["applied"] is True
    assert set(_events(db)) == {
        "UNKNOWN_FUTURE_EVENT", "OUTCOME_ENTRY_READINESS_SHADOW",
        "OUTCOME_STRUCTURAL_COLLAPSE_SHADOW", "OUTCOME_ORDER_INTENT",
    }
    with sqlite3.connect(db.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM order_events WHERE event_type='FILL_MARKOUT'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM outcome_market_settlement_registry").fetchone()[0] == 0


def test_dry_run_and_disabled_retention_delete_nothing(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    _event(db, "OUTCOME_WS_ALL_MIDS", {"raw": {"recording_scope": ALL_MIDS_COMPACT_SCOPE}}, age_days=31)
    dry = prune_database(db.db_path, apply=False, enabled=True, now=NOW)
    disabled = prune_database(db.db_path, apply=True, enabled=False, now=NOW)
    assert dry["reason"] == "dry_run"
    assert disabled["reason"] == "retention_disabled"
    assert _events(db) == ["OUTCOME_WS_ALL_MIDS"]


def test_apply_prunes_only_old_allowlisted_compact_all_mids_in_batches(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    for _ in range(3):
        _event(db, "OUTCOME_WS_ALL_MIDS", {"raw": {"recording_scope": ALL_MIDS_COMPACT_SCOPE}}, age_days=31)
    _event(db, "OUTCOME_WS_ALL_MIDS", {"raw": {"recording_scope": ALL_MIDS_COMPACT_SCOPE}}, age_days=29)
    _event(db, "OUTCOME_WS_L2_BOOK", {"raw": {"recording_scope": ALL_MIDS_COMPACT_SCOPE}}, age_days=90)
    result = prune_database(db.db_path, apply=True, enabled=True, now=NOW, batch_size=1)
    compact = next(row for row in result["deleted"] if row["rule"] == "compact_all_mids_rolling_30d")
    assert compact["deleted_rows"] == 3
    assert _events(db) == ["OUTCOME_WS_ALL_MIDS", "OUTCOME_WS_L2_BOOK"]


def test_legacy_unscoped_all_mids_is_explicitly_prunable_but_current_scoped_is_not(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    _event(db, "OUTCOME_WS_ALL_MIDS", {"raw": {"mids": {"BTC": "1"}}}, age_days=1)
    _event(db, "OUTCOME_WS_ALL_MIDS", {"raw": {"recording_scope": ALL_MIDS_COMPACT_SCOPE}}, age_days=1)
    result = prune_database(db.db_path, apply=True, enabled=True, now=NOW)
    legacy = next(row for row in result["deleted"] if row["rule"] == "legacy_unscoped_all_mids_immediate")
    assert legacy["deleted_rows"] == 1
    assert _events(db) == ["OUTCOME_WS_ALL_MIDS"]


def test_audit_inventories_all_tables_and_only_all_mids_as_v1_candidate(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    _event(db, "OUTCOME_WS_ALL_MIDS", {"raw": {"recording_scope": ALL_MIDS_COMPACT_SCOPE}}, age_days=31)
    report = audit_database(db.db_path, now=NOW)
    assert report["tables"]["order_events"]["retention"] == "MUST_KEEP_FOREVER"
    assert report["tables"]["outcome_deribit_feature_rows"]["retention"] == "KEEP_LONG_TERM"
    assert report["unknown_default"] == "KEEP"
    assert {row["event_type"] for row in report["prune_candidates"]} == {"OUTCOME_WS_ALL_MIDS"}


def test_retention_configuration_defaults_off_and_cutoff_is_utc_safe():
    assert retention_enabled({}) is False
    assert retention_enabled({"OUTCOME_TELEMETRY_RETENTION_ENABLED": "1"}) is True
    assert utc_cutoff_iso(now=NOW, days=30) == "2026-08-21T12:00:00+00:00"


def test_cleanup_failure_rolls_back_before_it_can_harm_even_allowlisted_rows(tmp_path, monkeypatch):
    db = TradeJournalDB(tmp_path / "journal.db")
    _event(db, "OUTCOME_WS_ALL_MIDS", {"raw": {"recording_scope": ALL_MIDS_COMPACT_SCOPE}}, age_days=31)
    broken = RetentionRule("broken", "strategy_events", "OUTCOME_WS_ALL_MIDS", 30, "not valid sql", "test rollback")
    monkeypatch.setattr("bot.outcome_telemetry_retention.PRUNABLE_RULES", (PRUNABLE_RULES[0], broken))
    try:
        prune_database(db.db_path, apply=True, enabled=True, now=NOW)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("malformed allowlist predicate must fail")
    assert _events(db) == ["OUTCOME_WS_ALL_MIDS"]
