"""Read-only replay of candidate fast-failure risk lanes.

The report is deliberately conservative: a candidate is an observed historical
decision boundary, *not* a synthetic order.  It cannot submit, cancel, alter
the journal, or claim that a passive quote would have filled.  Its purpose is
to make the trade-off explicit: did a multi-signal warning appear while a
full position could still have been sold inside a stated loss cap, and would
the same rule have cut a later profitable lifecycle?
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


WARNING_DRAWDOWN = Decimal("-0.05")
HARD_DRAWDOWN = Decimal("-0.10")
HARD_LOSS_CAP = Decimal("-0.15")
REBRAKE_DRAWDOWN = Decimal("-0.20")
REBRAKE_RECOVERY = Decimal("-0.10")
VELOCITY_30_BPS = Decimal("-250")
DEPTH_RATIO_30 = Decimal("0.70")
PERSISTENCE_SEC = 10.0
KNOWN_OUTCOMES = {1993, 2437, 2639, 2820}


def _payload(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _timestamp(value: object) -> float | None:
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


def _nearest_before(rows: list[tuple[float, dict[str, Any]]], at: float, tolerance: float = 12.0) -> dict[str, Any] | None:
    if not rows:
        return None
    times = [row[0] for row in rows]
    index = bisect.bisect_right(times, at) - 1
    if index < 0:
        return None
    timestamp, payload = rows[index]
    return payload if at - timestamp <= tolerance else None


def _raw_book(payload: dict[str, Any]) -> tuple[str, Decimal, Decimal] | None:
    """Extract only BBO and top-three bid size from immutable WS L2 payload."""
    try:
        data = payload["raw"]["data"]
        coin = str(data["coin"])
        bids = data["levels"][0]
        parsed = [(Decimal(str(row["px"])), Decimal(str(row["sz"]))) for row in bids[:3]]
    except (KeyError, TypeError, ValueError, InvalidOperation, IndexError):
        return None
    if not parsed or parsed[0][0] <= 0 or any(price <= 0 or size <= 0 for price, size in parsed):
        return None
    return coin, parsed[0][0], sum(size for _, size in parsed)


def _raw_metrics(rows: list[tuple[float, Decimal, Decimal]], at: float) -> dict[str, Any] | None:
    """Build the same 30-second velocity/depth facts from raw WS, not a later shadow."""
    if not rows:
        return None
    times = [row[0] for row in rows]
    current_index = bisect.bisect_right(times, at) - 1
    if current_index < 0 or at - rows[current_index][0] > 12.0:
        return None
    current_ts, current_bid, current_depth = rows[current_index]
    prior_index = bisect.bisect_right(times, current_ts - 30.0) - 1
    if prior_index < 0:
        return None
    _, prior_bid, prior_depth = rows[prior_index]
    if prior_bid <= 0 or prior_depth <= 0:
        return None
    return {
        "bid_velocity_bps": {"30": str((current_bid / prior_bid - Decimal("1")) * Decimal("10000"))},
        "top3_depth_ratio": {"30": str(current_depth / prior_depth)},
        "source": "raw_ws_l2",
    }


def _holding_metrics(rows: list[tuple[float, dict[str, Any]]], at: float) -> dict[str, Any] | None:
    """Reconstruct 30-second executable BBO/depth movement from bound paths.

    ``marketable_exit_depth_shares`` is full-inventory depth, not top-three
    depth.  It is nevertheless an executable capacity fact and is preferable
    to declaring older lifecycles unobservable merely because crash-shadow was
    added later.  Consumers must retain the source label below.
    """
    times = [row[0] for row in rows]
    current_index = bisect.bisect_right(times, at) - 1
    if current_index < 0:
        return None
    current_ts, current = rows[current_index]
    prior_index = bisect.bisect_right(times, current_ts - 30.0) - 1
    if prior_index < 0:
        return None
    _, prior = rows[prior_index]
    current_bid = _decimal(current.get("best_bid"))
    prior_bid = _decimal(prior.get("best_bid"))
    current_depth = _decimal(current.get("marketable_exit_depth_shares"))
    prior_depth = _decimal(prior.get("marketable_exit_depth_shares"))
    if current_bid is None or prior_bid is None or current_bid <= 0 or prior_bid <= 0:
        return None
    values: dict[str, Any] = {
        "bid_velocity_bps": {"30": str((current_bid / prior_bid - Decimal("1")) * Decimal("10000"))},
        "source": "holding_path_executable_bbo",
    }
    if current_depth is not None and prior_depth is not None and current_depth > 0 and prior_depth > 0:
        values["top3_depth_ratio"] = {"30": str(current_depth / prior_depth)}
        values["depth_metric_kind"] = "full_inventory_executable_depth_not_top3"
    return values


def _side_thesis_failed(payload: dict[str, Any]) -> bool:
    """Use only a contemporaneous strike-side contradiction, never hindsight."""
    bps = _decimal(payload.get("spot_strike_bps"))
    try:
        side = int(payload.get("entry_side_index"))
    except (TypeError, ValueError):
        return False
    return bool((side == 0 and bps is not None and bps <= 0) or (side == 1 and bps is not None and bps >= 0))


def _signals(path: dict[str, Any], crash: dict[str, Any] | None) -> list[str]:
    net = _decimal(path.get("marketable_net_exit_vs_entry_pct") or path.get("net_exit_vs_entry_pct"))
    if net is None or net > WARNING_DRAWDOWN:
        return []
    result = ["executable_drawdown"]
    if crash is not None:
        velocity = _decimal((crash.get("bid_velocity_bps") or {}).get("30"))
        depth = _decimal((crash.get("top3_depth_ratio") or {}).get("30"))
        if velocity is not None and velocity <= VELOCITY_30_BPS:
            result.append("bid_velocity")
        if depth is not None and depth <= DEPTH_RATIO_30:
            result.append("depth_depletion")
    if _side_thesis_failed(path):
        result.append("spot_strike_thesis_failure")
    return result


def _close_results(conn: sqlite3.Connection) -> dict[str, tuple[Decimal, Decimal]]:
    rows = conn.execute(
        """SELECT open_trade_id,SUM(CAST(cost_usdc AS REAL)),SUM(CAST(realized_net_usdc AS REAL))
           FROM outcome_realized_pnl_lots GROUP BY open_trade_id"""
    ).fetchall()
    result: dict[str, tuple[Decimal, Decimal]] = {}
    for trade, cost, pnl in rows:
        parsed_cost, parsed_pnl = _decimal(cost), _decimal(pnl)
        if parsed_cost is not None and parsed_pnl is not None:
            result[str(trade)] = parsed_cost, parsed_pnl
    return result


def _second_drawdown_after_meaningful_recovery(
    observations: list[tuple[float, dict[str, Any]]],
) -> dict[str, Any] | None:
    """Find ``<-20% → recover to >=-10% → <-20%`` without hindsight fills.

    This is intentionally a report-only *definition* of "跌回來又再跌".
    A rebound smaller than ten percentage points does not reset the episode,
    which prevents ordinary quote noise near the boundary being called a
    second independent crash.
    """
    first_cross: tuple[float, dict[str, Any], Decimal] | None = None
    recovery: tuple[float, dict[str, Any], Decimal] | None = None
    for timestamp, payload in observations:
        value = _decimal(payload.get("marketable_net_exit_vs_entry_pct") or payload.get("net_exit_vs_entry_pct"))
        if value is None:
            continue
        if first_cross is None and value <= REBRAKE_DRAWDOWN:
            first_cross = (timestamp, payload, value)
        elif first_cross is not None and recovery is None and value >= REBRAKE_RECOVERY:
            recovery = (timestamp, payload, value)
        elif recovery is not None and value <= REBRAKE_DRAWDOWN:
            return {
                "first_drawdown_ts": datetime.fromtimestamp(first_cross[0]).astimezone().isoformat(),
                "first_drawdown_pct": str(first_cross[2]),
                "recovery_ts": datetime.fromtimestamp(recovery[0]).astimezone().isoformat(),
                "recovery_pct": str(recovery[2]),
                "rebreak_ts": datetime.fromtimestamp(timestamp).astimezone().isoformat(),
                "rebreak_pct": str(value),
                "rebreak_full_inventory_depth": payload.get("marketable_exit_full_inventory") is True,
                "rebreak_holding_age_sec": payload.get("holding_age_sec"),
                "rebreak_time_left_sec": payload.get("time_left_sec"),
            }
    return None


def report(db_path: str | Path, *, period: str = "1d", include_raw_known: bool = False) -> dict[str, Any]:
    """Replay warning/hard candidate boundaries from immutable journal facts."""
    path = Path(db_path)
    result: dict[str, Any] = {
        "report": "outcome_fast_failure_replay", "schema_version": 1,
        "period": period, "live_authority": False, "execution_submitted": False,
        "config": {
            "warning_drawdown_pct": str(WARNING_DRAWDOWN), "hard_drawdown_pct": str(HARD_DRAWDOWN),
            "hard_loss_cap_pct": str(HARD_LOSS_CAP), "bid_velocity_30_bps": str(VELOCITY_30_BPS),
            "top3_depth_ratio_30": str(DEPTH_RATIO_30), "minimum_distinct_signals": 2,
            "persistence_sec": PERSISTENCE_SEC,
            "second_drawdown_definition": f"<={REBRAKE_DRAWDOWN} then >={REBRAKE_RECOVERY} then <={REBRAKE_DRAWDOWN}",
        },
        "lifecycles": [], "known_cases": [], "summary": {}, "blockers": [],
    }
    if not path.exists():
        result["blockers"] = ["journal_missing"]
        return result
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"strategy_events", "outcome_realized_pnl_lots"}.issubset(tables):
            result["blockers"] = ["required_journal_tables_missing"]
            return result
        rows = conn.execute(
            """SELECT ts,event_type,payload_json FROM strategy_events
               WHERE event_type IN ('OUTCOME_HOLDING_PATH_OBSERVATION','OUTCOME_CRASH_CIRCUIT_SHADOW')
               ORDER BY id"""
        ).fetchall()
        closes = _close_results(conn)

    paths: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    crashes: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    for raw_ts, event_type, raw in rows:
        timestamp, payload = _timestamp(raw_ts), _payload(raw)
        lifecycle = str(payload.get("entry_lifecycle_id") or "")
        if timestamp is None or not lifecycle or payload.get("period") != period:
            continue
        if event_type == "OUTCOME_HOLDING_PATH_OBSERVATION":
            paths[lifecycle].append((timestamp, payload))
        else:
            crashes[lifecycle].append((timestamp, payload))
    for values in list(paths.values()) + list(crashes.values()):
        values.sort(key=lambda row: row[0])
    # The control set is deliberately exact and bounded.  Fetching every raw
    # WS payload from a multi-GB journal would make a read-only report itself
    # a source of operational pressure.  Instead, fetch only the observed
    # holding windows for the named tail/control outcomes (plus a small
    # lookback required for 30-second velocity).
    raw_books: dict[str, list[tuple[float, Decimal, Decimal]]] = defaultdict(list)
    windows: list[tuple[str, str]] = []
    for observations in paths.values():
        first = observations[0][1]
        try:
            outcome_id = int(first.get("outcome_id"))
        except (TypeError, ValueError):
            continue
        if outcome_id not in KNOWN_OUTCOMES:
            continue
        start = observations[0][0] - 40.0
        end = observations[-1][0] + 5.0
        windows.append((
            datetime.fromtimestamp(start, timezone.utc).isoformat(),
            datetime.fromtimestamp(end, timezone.utc).isoformat(),
        ))
    if include_raw_known and windows:
        clauses = " OR ".join("(ts >= ? AND ts <= ?)" for _ in windows)
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
            raw_rows = conn.execute(
                f"SELECT ts,payload_json FROM strategy_events WHERE event_type='OUTCOME_WS_L2_BOOK' AND ({clauses}) ORDER BY id",
                [value for window in windows for value in window],
            ).fetchall()
        for raw_ts, raw in raw_rows:
            timestamp, parsed = _timestamp(raw_ts), _raw_book(_payload(raw))
            if timestamp is not None and parsed is not None:
                coin, bid, depth = parsed
                raw_books[coin].append((timestamp, bid, depth))
    for values in raw_books.values():
        values.sort(key=lambda row: row[0])

    positive_hard_candidates = 0
    cap_eligible_hard_candidates = 0
    positive_cap_eligible_hard_candidates = 0
    known_found: set[int] = set()
    warning_candidates = 0
    hard_candidates = 0
    second_drawdown_candidates = 0
    profitable_second_drawdown_candidates = 0
    no_crash_coverage = 0
    for lifecycle, observations in sorted(paths.items()):
        first = observations[0][1]
        outcome_id = first.get("outcome_id")
        try:
            outcome_number = int(outcome_id)
        except (TypeError, ValueError):
            outcome_number = None
        trade_id = str(first.get("entry_trade_id") or "")
        cost, final_pnl = closes.get(trade_id, (None, None))
        final_return = final_pnl / cost if cost is not None and cost > 0 and final_pnl is not None else None
        warning_started: float | None = None
        warning: dict[str, Any] | None = None
        hard: dict[str, Any] | None = None
        crash_rows = crashes.get(lifecycle, [])
        if not crash_rows:
            no_crash_coverage += 1
        for timestamp, payload in observations:
            crash = _nearest_before(crash_rows, timestamp)
            source = "crash_shadow" if crash is not None else None
            if crash is None:
                crash = _holding_metrics(observations, timestamp)
                source = crash.get("source") if crash is not None else None
            if crash is None and include_raw_known:
                crash = _raw_metrics(raw_books.get(str(payload.get("coin") or ""), []), timestamp)
                source = crash.get("source") if crash is not None else None
            signal_names = _signals(payload, crash)
            if len(signal_names) < 2:
                warning_started = None
                continue
            if warning_started is None:
                warning_started = timestamp
            if timestamp - warning_started < PERSISTENCE_SEC:
                continue
            net_return = _decimal(payload.get("marketable_net_exit_vs_entry_pct") or payload.get("net_exit_vs_entry_pct"))
            full_depth = payload.get("marketable_exit_full_inventory") is True
            candidate = {
                "ts": datetime.fromtimestamp(timestamp).astimezone().isoformat(),
                "holding_age_sec": payload.get("holding_age_sec"), "time_left_sec": payload.get("time_left_sec"),
                "signals": signal_names, "full_inventory_depth": full_depth,
                "net_exit_return_pct": str(net_return) if net_return is not None else None,
                "bid_velocity_30_bps": (crash or {}).get("bid_velocity_bps", {}).get("30"),
                "top3_depth_ratio_30": (crash or {}).get("top3_depth_ratio", {}).get("30"),
                "microstructure_source": source,
                "depth_metric_kind": (
                    (crash or {}).get("depth_metric_kind")
                    or ("unavailable" if source == "holding_path_executable_bbo" else "top3_depth")
                ),
            }
            if warning is None:
                warning = candidate
            if hard is None and net_return is not None and net_return <= HARD_DRAWDOWN:
                hard = {**candidate, "within_hard_cap": bool(full_depth and net_return >= HARD_LOSS_CAP)}
        if warning is not None:
            warning_candidates += 1
        if hard is not None:
            hard_candidates += 1
            if final_pnl is not None and final_pnl > 0:
                positive_hard_candidates += 1
            if hard["within_hard_cap"]:
                cap_eligible_hard_candidates += 1
                if final_pnl is not None and final_pnl > 0:
                    positive_cap_eligible_hard_candidates += 1
        second_drawdown = _second_drawdown_after_meaningful_recovery(observations)
        if second_drawdown is not None:
            second_drawdown_candidates += 1
            if final_pnl is not None and final_pnl > 0:
                profitable_second_drawdown_candidates += 1
        row = {
            "entry_lifecycle_id": lifecycle, "entry_trade_id": trade_id or None,
            "outcome_id": outcome_number, "coin": first.get("coin"),
            "entry_side_index": first.get("entry_side_index"), "entry_fill_vwap": first.get("fill_vwap"),
            "final_realized_pnl_usdc": str(final_pnl) if final_pnl is not None else None,
            "final_realized_return_pct": str(final_return) if final_return is not None else None,
            "holding_observation_count": len(observations), "crash_telemetry_count": len(crash_rows),
            "warning_candidate": warning, "hard_candidate": hard,
            "second_drawdown_after_recovery_candidate": second_drawdown,
            "interpretation": (
                "hard_candidate_within_cap_is_a_replay_eligibility_fact_not_a_synthetic_IOC_fill"
                if hard is not None else "no_multi_signal_hard_candidate_in_available_lifecycle_bound_telemetry"
            ),
        }
        result["lifecycles"].append(row)
        if outcome_number in KNOWN_OUTCOMES:
            known_found.add(outcome_number)
            result["known_cases"].append(row)
    result["summary"] = {
        "lifecycle_count": len(result["lifecycles"]), "warning_candidate_count": warning_candidates,
        "hard_candidate_count": hard_candidates,
        "profitable_lifecycles_with_hard_candidate": positive_hard_candidates,
        "hard_candidate_within_cap_count": cap_eligible_hard_candidates,
        "profitable_lifecycles_with_hard_candidate_within_cap": positive_cap_eligible_hard_candidates,
        "second_drawdown_after_recovery_candidate_count": second_drawdown_candidates,
        "profitable_lifecycles_with_second_drawdown_after_recovery": profitable_second_drawdown_candidates,
        "lifecycles_without_crash_telemetry": no_crash_coverage,
        "known_outcomes_found": sorted(known_found),
        "known_outcomes_missing": sorted(KNOWN_OUTCOMES - known_found),
        "raw_known_control_windows_loaded": include_raw_known,
    }
    result["blockers"] = [
        "replay_only_no_synthetic_queue_or_ioc_fill",
        "hard_candidate_requires_fresh_full_inventory_depth_in_live_controller",
        "candidate thresholds_are_not_live_policy_or_calibrated_parameters",
    ]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome fast-failure replay report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--include-raw-known", action="store_true",
                        help="Read raw WS only for #1993/#2437/#2639/#2820 holding windows; slower on a multi-GB journal.")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period, include_raw_known=args.include_raw_known), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
