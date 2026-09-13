"""Read-only multivariate tail diagnostic; deliberately not an ML trainer."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    path = Path(db_path)
    result: dict[str, Any] = {
        "report": "outcome_multivariate_tail_diagnostic", "schema_version": 1, "period": period,
        "live_authority": False, "episodes": [], "feature_presence": {}, "blockers": [],
        "known_case_lookup": {"2437": "matched by outcome_id when available", "2639": "matched by outcome_id when available"},
    }
    if not path.exists():
        result["blockers"] = ["journal_missing"]
        return result
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "strategy_events" not in tables:
            result["blockers"] = ["strategy_events_missing"]
            return result
        rows = conn.execute(
            """SELECT ts,event_type,payload_json FROM strategy_events
               WHERE event_type IN ('OUTCOME_MARKET_RISK_MONITOR_SHADOW','OUTCOME_CRASH_CIRCUIT_SHADOW',
                                    'OUTCOME_HOLDING_PATH_OBSERVATION') ORDER BY id"""
        ).fetchall()
    episodes: dict[str, dict[str, Any]] = {}
    for ts, event_type, raw in rows:
        payload = _payload(raw)
        if payload.get("period") != period:
            continue
        lifecycle = str(payload.get("entry_lifecycle_id") or "")
        if not lifecycle:
            continue
        row = episodes.setdefault(lifecycle, {"entry_lifecycle_id": lifecycle, "first_ts": ts, "observations": 0})
        row["observations"] += 1
        row.update({key: payload.get(key) for key in (
            "outcome_id", "coin", "time_left_sec", "current_time_left_sec", "holding_age_sec",
            "spot_strike_bps", "mark_return_bps", "oi_return_bps", "oi_age_ms", "regime_state",
            "reversal_state", "top3_depth", "top3_bid_depth", "spread_bps", "executable_return_pct",
        ) if payload.get(key) is not None})
        state = str(payload.get("state") or payload.get("research_state") or "")
        if state in {"RISK_COMPRESSION_SHADOW", "HARD_CAPITAL_PROTECTION_SHADOW", "RAPID_DRAWDOWN_RESEARCH"}:
            row.setdefault("first_risk_ts", ts)
            row.setdefault("first_risk_state", state)
    result["episodes"] = sorted(episodes.values(), key=lambda item: str(item["first_ts"]))
    counts: Counter[str] = Counter()
    for row in result["episodes"]:
        for feature in ("spot_strike_bps", "mark_return_bps", "oi_return_bps", "regime_state", "spread_bps", "executable_return_pct"):
            if row.get(feature) is not None:
                counts[feature] += 1
    result["feature_presence"] = dict(counts)
    result["episode_count"] = len(result["episodes"])
    result["blockers"] = [
        "diagnostic_only_no_threshold_optimization",
        "requires_lifecycle_bound_recovery_and_realized_pnl_join_for_counterfactual_ev",
    ]
    result["limits"] = [
        "No candidate rule is promoted from individual tails or snapshot counts.",
        "Market-risk monitor data is read-only and cannot call exchange execution.",
    ]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome multivariate tail diagnostic")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
