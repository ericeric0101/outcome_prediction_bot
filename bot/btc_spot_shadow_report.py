"""Read-only BTC spot momentum baseline versus future spot-price report."""
from __future__ import annotations

import argparse
import json
import sqlite3
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any


EVENT = "BTC_SPOT_SHADOW_SNAPSHOT"


def _payload(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def build_report(db_path: str | Path, *, tolerance_sec: int = 15) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "strategy_events" not in tables:
            return {"status": "missing_strategy_events", "snapshots": 0}
        rows = conn.execute(
            "SELECT id, payload_json FROM strategy_events WHERE event_type=? ORDER BY id", (EVENT,),
        ).fetchall()
    finally:
        conn.close()
    points = []
    for event_id, raw in rows:
        p = _payload(raw)
        try:
            ts, mid = int(p["snapshot_timestamp_ms"]), float(p["mid"])
            if ts > 0 and mid > 0 and p.get("read_only") is True:
                context = p.get("perp_context") or {}
                points.append({"id": int(event_id), "ts": ts, "mid": mid,
                               "perp_mid": _float(p.get("btc_perp_mid")),
                               "perp_mid_ts": _int(p.get("btc_perp_mid_observed_at_ms")),
                               "oi": _float(context.get("openInterest")),
                               "context_ts": _int(p.get("perp_context_observed_at_ms"))})
        except (KeyError, TypeError, ValueError):
            continue
    points.sort(key=lambda x: (x["ts"], x["id"]))
    times = [p["ts"] for p in points]
    results: dict[str, Any] = {}
    for horizon_min in (5, 15, 60):
        scored: dict[str, list[dict[str, float]]] = {"spot_5m_momentum": [], "perp_mid_5m_momentum": [], "perp_oi_5m_change": []}
        common_scored: dict[str, list[dict[str, float]]] = {name: [] for name in scored}
        for i, point in enumerate(points):
            past_index = bisect_right(times, point["ts"] - 300_000, 0, i) - 1
            if past_index < 0 or point["ts"] - 300_000 - points[past_index]["ts"] > tolerance_sec * 1000:
                continue
            target = point["ts"] + horizon_min * 60_000
            future_index = bisect_left(times, target, i + 1)
            if future_index >= len(points) or points[future_index]["ts"] - target > tolerance_sec * 1000:
                continue
            outcome = points[future_index]["mid"] / point["mid"] - 1.0
            if outcome == 0:
                continue
            predictors: dict[str, float | None] = {
                "spot_5m_momentum": point["mid"] / points[past_index]["mid"] - 1.0,
                "perp_mid_5m_momentum": (
                    point["perp_mid"] / points[past_index]["perp_mid"] - 1.0
                    if point["perp_mid"] and points[past_index]["perp_mid"]
                    and point["perp_mid_ts"] and points[past_index]["perp_mid_ts"]
                    and point["ts"] - point["perp_mid_ts"] <= 30_000
                    and points[past_index]["ts"] - points[past_index]["perp_mid_ts"] <= 30_000 else None
                ),
                "perp_oi_5m_change": (
                    point["oi"] / points[past_index]["oi"] - 1.0
                    if point["oi"] and points[past_index]["oi"] and point["context_ts"]
                    and points[past_index]["context_ts"]
                    and point["ts"] - point["context_ts"] <= 30_000
                    and points[past_index]["ts"] - points[past_index]["context_ts"] <= 30_000 else None
                ),
            }
            for name, signal in predictors.items():
                if signal is None or signal == 0:
                    continue
                row = {"forward_return": outcome, "signed_return": outcome if signal > 0 else -outcome}
                scored[name].append(row)
            if all(predictors[name] is not None and predictors[name] != 0 for name in predictors):
                for name, signal in predictors.items():
                    common_scored[name].append({
                        "forward_return": outcome,
                        "signed_return": outcome if signal > 0 else -outcome,
                    })

        def _metrics(items: list[dict[str, float]]) -> dict[str, Any]:
            return {
                "n": len(items),
                "coverage_pct_of_snapshots": round(100 * len(items) / len(points), 2) if points else 0.0,
                "direction_hit_rate_pct": round(100 * sum(x["signed_return"] > 0 for x in items) / len(items), 2) if items else None,
                "mean_forward_spot_return_pct": round(100 * mean(x["forward_return"] for x in items), 5) if items else None,
                "mean_signed_return_pct": round(100 * mean(x["signed_return"] for x in items), 5) if items else None,
            }

        results[f"{horizon_min}m"] = {
            "per_predictor_complete_cases": {
                name: _metrics(items) for name, items in scored.items()
            },
            "head_to_head_common_cohort": {
                "n": len(next(iter(common_scored.values()))) if common_scored else 0,
                "predictors": {name: _metrics(items) for name, items in common_scored.items()},
            },
        }
    days = len({datetime.fromtimestamp(p["ts"] / 1000, tz=timezone.utc).date() for p in points})
    return {"status": "insufficient_data" if len(points) < 10_000 or days < 15 else "exploratory",
            "event_type": EVENT, "snapshots": len(points), "utc_days": days,
            "method": "5m spot/perp-mid/OI-change sign vs future spot mid return; no interpolation; first snapshot at/after horizon within tolerance; descriptive, not OOS validation",
            "tolerance_sec": tolerance_sec, "horizons": results,
            "authority": {"shadow_only": True, "live_authority": False, "execution_enabled": False}}


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--tolerance-sec", type=int, default=15)
    args = parser.parse_args()
    print(json.dumps(build_report(args.db, tolerance_sec=args.tolerance_sec), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
