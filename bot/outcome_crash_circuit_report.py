"""Read-only calibration report for crash-circuit shadow episodes."""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


HORIZONS_SEC = (300, 900, 1800, 3600)


def _payload(value: object) -> dict[str, Any]:
    try:
        item = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return item if isinstance(item, dict) else {}


def _epoch(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _decimal(value: object) -> Decimal | None:
    try:
        item = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return item if item.is_finite() else None


def _nearest(items: list[tuple[float, dict[str, Any]]], target: float, tolerance_sec: int) -> dict[str, Any] | None:
    times = [item[0] for item in items]
    index = bisect.bisect_left(times, target)
    candidates = items[max(0, index - 1): min(len(items), index + 2)]
    if not candidates:
        return None
    timestamp, payload = min(candidates, key=lambda item: abs(item[0] - target))
    return payload if abs(timestamp - target) <= tolerance_sec else None


def report(db_path: str | Path, *, period: str = "1d", tolerance_sec: int = 90) -> dict[str, Any]:
    path = Path(db_path)
    empty = {
        "report": "outcome_crash_circuit_shadow", "schema_version": 1, "period": period,
        "ws_observations": 0, "rapid_drawdown_episodes": 0,
        "ready_for_live": False, "live_authority": False,
        "blockers": ["crash_circuit_shadow_evidence_required", "no_live_crash_ioc_authority"],
    }
    if not path.exists():
        return empty
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "strategy_events" not in tables:
            return empty
        events = conn.execute(
            """SELECT ts,payload_json FROM strategy_events
               WHERE event_type='OUTCOME_CRASH_CIRCUIT_SHADOW' ORDER BY id"""
        ).fetchall()
        paths = conn.execute(
            """SELECT ts,payload_json FROM strategy_events
               WHERE event_type='OUTCOME_HOLDING_PATH_OBSERVATION' ORDER BY id"""
        ).fetchall()

    full_depth_paths: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    for raw_ts, raw in paths:
        timestamp, payload = _epoch(raw_ts), _payload(raw)
        lifecycle = str(payload.get("entry_lifecycle_id") or "")
        if timestamp is not None and lifecycle and payload.get("period") == period:
            full_depth_paths[lifecycle].append((timestamp, payload))
    for values in full_depth_paths.values():
        values.sort(key=lambda item: item[0])

    observations = 0
    episodes: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw_ts, raw in events:
        timestamp, payload = _epoch(raw_ts), _payload(raw)
        lifecycle = str(payload.get("entry_lifecycle_id") or "")
        episode_id = payload.get("research_episode_id")
        if timestamp is None or not lifecycle or payload.get("period") != period:
            continue
        observations += 1
        if payload.get("research_state") != "RAPID_DRAWDOWN_RESEARCH" or episode_id is None:
            continue
        try:
            key = lifecycle, int(episode_id)
        except (TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        future = full_depth_paths.get(lifecycle, [])
        valid = [
            _decimal(point.get("marketable_net_exit_vs_entry_pct"))
            for ts, point in future if ts >= timestamp and point.get("marketable_exit_full_inventory") is True
        ]
        valid = [value for value in valid if value is not None]
        outcomes: dict[str, str | None] = {}
        for horizon in HORIZONS_SEC:
            point = _nearest(future, timestamp + horizon, tolerance_sec)
            outcomes[str(horizon)] = (
                point.get("marketable_net_exit_vs_entry_pct")
                if point is not None and point.get("marketable_exit_full_inventory") is True else None
            )
        episodes.append({
            "entry_lifecycle_id": lifecycle, "outcome_id": payload.get("outcome_id"), "coin": payload.get("coin"),
            "episode_id": episode_id, "episode_start_ts": datetime.fromtimestamp(timestamp).astimezone().isoformat(),
            "trigger_features": {
                "ws_top_bid_gross_return_pct": payload.get("ws_top_bid_gross_return_pct"),
                "bid_velocity_bps": payload.get("bid_velocity_bps"),
                "top3_depth_ratio": payload.get("top3_depth_ratio"),
                "spot_strike_bps": payload.get("spot_strike_bps"), "mark_return_bps": payload.get("mark_return_bps"),
                "oi_return_bps": payload.get("oi_return_bps"), "oi_age_ms": payload.get("oi_age_ms"),
                "regime_state": payload.get("regime_state"), "reversal_state": payload.get("reversal_state"),
            },
            "later_full_depth_net_return_by_horizon_sec": outcomes,
            "later_min_full_depth_net_return_pct": str(min(valid)) if valid else None,
            "later_max_full_depth_net_return_pct": str(max(valid)) if valid else None,
            "later_recovered_to_cost": any(value >= 0 for value in valid),
            "limits": "Future outcomes use low-frequency fresh REST full-depth observations; no synthetic IOC fill is inferred.",
        })
    blockers = ["crash_circuit_shadow_evidence_required"]
    if len(episodes) < 20:
        blockers.append("fewer_than_20_rapid_drawdown_episodes")
    if len({row["outcome_id"] for row in episodes if row["outcome_id"] is not None}) < 5:
        blockers.append("fewer_than_5_daily_markets_with_crash_episodes")
    blockers.append("no_live_crash_ioc_authority")
    return {
        "report": "outcome_crash_circuit_shadow", "schema_version": 1, "period": period,
        "ws_observations": observations, "rapid_drawdown_episodes": len(episodes), "episodes": episodes,
        "ready_for_live": False, "live_authority": False, "blockers": blockers,
        "limits": [
            "RAPID_DRAWDOWN_RESEARCH is an analytical bucket only; it has no live stop-loss or IOC authority.",
            "Calibration must compare false-positive recoveries against continued-drawdown containment across unseen markets.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only crash-circuit shadow evidence report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--tolerance-sec", type=int, default=90)
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period, tolerance_sec=args.tolerance_sec), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
