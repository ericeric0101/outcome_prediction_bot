"""Conservative, operator-invoked retention for Outcome journal telemetry.

The live runtime never imports this module.  Unknown tables/event families are
classified KEEP, and deletion is limited to this explicit allowlist.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ALL_MIDS_EVENT = "OUTCOME_WS_ALL_MIDS"
ALL_MIDS_COMPACT_SCOPE = "btc_and_active_outcome_only_v2"
DEFAULT_ALL_MIDS_DAYS = 30


@dataclass(frozen=True)
class RetentionRule:
    name: str
    table: str
    event_type: str
    retention_days: int
    predicate_sql: str
    rationale: str


# This is deliberately an allowlist, not a statement that all other raw data
# is disposable.  New event types cannot enter it without a code review.
PRUNABLE_RULES: tuple[RetentionRule, ...] = (
    RetentionRule(
        name="compact_all_mids_rolling_30d",
        table="strategy_events",
        event_type=ALL_MIDS_EVENT,
        retention_days=DEFAULT_ALL_MIDS_DAYS,
        predicate_sql="json_extract(payload_json, '$.raw.recording_scope')=?",
        rationale=("Compact BTC + active Outcome allMids snapshots have no repository reader, "
                   "while live consumers receive the WS callback directly.  Entry-readiness "
                   "and structural-collapse retain their compact derived results separately."),
    ),
    RetentionRule(
        name="legacy_unscoped_all_mids_immediate",
        table="strategy_events",
        event_type=ALL_MIDS_EVENT,
        retention_days=0,
        predicate_sql="json_extract(payload_json, '$.raw.recording_scope') IS NULL",
        rationale=("Pre-v2 allMids mirrored the entire venue map and is superseded by the compact "
                   "schema.  No current replay/model/report reads it; this rule never matches "
                   "the current writer's scoped payload."),
    ),
)


TABLE_CLASSIFICATIONS: dict[str, dict[str, str]] = {
    "strategy_runs": {"authority": "CANONICAL", "retention": "MUST_KEEP_FOREVER", "writer": "TradeJournalDB run lifecycle"},
    "order_events": {"authority": "CANONICAL", "retention": "MUST_KEEP_FOREVER", "writer": "OutcomeExecutionLedger / official fills"},
    "outcome_fill_registry": {"authority": "CANONICAL", "retention": "MUST_KEEP_FOREVER", "writer": "fill dedupe registry"},
    "outcome_realized_pnl_lots": {"authority": "CANONICAL", "retention": "MUST_KEEP_FOREVER", "writer": "canonical FIFO reconciliation"},
    "outcome_market_settlement_registry": {"authority": "CANONICAL", "retention": "MUST_KEEP_FOREVER", "writer": "official settlement reconciliation"},
    "outcome_settlement_fill_cursor": {"authority": "CANONICAL", "retention": "MUST_KEEP_FOREVER", "writer": "settlement worker cursor"},
    "outcome_settlement_payout_fills": {"authority": "CANONICAL", "retention": "MUST_KEEP_FOREVER", "writer": "official payout fill capture"},
    "outcome_settlement_status": {"authority": "CANONICAL", "retention": "MUST_KEEP_FOREVER", "writer": "settlement worker"},
    "strategy_events": {"authority": "MIXED_BY_EVENT_FAMILY", "retention": "ALLOWLIST_ONLY", "writer": "runtime, WS recorder, research workers"},
    "outcome_p3_quote_index": {"authority": "RESEARCH_REPLAY", "retention": "CANDIDATE_FOR_ROLLING_RETENTION", "writer": "P3 pipeline (existing bounded prune)"},
    "outcome_p3_pending_fills": {"authority": "CANONICAL", "retention": "CANDIDATE_FOR_ROLLING_RETENTION", "writer": "P3 pipeline; expires after final horizon"},
    "outcome_wide_spread_candidates": {"authority": "DERIVED_DURABLE", "retention": "KEEP_LONG_TERM", "writer": "wide-spread tracker"},
    "outcome_wide_spread_candidate_paths": {"authority": "DERIVED_DURABLE", "retention": "KEEP_LONG_TERM", "writer": "wide-spread tracker"},
    "binance_oi_observations": {"authority": "RESEARCH_REPLAY", "retention": "KEEP_LONG_TERM", "writer": "Binance OI collector; live S0 reads recent rows"},
    "outcome_oi_feature_rows": {"authority": "DERIVED_DURABLE", "retention": "KEEP_LONG_TERM", "writer": "derived feature worker / training"},
    "outcome_deribit_feature_rows": {"authority": "DERIVED_DURABLE", "retention": "KEEP_LONG_TERM", "writer": "Deribit feature builder / walk-forward"},
    "hyperliquid_perp_context_observations": {"authority": "RESEARCH_REPLAY", "retention": "KEEP_LONG_TERM", "writer": "WS native-perp comparator"},
    "outcome_oi_fill_feature_rows": {"authority": "DERIVED_DURABLE", "retention": "KEEP_LONG_TERM", "writer": "fill feature builder / calibration"},
}


def retention_enabled(environ: dict[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return source.get("OUTCOME_TELEMETRY_RETENTION_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def utc_cutoff_iso(*, now: datetime, days: int) -> str:
    """Return a timezone-safe UTC ISO cutoff for a rule."""
    if now.tzinfo is None:
        raise ValueError("retention now must be timezone-aware")
    return (now.astimezone(timezone.utc) - timedelta(days=int(days))).isoformat()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def _database_tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def _rule_summary(conn: sqlite3.Connection, rule: RetentionRule, *, now: datetime) -> dict[str, Any]:
    cutoff = utc_cutoff_iso(now=now, days=rule.retention_days)
    params: tuple[Any, ...]
    if "?" in rule.predicate_sql:
        params = (rule.event_type, cutoff, ALL_MIDS_COMPACT_SCOPE)
    else:
        params = (rule.event_type, cutoff)
    query = f"""
        SELECT COUNT(*), MIN(ts), MAX(ts), COALESCE(SUM(length(payload_json)), 0)
        FROM strategy_events
        WHERE event_type=? AND ts<? AND {rule.predicate_sql}
    """
    count, oldest, newest, payload_bytes = conn.execute(query, params).fetchone()
    return {
        "rule": rule.name, "table": rule.table, "event_type": rule.event_type,
        "retention_days": rule.retention_days, "cutoff_utc": cutoff,
        "eligible_rows": int(count), "oldest_ts": oldest, "newest_ts": newest,
        "approx_payload_bytes": int(payload_bytes), "rationale": rule.rationale,
        "apply_requires": "OUTCOME_TELEMETRY_RETENTION_ENABLED=1 and --apply",
    }


def audit_database(path: str | Path, *, now: datetime | None = None,
                   include_prune_estimates: bool = False) -> dict[str, Any]:
    """Read-only inventory.

    Exact JSON-predicate byte counts can require reading large legacy payloads,
    so the normal audit intentionally avoids that live-safe anti-pattern.
    ``--dry-run`` explicitly opts into the slower eligibility measurement.
    """
    db_path = Path(path)
    current = now or datetime.now(timezone.utc)
    with _connect_readonly(db_path) as conn:
        tables = _database_tables(conn)
        inventory: dict[str, Any] = {}
        for table in sorted(tables):
            row_count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            inventory[table] = {"rows": row_count, **TABLE_CLASSIFICATIONS.get(table, {
                "authority": "UNKNOWN_DEPENDENCY", "retention": "DO_NOT_TOUCH_UNTIL_DEPENDENCY_RESOLVED",
                "writer": "not classified by V1",
            })}
        event_rows = conn.execute(
            "SELECT event_type, COUNT(*) FROM strategy_events GROUP BY event_type ORDER BY event_type"
        ).fetchall() if "strategy_events" in tables else []
        events = []
        for event_type, count in event_rows:
            classification = classify_strategy_event(str(event_type))
            events.append({"event_type": str(event_type), "rows": int(count), **classification})
        return {
            "schema": "outcome_db_retention_audit_v1", "db": str(db_path),
            "tables": inventory, "strategy_event_families": events,
            "prune_candidates": ([_rule_summary(conn, rule, now=current) for rule in PRUNABLE_RULES]
                                 if include_prune_estimates else [
                {"rule": rule.name, "table": rule.table, "event_type": rule.event_type,
                 "retention_days": rule.retention_days, "cutoff_utc": utc_cutoff_iso(now=current, days=rule.retention_days),
                 "eligible_rows": None, "approx_payload_bytes": None,
                 "measurement": "run --dry-run during maintenance for exact eligibility", "rationale": rule.rationale}
                for rule in PRUNABLE_RULES]),
            "unknown_default": "KEEP",
        }


def classify_strategy_event(event_type: str) -> dict[str, str]:
    """Classify known families; intentionally conservative for all others."""
    if event_type == ALL_MIDS_EVENT:
        return {"authority": "HIGH_FREQUENCY_TELEMETRY", "retention": "CANDIDATE_FOR_ROLLING_RETENTION"}
    if event_type in {"OUTCOME_ENTRY_READINESS_SHADOW", "OUTCOME_STRUCTURAL_COLLAPSE_SHADOW"}:
        return {"authority": "DERIVED_DURABLE", "retention": "KEEP_LONG_TERM"}
    if event_type in {"OUTCOME_WS_L2_BOOK", "OUTCOME_WS_TRADES", "OUTCOME_P2_PARITY_SNAPSHOT", "DERIBIT_FEATURE_SNAPSHOT"}:
        return {"authority": "RESEARCH_REPLAY", "retention": "KEEP_LONG_TERM"}
    if event_type.startswith("OUTCOME_ORDER_INTENT") or event_type.startswith("OUTCOME_EXIT_LIFECYCLE") or event_type.startswith("OUTCOME_ENTRY_LIFECYCLE"):
        return {"authority": "CANONICAL", "retention": "MUST_KEEP_FOREVER"}
    if event_type in {"MARKET_SETTLEMENT", "OUTCOME_REALIZED_PNL_RECONCILED", "OUTCOME_LOSS_EXIT_CONFIRMED", "OUTCOME_LOSS_REENTRY_SUBMITTED"}:
        return {"authority": "CANONICAL", "retention": "MUST_KEEP_FOREVER"}
    if event_type in {"OUTCOME_ENTRY_ADMISSION_DECISION", "OUTCOME_ENTRY_GATE_DECISION", "OUTCOME_HOLDING_PATH_OBSERVATION", "OUTCOME_MARKET_RISK_MONITOR_SHADOW", "OUTCOME_CRASH_CIRCUIT_SHADOW", "OUTCOME_POST_FILL_QUALITY_SHADOW", "OUTCOME_TREND_CONTINUATION_PATH"}:
        return {"authority": "DERIVED_DURABLE", "retention": "KEEP_LONG_TERM"}
    return {"authority": "UNKNOWN_DEPENDENCY", "retention": "DO_NOT_TOUCH_UNTIL_DEPENDENCY_RESOLVED"}


def prune_database(path: str | Path, *, apply: bool, enabled: bool, now: datetime | None = None,
                   batch_size: int = 1_000) -> dict[str, Any]:
    """Delete only allowlisted rows in bounded batches; never VACUUM."""
    if not apply:
        return {"applied": False, "reason": "dry_run", "audit": audit_database(path, now=now, include_prune_estimates=True)}
    if not enabled:
        return {"applied": False, "reason": "retention_disabled", "audit": audit_database(path, now=now)}
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    db_path, current = Path(path), now or datetime.now(timezone.utc)
    deleted: list[dict[str, Any]] = []
    # Exclusive is intentional: maintenance refuses to compete with a live
    # bot, and no automatic scheduler invokes this function.
    conn = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("BEGIN EXCLUSIVE")
        for rule in PRUNABLE_RULES:
            cutoff = utc_cutoff_iso(now=current, days=rule.retention_days)
            total = 0
            while True:
                if "?" in rule.predicate_sql:
                    params: tuple[Any, ...] = (rule.event_type, cutoff, ALL_MIDS_COMPACT_SCOPE, int(batch_size))
                else:
                    params = (rule.event_type, cutoff, int(batch_size))
                cursor = conn.execute(f"""
                    DELETE FROM strategy_events WHERE id IN (
                        SELECT id FROM strategy_events
                        WHERE event_type=? AND ts<? AND {rule.predicate_sql}
                        ORDER BY id LIMIT ?
                    )
                """, params)
                total += max(0, cursor.rowcount)
                if cursor.rowcount < batch_size:
                    break
            deleted.append({"rule": rule.name, "event_type": rule.event_type, "deleted_rows": total, "cutoff_utc": cutoff})
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"applied": True, "deleted": deleted, "vacuum": "never automatic"}


def json_report(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
