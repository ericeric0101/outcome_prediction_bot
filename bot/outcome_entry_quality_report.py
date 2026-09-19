"""Read-only Phase-A/B audit for Outcome adverse-selection research."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _markout_summary(values: list[float]) -> dict[str, Any]:
    """Summarise observed P3 values without claiming counterfactual PnL."""
    if not values:
        return {"n": 0, "mean": None, "median": None, "negative_rate": None}
    return {
        "n": len(values),
        "mean": mean(values),
        "median": median(values),
        "negative_rate": sum(value < 0 for value in values) / len(values),
    }


def _timestamp(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _number(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    """Summarise shadow action candidates; never query the exchange."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = conn.execute(
            """SELECT ts,event_type,payload_json FROM strategy_events
               WHERE event_type IN ('OUTCOME_ENTRY_QUALITY_SHADOW','OUTCOME_POST_FILL_QUALITY_SHADOW')
               ORDER BY id"""
        ).fetchall()
        markout_rows = conn.execute(
            """SELECT payload_json FROM order_events WHERE event_type='FILL_MARKOUT' ORDER BY id"""
        ).fetchall()
        holding_rows = conn.execute(
            """SELECT ts,payload_json FROM strategy_events
               WHERE event_type='OUTCOME_HOLDING_PATH_OBSERVATION' ORDER BY id"""
        ).fetchall()
        monitor_rows = conn.execute(
            """SELECT ts,payload_json FROM strategy_events
               WHERE event_type='OUTCOME_MARKET_RISK_MONITOR_SHADOW' ORDER BY id"""
        ).fetchall()
        pnl_rows = conn.execute(
            """SELECT open_trade_id,realized_net_usdc,recorded_at
               FROM outcome_realized_pnl_lots"""
        ).fetchall() if "outcome_realized_pnl_lots" in tables else []
    observations = [(str(ts), str(kind), _payload(raw)) for ts, kind, raw in rows]
    prefill = [item for item in observations if item[1] == "OUTCOME_ENTRY_QUALITY_SHADOW" and item[2].get("period") == period]
    postfill = [item for item in observations if item[1] == "OUTCOME_POST_FILL_QUALITY_SHADOW" and item[2].get("period") == period]
    stale_by_order: dict[str, dict[str, Any]] = {}
    for ts, _kind, payload in prefill:
        order_id = str(payload.get("order_id") or "")
        action = payload.get("stale_cancel_shadow", {}).get("action") if isinstance(payload.get("stale_cancel_shadow"), dict) else None
        if order_id and action == "CANCEL_STALE_SHADOW":
            stale_by_order.setdefault(order_id, {"first_seen_at": ts, "payload": payload})
    p3: dict[str, dict[str, Any]] = defaultdict(dict)
    for (raw,) in markout_rows:
        payload = _payload(raw)
        fill_id, horizon = payload.get("fill_id"), payload.get("horizon_sec")
        if fill_id is not None and horizon is not None:
            p3[str(fill_id)][str(horizon)] = payload.get("signed_markout_ps")
    postfill_by_trade: dict[str, dict[str, Any]] = {}
    scratch_by_trade: dict[str, dict[str, Any]] = {}
    for ts, _kind, payload in postfill:
        trade_id = str(payload.get("fill_trade_id") or "")
        if trade_id:
            postfill_by_trade.setdefault(trade_id, {"first_seen_at": ts, "payload": payload})
            shadow = payload.get("post_fill_scratch_shadow")
            if isinstance(shadow, dict) and shadow.get("action") == "SCRATCH_IOC_COUNTERFACTUAL":
                scratch_by_trade.setdefault(trade_id, {"first_seen_at": ts, "payload": payload})
    postfill_markout_groups: dict[str, dict[str, list[float]]] = {
        "ever_scratch_candidate": defaultdict(list),
        "never_scratch_candidate": defaultdict(list),
    }
    for trade_id, horizons in p3.items():
        if trade_id not in postfill_by_trade:
            continue
        group = "ever_scratch_candidate" if trade_id in scratch_by_trade else "never_scratch_candidate"
        for horizon, value in horizons.items():
            try:
                postfill_markout_groups[group][horizon].append(float(value))
            except (TypeError, ValueError):
                continue

    paths_by_trade: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for ts, raw in holding_rows:
        payload = _payload(raw)
        trade_id = str(payload.get("entry_trade_id") or "")
        at = _timestamp(ts)
        net_return = _number(payload.get("marketable_net_exit_vs_entry_pct"))
        if net_return is None:
            net_return = _number(payload.get("net_exit_vs_entry_pct"))
        if trade_id and at is not None and net_return is not None:
            paths_by_trade[trade_id].append((at, net_return))
    for values in paths_by_trade.values():
        values.sort(key=lambda item: item[0])

    monitor_by_lifecycle: dict[str, list[tuple[datetime, float, float, float]]] = defaultdict(list)
    for ts, raw in monitor_rows:
        payload = _payload(raw)
        lifecycle_id = str(payload.get("entry_lifecycle_id") or "")
        at = _timestamp(ts)
        bid, ask, depth = (_number(payload.get("best_bid")), _number(payload.get("best_ask")),
                           _number(payload.get("top3_depth")))
        if lifecycle_id and at is not None and bid is not None and ask is not None and depth is not None and bid > 0 and ask > bid and depth > 0:
            monitor_by_lifecycle[lifecycle_id].append((at, bid, ask, depth))
    for values in monitor_by_lifecycle.values():
        values.sort(key=lambda item: item[0])

    def recovery_shape(samples: list[tuple[datetime, float, float, float]]) -> dict[str, Any]:
        """Mirror the existing read-only MarketRiskMonitor shape contract."""
        if len(samples) < 3:
            return {
                "classification": "SCRATCH_UNRESOLVED_RESEARCH", "missing_data_reason": "fewer_than_three_monitor_samples",
                "sample_count": len(samples), "bid_path_efficiency": None, "bid_direction_flips": 0,
                "depth_refill_ratio": None, "spread_convergence_ratio": None,
            }
        bids, depths = [item[1] for item in samples], [item[3] for item in samples]
        spreads = [((ask / bid) - 1) * 10_000 for _at, bid, ask, _depth in samples]
        changes = [current - prior for prior, current in zip(bids, bids[1:])]
        signs = [1 if value > 0 else -1 for value in changes if value != 0]
        flips = sum(1 for prior, current in zip(signs, signs[1:]) if prior != current)
        travelled = sum(abs(current - prior) for prior, current in zip(bids, bids[1:]))
        efficiency = abs(bids[-1] - bids[0]) / travelled if travelled > 0 else 0.0
        refill = depths[-1] / min(depths) if min(depths) > 0 else None
        convergence = spreads[-1] / max(spreads) if max(spreads) > 0 else None
        chop = bool(flips >= 1 and efficiency <= 0.60 and refill is not None and refill >= 1.25
                    and convergence is not None and convergence <= 0.80)
        return {
            "classification": "SCRATCH_CHOP_RECOVERY_RESEARCH" if chop else "SCRATCH_PERSISTENT_DETERIORATION_RESEARCH",
            "missing_data_reason": None, "sample_count": len(samples),
            "bid_path_efficiency": efficiency, "bid_direction_flips": flips,
            "depth_refill_ratio": refill, "spread_convergence_ratio": convergence,
        }

    realized_by_trade: dict[str, float] = defaultdict(float)
    close_at_by_trade: dict[str, str] = {}
    for trade_id, realized, recorded_at in pnl_rows:
        value = _number(realized)
        if value is None:
            continue
        key = str(trade_id)
        realized_by_trade[key] += value
        close_at_by_trade[key] = max(close_at_by_trade.get(key, ""), str(recorded_at or ""))

    scratch_lifecycle_outcomes: list[dict[str, Any]] = []
    for trade_id, scratch in sorted(scratch_by_trade.items(), key=lambda item: str(item[1]["first_seen_at"])):
        scratch_at = _timestamp(scratch["first_seen_at"])
        lifecycle_id = f"official_buy:{scratch['payload'].get('order_id')}:{trade_id}"
        monitor_window = [
            item for item in monitor_by_lifecycle.get(lifecycle_id, [])
            if scratch_at is not None and item[0] >= scratch_at
            and (item[0] - scratch_at).total_seconds() <= 90
        ]
        path = paths_by_trade.get(trade_id, [])
        path_windows: dict[str, Any] = {}
        for horizon_sec in (30, 60, 120):
            values = [
                (at, net_return) for at, net_return in path
                if scratch_at is not None and at >= scratch_at
                and (at - scratch_at).total_seconds() <= horizon_sec
            ]
            recovery = next((at.isoformat() for at, net_return in values if net_return >= 0), None)
            path_windows[str(horizon_sec)] = {
                "samples": len(values),
                "minimum_executable_return_pct": min((value for _at, value in values), default=None),
                "maximum_executable_return_pct": max((value for _at, value in values), default=None),
                "recovered_to_nonnegative_at": recovery,
            }
        realized = realized_by_trade.get(trade_id)
        outcome = (
            "realized_profit_recovery" if realized is not None and realized > 0
            else "realized_loss_persistent" if realized is not None and realized < 0
            else "flat_or_unknown_final_outcome"
        )
        scratch_lifecycle_outcomes.append({
            "fill_trade_id": trade_id,
            "first_scratch_at": scratch["first_seen_at"],
            "outcome_id": scratch["payload"].get("outcome_id"),
            "order_id": scratch["payload"].get("order_id"),
            "entry_lifecycle_id": lifecycle_id,
            "executable_return_pct_at_first_scratch": scratch["payload"].get("executable_return_pct"),
            "market_risk_90s": recovery_shape(monitor_window),
            "post_scratch_executable_path": path_windows,
            "canonical_realized_net_usdc": realized,
            "canonical_close_at": close_at_by_trade.get(trade_id),
            "classification": outcome,
        })
    scratch_outcome_counts = Counter(item["classification"] for item in scratch_lifecycle_outcomes)
    scratch_research_counts = Counter(
        str(item["market_risk_90s"].get("classification")) for item in scratch_lifecycle_outcomes
    )
    signal_states = Counter(str(payload.get("signal_state") or "unknown") for _ts, _kind, payload in prefill)
    actions = Counter(
        str((payload.get("stale_cancel_shadow") or {}).get("action") or "unknown")
        for _ts, _kind, payload in prefill
    )
    return {
        "report": "outcome_entry_quality_shadow", "schema_version": 1, "period": period,
        "prefill_observation_count": len(prefill), "postfill_observation_count": len(postfill),
        "prefill_signal_states": dict(signal_states), "stale_cancel_shadow_actions": dict(actions),
        "first_stale_cancel_candidate_by_order": [
            {"order_id": order_id, **value} for order_id, value in sorted(stale_by_order.items())
        ],
        "first_postfill_watch_by_trade": [
            {"fill_trade_id": trade_id, **value, "p3_signed_markout_ps": p3.get(trade_id, {})}
            for trade_id, value in sorted(postfill_by_trade.items())
        ],
        "postfill_scratch_p3_comparison": {
            group: {
                horizon: _markout_summary(values)
                for horizon, values in sorted(horizons.items(), key=lambda item: int(item[0]))
            }
            for group, horizons in postfill_markout_groups.items()
        },
        "postfill_scratch_lifecycle_outcomes": scratch_lifecycle_outcomes,
        "postfill_scratch_lifecycle_outcome_counts": dict(scratch_outcome_counts),
        "postfill_scratch_market_risk_90s_counts": dict(scratch_research_counts),
        "limits": [
            "Shadow candidates are not fills, cancellations, or IOC simulations.",
            "IOC counterfactual uses only as-of top-of-book and excludes full-depth/slippage/fee certainty.",
            "P3 markout is joined only by immutable official fill trade ID when available.",
            "A scratch candidate means its observed path crossed the shadow watch; it does not prove an IOC would have filled at that price or improved lifecycle PnL.",
            "A later positive canonical FIFO result establishes that a blanket scratch would have cut a winning lifecycle; it does not reconstruct the exact hypothetical IOC fill.",
        ],
        "promotion_blockers": [
            "Need independent daily markets and official fill/P3 outcomes before any live cancellation or active-entry canary.",
            "Need full-depth and fee-aware replay before comparing JOIN to price-protected IOC EV.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome adverse-selection shadow report")
    parser.add_argument("--db", required=True)
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
