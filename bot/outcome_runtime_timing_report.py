"""Read-only, bounded attribution report for Outcome runtime latency."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from statistics import median
from typing import Any


_STAGES = (
    "total_ms", "account_recovery_ms", "exit_recovery_ms", "exit_continuations_ms",
    "fill_sync_ms", "holding_before_stream_ms", "stream_gate_ms",
    "holding_after_stream_ms", "entry_preflight_ms", "lifecycle_lookup_ms",
    "entry_gate_journal_ms", "fee_read_ms", "research_shadow_ms", "book_request_ms",
    "target_policy_ms", "risk_gate_ms", "portfolio_guard_ms", "entry_submit_ms",
    "journal_write_ms", "unattributed_runtime_ms",
)


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "sum_ms": 0.0, "median_ms": None, "p90_ms": None, "max_ms": None}
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int((len(ordered) - 1) * .90))]
    return {
        "count": len(ordered), "sum_ms": round(sum(ordered), 3),
        "median_ms": round(float(median(ordered)), 3), "p90_ms": round(p90, 3),
        "max_ms": round(ordered[-1], 3),
    }


def report(db_path: str | Path, *, recent_event_limit: int = 10_000) -> dict[str, Any]:
    """Summarise a bounded primary-key window without mutating the journal."""
    if recent_event_limit <= 0:
        raise ValueError("recent_event_limit must be positive")
    path = Path(db_path)
    base: dict[str, Any] = {
        "report": "outcome_runtime_timing", "schema_version": 1, "live_authority": False,
        "recent_event_limit": recent_event_limit, "rows_scanned": 0, "valid_timing_rows": 0,
        "id_window": None, "stages_ms": {}, "account_endpoints_ms": {}, "sdk_commands_ms": {},
        "sdk_command_steps_ms": {},
        "blockers": [],
        "limits": [
            "Reads only OUTCOME_RUNTIME_TIMING in a bounded primary-key window.",
            "Does not change account reconciliation, fill sync, order mutation, or journal state.",
        ],
    }
    if not path.exists():
        base["blockers"].append("journal_missing")
        return base
    stage_values: dict[str, list[float]] = {name: [] for name in _STAGES}
    account_values: dict[str, list[float]] = {}
    sdk_values: dict[str, list[float]] = {}
    sdk_step_values: dict[str, dict[str, list[float]]] = {}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "strategy_events" not in tables:
            base["blockers"].append("strategy_events_missing")
            return base
        latest_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM strategy_events").fetchone()[0])
        first_id = max(1, latest_id - recent_event_limit + 1)
        base["id_window"] = {"first_id": first_id, "latest_id": latest_id}
        cursor = conn.execute(
            """SELECT id,payload_json FROM strategy_events
               WHERE event_type='OUTCOME_RUNTIME_TIMING' AND id>=? ORDER BY id""",
            (first_id,),
        )
        for _, raw in cursor:
            base["rows_scanned"] += 1
            payload = _payload(raw)
            if not payload:
                continue
            base["valid_timing_rows"] += 1
            for name in _STAGES:
                value = _number(payload.get(name))
                if value is not None:
                    stage_values[name].append(value)
            account = payload.get("account_read_timing")
            if isinstance(account, dict):
                for name, details in account.items():
                    if not isinstance(details, dict):
                        continue
                    value = _number(details.get("elapsed_ms"))
                    if value is not None:
                        account_values.setdefault(str(name), []).append(value)
            requests = payload.get("sdk_requests")
            if isinstance(requests, list):
                for request in requests:
                    if not isinstance(request, dict):
                        continue
                    command = str(request.get("command") or request.get("gateway_command") or "unknown")
                    value = _number(request.get("python_round_trip_ms"))
                    if value is not None:
                        sdk_values.setdefault(command, []).append(value)
                    # Step timings are emitted only by the persistent SDK
                    # sidecar.  Keep boolean diagnostics (for example a cache
                    # hit) out of millisecond summaries.
                    step_timing = request.get("sidecar_step_timing")
                    if isinstance(step_timing, dict):
                        for name, raw_value in step_timing.items():
                            if not str(name).endswith("_ms"):
                                continue
                            step_value = _number(raw_value)
                            if step_value is not None:
                                sdk_step_values.setdefault(command, {}).setdefault(str(name), []).append(step_value)
    base["stages_ms"] = {name: _summary(values) for name, values in stage_values.items() if values}
    base["account_endpoints_ms"] = {name: _summary(values) for name, values in sorted(account_values.items())}
    base["sdk_commands_ms"] = {name: _summary(values) for name, values in sorted(sdk_values.items())}
    base["sdk_command_steps_ms"] = {
        command: {name: _summary(values) for name, values in sorted(steps.items())}
        for command, steps in sorted(sdk_step_values.items())
    }
    if not base["valid_timing_rows"]:
        base["blockers"].append("no_runtime_timing_rows_in_id_window")
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only, bounded Outcome runtime timing report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--recent-event-limit", type=int, default=10_000)
    args = parser.parse_args()
    print(json.dumps(report(args.db, recent_event_limit=args.recent_event_limit), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
