"""Read-only timing report for bounded Outcome emergency-exit decisions."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _epoch(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "median": None, "p75": None, "p90": None, "p95": None, "max": None}
    ordered = sorted(values)
    def percentile(percent: float) -> float:
        index = min(len(ordered) - 1, int((len(ordered) - 1) * percent))
        return round(ordered[index], 3)
    return {"count": len(values), "median": round(float(median(values)), 3), "p75": percentile(.75),
            "p90": percentile(.90), "p95": percentile(.95), "max": round(ordered[-1], 3)}


def report(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path)
    base: dict[str, Any] = {
        "report": "outcome_exit_latency", "schema_version": 1, "live_authority": False,
        "rows": [], "segments_ms": {}, "blockers": [],
        "comparison": {"known_2437_minus5_to_minus10_window_sec": 11.0},
    }
    if not path.exists():
        base["blockers"] = ["journal_missing"]
        return base
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "strategy_events" not in tables:
            base["blockers"] = ["strategy_events_missing"]
            return base
        rows = conn.execute(
            """SELECT ts,event_type,payload_json FROM strategy_events
               WHERE event_type IN ('OUTCOME_FAST_FAILURE_EXIT_DECISION','OUTCOME_EMERGENCY_EXIT_DECISION',
                                    'OUTCOME_EXIT_LIFECYCLE') ORDER BY id"""
        ).fetchall()
    decisions: dict[tuple[int, str], list[tuple[float, str, dict[str, Any]]]] = {}
    lifecycle: dict[tuple[int, str], list[tuple[float, dict[str, Any]]]] = {}
    for raw_ts, kind, raw in rows:
        ts, payload = _epoch(raw_ts), _payload(raw)
        try:
            key = int(payload["outcome_id"]), str(payload["coin"])
        except (KeyError, ValueError, TypeError):
            continue
        if ts is None:
            continue
        if kind == "OUTCOME_EXIT_LIFECYCLE":
            lifecycle.setdefault(key, []).append((ts, payload))
        elif payload.get("action") == "EXECUTE":
            decisions.setdefault(key, []).append((ts, str(kind), payload))
    segment_values: dict[str, list[float]] = {}
    for key, entries in decisions.items():
        for decision_ts, kind, payload in entries:
            following = [row for row in lifecycle.get(key, []) if row[0] >= decision_ts]
            marks: dict[str, float] = {"risk_detected_ts": decision_ts}
            inventory_reduced = False
            inventory_flat = False
            for ts, event in following:
                timing = event.get("execution_timing")
                if isinstance(timing, dict):
                    for name in (
                        "risk_detected_ts", "cancel_requested_ts", "cancel_confirmed_ts",
                        "fresh_book_ready_ts", "ioc_plan_ready_ts", "durable_intent_committed_ts",
                        "ioc_submit_ts", "ack_ts", "first_fill_ts", "inventory_confirmed_ts", "complete_fill_ts",
                    ):
                        try:
                            if name in timing:
                                # The decision event is only the controller's
                                # audit point.  A controller-provided risk
                                # detection timestamp is the authoritative
                                # start of the end-to-end execution chain.
                                if name == "risk_detected_ts":
                                    marks[name] = float(timing[name])
                                else:
                                    marks.setdefault(name, float(timing[name]))
                        except (TypeError, ValueError):
                            pass
                state = str(event.get("state") or "")
                if state == "EMERGENCY_CANCEL_SUBMITTED": marks.setdefault("cancel_requested_ts", ts)
                elif state == "EMERGENCY_EXIT_SUBMITTED": marks.setdefault("ack_ts", ts)
                elif state in {"CLOSED", "EMERGENCY_RESIDUAL"}:
                    marks.setdefault("complete_fill_or_reconcile_ts", ts)
                    # Old rows lack this field, so they remain evidence of a
                    # reconciliation but must not be promoted as reduction.
                    inventory_reduced = inventory_reduced or event.get("inventory_reduced") is True
                    inventory_flat = inventory_flat or event.get("inventory_flat") is True
            row: dict[str, Any] = {"outcome_id": key[0], "coin": key[1], "decision_event": kind, **marks}
            row["inventory_reduced"] = inventory_reduced
            row["inventory_flat"] = inventory_flat
            ordered_names = [
                "risk_detected_ts", "cancel_requested_ts", "cancel_confirmed_ts", "fresh_book_ready_ts",
                "ioc_plan_ready_ts", "durable_intent_committed_ts", "ioc_submit_ts", "ack_ts",
                "inventory_confirmed_ts", "complete_fill_ts", "complete_fill_or_reconcile_ts",
            ]
            for earlier, later in zip(ordered_names, ordered_names[1:]):
                if earlier in marks and later in marks:
                    ms = (marks[later] - marks[earlier]) * 1000
                    label = f"{earlier}_to_{later}"
                    row[label] = round(ms, 3)
                    segment_values.setdefault(label, []).append(ms)
            base["rows"].append(row)
    base["segments_ms"] = {name: _summary(values) for name, values in segment_values.items()}
    decision_to_ack = [
        (row["ack_ts"] - row["risk_detected_ts"]) * 1000 for row in base["rows"]
        if "ack_ts" in row and "risk_detected_ts" in row
    ]
    decision_to_inventory = [
        (row["inventory_confirmed_ts"] - row["risk_detected_ts"]) * 1000 for row in base["rows"]
        if row.get("inventory_reduced") and "inventory_confirmed_ts" in row and "risk_detected_ts" in row
    ]
    decision_to_flat = [
        (row["complete_fill_ts"] - row["risk_detected_ts"]) * 1000 for row in base["rows"]
        if row.get("inventory_flat") and "complete_fill_ts" in row and "risk_detected_ts" in row
    ]
    base["decision_to_exchange_ack_ms"] = _summary(decision_to_ack)
    base["decision_to_confirmed_inventory_reduction_ms"] = _summary(decision_to_inventory)
    base["decision_to_confirmed_flat_ms"] = _summary(decision_to_flat)
    if not decision_to_inventory:
        base["blockers"].append("no_confirmed_inventory_reduction_chain_yet")
    if not decision_to_flat:
        base["blockers"].append("no_confirmed_flat_exit_chain_yet")
    elif float(base["decision_to_confirmed_flat_ms"]["p90"] or 0) > 11_000:
        base["blockers"].append("p90_exceeds_2437_11_second_risk_window")
    base["limits"] = [
        "Historical rows without an EXECUTE decision or matching lifecycle state are not inferred.",
        "This report never changes cancellation, durable intent, ambiguity fencing, or depth protection.",
    ]
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome emergency-exit latency report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    print(json.dumps(report(parser.parse_args().db), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
