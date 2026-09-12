"""Read-only evidence for time-left-aware thesis-failure exits.

This report deliberately compares *observed* recovery and continued-loss paths
after a reversal or rapid-drawdown signal.  It does not synthesize an IOC,
pretend a later manual close was a bot action, or grant an exit controller any
authority.  The compact lifecycle keys are also the future join points for the
existing B multi-target model; they are not a second model.
"""
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
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _epoch(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _nearest(items: list[tuple[float, dict[str, Any]]], target: float, tolerance: int) -> dict[str, Any] | None:
    times = [row[0] for row in items]
    index = bisect.bisect_left(times, target)
    candidates = items[max(0, index - 1):min(len(items), index + 2)]
    if not candidates:
        return None
    timestamp, payload = min(candidates, key=lambda row: abs(row[0] - target))
    return payload if abs(timestamp - target) <= tolerance else None


def _time_bucket(value: object) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if seconds < 2 * 3600:
        return "under_2h"
    if seconds < 6 * 3600:
        return "2h_to_6h"
    if seconds < 12 * 3600:
        return "6h_to_12h"
    return "12h_plus"


def report(db_path: str | Path, *, period: str = "1d", tolerance_sec: int = 90) -> dict[str, Any]:
    """Produce one first-signal episode per immutable entry lifecycle."""
    path = Path(db_path)
    result: dict[str, Any] = {
        "report": "outcome_thesis_failure_shadow", "schema_version": 1, "period": period,
        "episodes": [], "episode_count": 0, "ready_for_live": False, "live_authority": False,
        "blockers": ["thesis_failure_shadow_evidence_required", "no_live_time_left_exit_authority"],
    }
    if not path.exists():
        return result
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "strategy_events" not in tables:
            return result
        strategies = conn.execute(
            """SELECT ts,event_type,payload_json FROM strategy_events
               WHERE event_type IN ('OUTCOME_REVERSAL_SHADOW_DECISION','OUTCOME_CRASH_CIRCUIT_SHADOW',
                                    'OUTCOME_HOLDING_PATH_OBSERVATION') ORDER BY id"""
        ).fetchall()
        close_rows = conn.execute(
            """SELECT open_trade_id,SUM(CAST(cost_usdc AS REAL)),SUM(CAST(realized_net_usdc AS REAL))
               FROM outcome_realized_pnl_lots GROUP BY open_trade_id"""
        ).fetchall() if "outcome_realized_pnl_lots" in tables else []
        order_rows = conn.execute(
            """SELECT ts,instrument_id,payload_json FROM order_events
               WHERE event_type='ORDER_FILLED' AND side='SELL' ORDER BY id"""
        ).fetchall() if "order_events" in tables else []

    paths: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    candidates: dict[str, list[tuple[float, str, dict[str, Any]]]] = defaultdict(list)
    for raw_ts, event_type, raw in strategies:
        timestamp, payload = _epoch(raw_ts), _payload(raw)
        lifecycle = str(payload.get("entry_lifecycle_id") or "")
        if timestamp is None or not lifecycle or payload.get("period") != period:
            continue
        if event_type == "OUTCOME_HOLDING_PATH_OBSERVATION":
            paths[lifecycle].append((timestamp, payload))
        elif event_type == "OUTCOME_REVERSAL_SHADOW_DECISION" and payload.get("state") == "REVERSAL_CONFIRMED":
            candidates[lifecycle].append((timestamp, "reversal_confirmed", payload))
        elif event_type == "OUTCOME_CRASH_CIRCUIT_SHADOW" and payload.get("research_state") == "RAPID_DRAWDOWN_RESEARCH":
            candidates[lifecycle].append((timestamp, "rapid_drawdown", payload))
    for observations in paths.values():
        observations.sort(key=lambda row: row[0])
    closed = {str(trade): (_decimal(cost), _decimal(pnl)) for trade, cost, pnl in close_rows}
    sells: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    for raw_ts, coin, raw in order_rows:
        timestamp = _epoch(raw_ts)
        if timestamp is not None:
            sells[str(coin)].append((timestamp, _payload(raw)))

    episodes: list[dict[str, Any]] = []
    for lifecycle, signals in sorted(candidates.items()):
        # The first observed warning is the only admissible counterfactual
        # decision point.  Later copies describe the same ongoing episode.
        signal_ts, kind, signal = min(signals, key=lambda row: row[0])
        future = paths.get(lifecycle, [])
        at_signal = _nearest(future, signal_ts, tolerance_sec)
        full_depth = [
            (_decimal(payload.get("marketable_net_exit_vs_entry_pct")), timestamp, payload)
            for timestamp, payload in future
            if timestamp >= signal_ts and payload.get("marketable_exit_full_inventory") is True
        ]
        full_depth = [(value, timestamp, payload) for value, timestamp, payload in full_depth if value is not None]
        horizon_values: dict[str, str | None] = {}
        for horizon in HORIZONS_SEC:
            point = _nearest(future, signal_ts + horizon, tolerance_sec)
            horizon_values[str(horizon)] = (
                point.get("marketable_net_exit_vs_entry_pct")
                if point is not None and point.get("marketable_exit_full_inventory") is True else None
            )
        metadata = at_signal or signal
        trade_id = str(metadata.get("entry_trade_id") or "")
        cost, final_pnl = closed.get(trade_id, (None, None))
        final_return = final_pnl / cost if cost is not None and final_pnl is not None and cost > 0 else None
        coin = str(metadata.get("coin") or "")
        manual_or_unknown = None
        for sell_ts, sell in sells.get(coin, []):
            if sell_ts >= signal_ts:
                manual_or_unknown = sell.get("execution_origin")
                break
        target = _decimal(metadata.get("entry_target_return_pct"))
        future_returns = [value for value, _, _ in full_depth]
        episodes.append({
            "entry_lifecycle_id": lifecycle, "entry_trade_id": trade_id or None,
            "outcome_id": metadata.get("outcome_id"), "coin": coin or None,
            "first_signal_ts": datetime.fromtimestamp(signal_ts).astimezone().isoformat(),
            "trigger_kind": kind, "time_left_sec": metadata.get("time_left_sec"),
            "time_left_bucket": _time_bucket(metadata.get("time_left_sec")),
            "holding_age_sec": metadata.get("holding_age_sec"),
            "entry_side_index": metadata.get("entry_side_index"), "entry_tier": metadata.get("entry_tier"),
            "entry_target_return_pct": metadata.get("entry_target_return_pct"),
            "signal_features": {
                "reversal_state": signal.get("state") or signal.get("reversal_state"),
                "bid_velocity_bps": signal.get("bid_velocity_bps"),
                "top3_depth_ratio": signal.get("top3_depth_ratio"),
                "spot_strike_bps": signal.get("spot_strike_bps"),
                "mark_return_bps": signal.get("mark_return_bps"),
                "oi_return_bps": signal.get("oi_return_bps"),
                "regime_state": signal.get("regime_state"),
            },
            "signal_full_depth_net_return_pct": (
                at_signal.get("marketable_net_exit_vs_entry_pct")
                if at_signal is not None and at_signal.get("marketable_exit_full_inventory") is True else None
            ),
            "later_full_depth_net_return_by_horizon_sec": horizon_values,
            "later_min_full_depth_net_return_pct": str(min(future_returns)) if future_returns else None,
            "later_max_full_depth_net_return_pct": str(max(future_returns)) if future_returns else None,
            "later_recovered_to_cost": any(value >= 0 for value in future_returns),
            "later_reached_entry_target": bool(target is not None and any(value >= target for value in future_returns)),
            "final_realized_return_pct": str(final_return) if final_return is not None else None,
            "first_later_sell_execution_origin": manual_or_unknown,
            "limits": "Observed later path is not a synthetic IOC or causal counterfactual; external_manual_or_unknown closes are retained as audit facts only.",
        })
    result["episodes"] = episodes
    result["episode_count"] = len(episodes)
    if len(episodes) < 20:
        result["blockers"].append("fewer_than_20_lifecycle_bound_thesis_failure_episodes")
    if len({row["outcome_id"] for row in episodes if row["outcome_id"] is not None}) < 5:
        result["blockers"].append("fewer_than_5_daily_markets_with_episodes")
    result["future_b_model_join"] = {
        "single_model_family": "outcome_active_multi_target_model",
        "join_key": "entry_lifecycle_id",
        "planned_holding_targets": ["future_executable_bid_5m_15m_30m_60m", "recovery_to_cost", "recovery_to_entry_target", "continued_drawdown"],
        "not_trained_yet": "This report is a label/audit source, not a separate trained model; current episode count is insufficient for training.",
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome thesis-failure evidence report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--tolerance-sec", type=int, default=90)
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period, tolerance_sec=args.tolerance_sec), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
