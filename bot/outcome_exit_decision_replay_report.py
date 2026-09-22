"""Read-only replay of executed Outcome hard exits using existing journal facts.

This deliberately joins the already-recorded holding path, risk monitors,
order lifecycle and (optionally) raw L2 stream.  It creates no event, changes
no schema and has no import path to an execution gateway.  Its purpose is to
compare one-sided deterioration with choppy/depth-recovering conditions before
adding any new live exit authority.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


RISK_EVENTS = (
    "OUTCOME_MARKET_RISK_MONITOR_SHADOW",
    "OUTCOME_CRASH_CIRCUIT_SHADOW",
    "OUTCOME_FAST_FAILURE_LANE_SHADOW",
    "OUTCOME_HOLDING_PATH_OBSERVATION",
    "OUTCOME_HOLDING_RISK_DECISION_SHADOW",
)
NARROW_EXECUTION_TYPE = "narrow_hard_failure_price_protected_fak_ioc"

# This is a replay-only proposal, deliberately separate from the complete
# HOLD_FOR_RECOVERY definition.  It captures the three *early* recovery facts
# that can be visible while the 90-second trend-efficiency denominator still
# reflects the preceding one-way selloff.  It has no live caller.
RECOVERY_PROBE_SEC = 60
RECOVERY_PROBE_TOLERANCE_SEC = 45

# These are exact entry lifecycles, rather than outcome ids.  A single daily
# market can contain several unrelated BUY/SELL lots, so an outcome-id lookup
# would silently mix a tail with ordinary profitable round trips.
CONTROL_LIFECYCLES: tuple[dict[str, str], ...] = (
    {"case": "1993_tail", "entry_lifecycle_id": "official_buy:539350046368:965286494611543", "expected": "persistent"},
    {"case": "2437_tail", "entry_lifecycle_id": "official_buy:542401099983:37264263716062", "expected": "persistent"},
    {"case": "2820_tail", "entry_lifecycle_id": "official_buy:544175257282:916037474879419", "expected": "persistent"},
    {"case": "2639_recovered_control", "entry_lifecycle_id": "official_buy:543149953671:599501425915030", "expected": "not_persistent"},
    {"case": "3253_ioc_64815", "entry_lifecycle_id": "official_buy:545599571527:317333610957890", "expected": "persistent"},
    {"case": "3253_ioc_70043", "entry_lifecycle_id": "official_buy:545872396577:992265207394277", "expected": "not_persistent"},
)


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _summary(values: Iterable[float]) -> dict[str, float | None]:
    items = list(values)
    return {
        "first": items[0] if items else None,
        "latest": items[-1] if items else None,
        "minimum": min(items) if items else None,
        "maximum": max(items) if items else None,
    }


def _sign_flips(values: Iterable[float]) -> int:
    signs = [1 if value > 0 else -1 for value in values if value != 0]
    return sum(1 for prior, current in zip(signs, signs[1:]) if prior != current)


def _chop_label(*, samples: int, velocities: list[float], depths: list[float], spreads: list[float]) -> str:
    """A conservative replay label, never a live instruction.

    A label requires several monitor samples plus both a direction flip and
    evidence that the displayed book recovered.  Missing feature coverage is
    intentionally reported rather than inferred from a later price.
    """
    if samples < 3 or not velocities or not depths or not spreads:
        return "insufficient_monitor_coverage"
    minimum_depth, maximum_spread = min(depths), max(spreads)
    latest_depth, latest_spread = depths[-1], spreads[-1]
    recovered_depth = minimum_depth > 0 and latest_depth / minimum_depth >= 1.25
    contracted_spread = maximum_spread > 0 and latest_spread / maximum_spread <= 0.80
    if _sign_flips(velocities) >= 1 and recovered_depth and contracted_spread:
        return "chop_depth_recovery_candidate"
    return "persistent_or_unresolved_deterioration"


def _trend_efficiency(values: Iterable[float]) -> float | None:
    """Absolute net move divided by total path length; 1 is one-way."""
    items = list(values)
    if len(items) < 3:
        return None
    path = sum(abs(current - prior) for prior, current in zip(items, items[1:]))
    return abs(items[-1] - items[0]) / path if path > 0 else 0.0


def _control_label(*, bids: list[float], depths: list[float], spreads: list[float], thesis_state: str) -> str:
    """Read-only, predeclared replay label; never an execution instruction."""
    if len(bids) < 3 or len(depths) < 3 or len(spreads) < 3 or thesis_state == "unavailable":
        return "insufficient_data"
    efficiency = _trend_efficiency(bids)
    if efficiency is None:
        return "insufficient_data"
    latest_depth, min_depth = depths[-1], min(depths)
    latest_spread, max_spread = spreads[-1], max(spreads)
    depth_recovered = min_depth > 0 and latest_depth / min_depth >= 1.25
    spread_contracted = max_spread > 0 and latest_spread / max_spread <= 0.80
    bid_changes = [current - prior for prior, current in zip(bids, bids[1:])]
    # A chop finding is deliberately conjunctive: one flip alone is ordinary
    # noise and cannot veto a safety action in a future phase.
    if (_sign_flips(bid_changes) >= 1 and efficiency <= 0.60
            and depth_recovered and spread_contracted):
        return "chop_recovery_candidate"
    # The control replay does not infer that the underlying thesis is false:
    # #2437 is an explicit counterexample.  Persistent book deterioration can
    # still be an observed tail-risk regime while thesis remains intact.
    return "persistent_or_unresolved_deterioration"


def _recovery_building(shape: dict[str, Any]) -> bool:
    """Return the predeclared early-recovery shape for replay only.

    Full ``HOLD_FOR_RECOVERY`` additionally requires trend efficiency <= .60.
    This earlier state does not relax that production definition; it asks a
    bounded counterfactual question: would retaining passive protection for
    60 seconds have allowed the full shape to resolve before an IOC?
    """
    try:
        flips = int(shape.get("bid_direction_flips"))
        depth = float(shape.get("depth_recovery_ratio"))
        spread = float(shape.get("spread_contraction_ratio"))
    except (TypeError, ValueError):
        return False
    return flips >= 1 and depth >= 1.25 and spread <= 0.80


def _counterfactual_eligible(payload: dict[str, Any], lane: str) -> bool:
    actions = payload.get("counterfactual_actions")
    action = actions.get(lane) if isinstance(actions, dict) else None
    return bool(action.get("eligible")) if isinstance(action, dict) else False


def _first_checkpoint(
    rows: list[tuple[datetime, dict[str, Any]]], *, after: datetime, seconds: int,
) -> tuple[datetime, dict[str, Any]] | None:
    """Use the first observed checkpoint, never interpolated book data."""
    earliest = after + timedelta(seconds=seconds)
    latest = earliest + timedelta(seconds=RECOVERY_PROBE_TOLERANCE_SEC)
    return next(((at, payload) for at, payload in rows if earliest <= at <= latest), None)


def _recovery_probe_case(
    lifecycle_id: str, rows: list[tuple[datetime, dict[str, Any]]], *, final_pnl: float | None,
) -> dict[str, Any] | None:
    """Replay immediate IOC vs a bounded 60-second recovery observation.

    A missing row, lost cap capacity, or missing final FIFO result remains an
    explicit unknown.  The helper deliberately never claims that an IOC or a
    passive order would have filled.
    """
    first_hard = next((item for item in rows if _counterfactual_eligible(item[1], "hard_ioc")), None)
    if first_hard is None:
        return None
    hard_at, hard_payload = first_hard
    initial_shape = hard_payload.get("recovery_shape")
    initial_shape = initial_shape if isinstance(initial_shape, dict) else {}
    building = _recovery_building(initial_shape)
    checkpoints: dict[str, dict[str, Any] | None] = {}
    for seconds in (30, RECOVERY_PROBE_SEC):
        checkpoint = _first_checkpoint(rows, after=hard_at, seconds=seconds)
        if checkpoint is None:
            checkpoints[str(seconds)] = None
            continue
        observed_at, payload = checkpoint
        shape = payload.get("recovery_shape")
        shape = shape if isinstance(shape, dict) else {}
        checkpoints[str(seconds)] = {
            "observed_ts": observed_at.isoformat(),
            "delay_sec": (observed_at - hard_at).total_seconds(),
            "net_return_pct": payload.get("full_depth_net_return_pct"),
            "hard_ioc_eligible": _counterfactual_eligible(payload, "hard_ioc"),
            "hold_for_recovery_eligible": _counterfactual_eligible(payload, "hold_for_recovery"),
            "recovery_building": _recovery_building(shape),
            "classification": shape.get("classification"),
        }
    at_probe = checkpoints.get(str(RECOVERY_PROBE_SEC))
    if not building:
        proposed_action = "IMMEDIATE_HARD_IOC_COUNTERFACTUAL"
    elif at_probe is None:
        proposed_action = "RECOVERY_PROBE_INSUFFICIENT_CHECKPOINT_DATA"
    elif at_probe["hold_for_recovery_eligible"]:
        proposed_action = "HOLD_FOR_RECOVERY_AFTER_60S_COUNTERFACTUAL"
    elif at_probe["hard_ioc_eligible"]:
        proposed_action = "HARD_IOC_AFTER_60S_PROBE_COUNTERFACTUAL"
    else:
        proposed_action = "RECOVERY_PROBE_LOST_CAP_OR_DEPTH_CAPACITY"
    return {
        "entry_lifecycle_id": lifecycle_id,
        "initial_hard_ts": hard_at.isoformat(),
        "initial_net_return_pct": hard_payload.get("full_depth_net_return_pct"),
        "initial_recovery_shape": initial_shape,
        "recovery_building_at_initial_hard": building,
        "immediate_action": "HARD_IOC_COUNTERFACTUAL",
        "checkpoints": checkpoints,
        "proposed_60s_action": proposed_action,
        "final_canonical_fifo_pnl": final_pnl,
        "final_label": (
            "profit" if final_pnl is not None and final_pnl > 0
            else "loss" if final_pnl is not None and final_pnl < 0
            else "flat" if final_pnl == 0 else "missing"
        ),
        "limits": [
            "read-only counterfactual", "no passive-fill inference",
            "no IOC-fill-price inference", "no live authority",
        ],
    }


def _raw_l2_window(
    conn: sqlite3.Connection, *, coin: str, center: datetime, before_sec: int = 90,
) -> list[tuple[datetime, float, float, float]]:
    """Read one bounded raw-L2 interval, never a whole-journal raw scan."""
    start = center - timedelta(seconds=before_sec)
    rows = conn.execute(
        """SELECT ts,payload_json FROM strategy_events
           WHERE event_type='OUTCOME_WS_L2_BOOK' AND ts >= ? AND ts <= ? ORDER BY id""",
        (start.isoformat(), center.isoformat()),
    ).fetchall()
    values: list[tuple[datetime, float, float, float]] = []
    for ts, raw in rows:
        payload = _payload(raw)
        data = payload.get("raw", {}).get("data", {}) if isinstance(payload.get("raw"), dict) else {}
        levels = data.get("levels") if isinstance(data, dict) else None
        if str(data.get("coin") or "") != coin or not isinstance(levels, list) or len(levels) < 2:
            continue
        try:
            bid, ask = float(levels[0][0]["px"]), float(levels[1][0]["px"])
            depth = sum(float(level["sz"]) for level in levels[0][:3])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        at = _timestamp(str(ts))
        if at is not None and bid > 0 and ask > bid and depth > 0:
            values.append((at, bid, ask, depth))
    return values


def _target_observation(
    observations: list[tuple[datetime, dict[str, Any]]], *, entry_lifecycle_id: str,
    narrow_exits: list[dict[str, Any]],
) -> tuple[datetime, str] | None:
    """Use the actual narrow submit when present; otherwise first -10% path boundary."""
    exact_exit = next((
        row for row in narrow_exits
        if entry_lifecycle_id.startswith(f"official_buy:{row.get('entry_order_id')}:")
    ), None)
    if exact_exit is not None:
        target = _timestamp(str(exact_exit.get("submit_ts") or ""))
        return (target, "actual_narrow_ioc_submit") if target is not None else None
    for at, payload in observations:
        net = _number(payload.get("marketable_net_exit_vs_entry_pct") or payload.get("net_exit_vs_entry_pct"))
        if net is not None and net <= -0.10:
            return at, "first_executable_drawdown_at_or_below_10pct"
    return None


def _thesis_state(payload: dict[str, Any], *, side_index: int | None = None) -> str:
    bps = _number(payload.get("spot_strike_bps"))
    side = side_index if side_index in (0, 1) else payload.get("entry_side_index")
    if bps is None or side not in (0, 1):
        return "unavailable"
    return "failed" if (side == 0 and bps <= 0) or (side == 1 and bps >= 0) else "intact"


def _raw_l2_outcomes(
    conn: sqlite3.Connection, *, exits: list[dict[str, Any]], horizon_sec: int,
) -> dict[str, dict[str, Any]]:
    """Optional, potentially slow raw WS inspection for a small known exit set."""
    wanted = {(row["coin"], row["exit_ts"]) for row in exits}
    parsed: dict[tuple[str, str], list[tuple[int, float, float, float]]] = defaultdict(list)
    # ``strategy_events`` is intentionally append-only and can be large.  This
    # optional mode is for an offline copy / stopped writer, never the live tick.
    for (raw,) in conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_WS_L2_BOOK'"):
        event = _payload(raw)
        data = event.get("raw", {}).get("data", {}) if isinstance(event.get("raw"), dict) else {}
        if not isinstance(data, dict):
            continue
        coin, timestamp = str(data.get("coin") or ""), data.get("time")
        if not isinstance(timestamp, int):
            continue
        levels = data.get("levels")
        if not isinstance(levels, list) or len(levels) < 2 or not levels[0] or not levels[1]:
            continue
        try:
            bid, ask = float(levels[0][0]["px"]), float(levels[1][0]["px"])
            depth = sum(float(level["sz"]) for level in levels[0][:3])
        except (KeyError, TypeError, ValueError):
            continue
        for target_coin, exit_ts in wanted:
            exit_at = _timestamp(exit_ts)
            if target_coin == coin and exit_at is not None:
                delta_ms = timestamp - int(exit_at.timestamp() * 1000)
                if -30_000 <= delta_ms <= horizon_sec * 1000:
                    parsed[(coin, exit_ts)].append((timestamp, bid, ask, depth))
    outcomes: dict[str, dict[str, Any]] = {}
    for row in exits:
        values = sorted(parsed.get((row["coin"], row["exit_ts"]), []))
        exit_at = _timestamp(row["exit_ts"])
        if exit_at is None or not values:
            outcomes[row["exit_order_id"]] = {"raw_ws_status": "insufficient_raw_ws_coverage"}
            continue
        base = int(exit_at.timestamp() * 1000)
        bids = [item[1] for item in values if item[0] >= base]
        outcomes[row["exit_order_id"]] = {
            "raw_ws_status": "available",
            "raw_ws_samples": len(values),
            "best_bid_at_or_after_exit": next((item[1] for item in values if item[0] >= base), None),
            "best_bid_30s": _summary(item[1] for item in values if base <= item[0] <= base + 30_000),
            "best_bid_60s": _summary(item[1] for item in values if base <= item[0] <= base + 60_000),
            "best_bid_180s": _summary(item[1] for item in values if base <= item[0] <= base + 180_000),
            "bid_update_direction_flips": _sign_flips(
                current - prior for prior, current in zip(bids, bids[1:])
            ),
        }
    return outcomes


def report(
    db_path: str | Path, *, period: str = "1d", monitor_window_sec: int = 90,
    include_raw_ws: bool = False, raw_ws_horizon_sec: int = 180,
    include_control_raw: bool = False, control_cases: set[str] | None = None,
) -> dict[str, Any]:
    """Build factual exit/re-entry and pre-exit risk summaries from one journal."""
    path = Path(db_path)
    result: dict[str, Any] = {
        "report": "outcome_exit_decision_replay_v1",
        "live_authority": False,
        "period": period,
        "monitor_window_sec": monitor_window_sec,
        "raw_ws_requested": include_raw_ws,
        "control_raw_requested": include_control_raw,
        "episodes": [],
        "control_cases": [],
        "recovery_probe_cases": [],
        "blockers": [],
        "limits": [
            "This report labels observed conditions; it never promotes a live exit threshold.",
            "A post-exit recovery is factual only when raw WS coverage is available; UI or settlement prices are not substituted.",
            "RECOVERY_BUILDING is a 60-second read-only counterfactual, not a live HOLD_FOR_RECOVERY veto.",
        ],
    }
    if not path.exists():
        result["blockers"].append("journal_missing")
        return result
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"order_events", "strategy_events"}.issubset(tables):
            result["blockers"].append("required_journal_tables_missing")
            return result
        order_rows = conn.execute(
            "SELECT id,ts,event_type,venue_order_id,side,price,qty,instrument_id,payload_json FROM order_events ORDER BY id"
        ).fetchall()
        realized_by_open_trade: dict[str, float] = {}
        if "outcome_realized_pnl_lots" in tables:
            for open_trade_id, pnl in conn.execute(
                "SELECT open_trade_id,SUM(CAST(realized_net_usdc AS REAL)) "
                "FROM outcome_realized_pnl_lots GROUP BY open_trade_id"
            ):
                parsed = _number(pnl)
                if parsed is not None:
                    realized_by_open_trade[str(open_trade_id)] = parsed
        exits: list[dict[str, Any]] = []
        for event_id, ts, event_type, order_id, side, price, qty, instrument, raw in order_rows:
            payload = _payload(raw)
            if (event_type == "ORDER_SUBMIT" and side == "SELL"
                    and payload.get("execution_type") == NARROW_EXECUTION_TYPE):
                exits.append({
                    "submit_event_id": int(event_id), "submit_ts": ts,
                    "exit_order_id": str(order_id or ""), "coin": str(instrument or payload.get("coin") or ""),
                    "outcome_id": payload.get("outcome_id"), "planned_net_return_pct": payload.get("planned_net_return_pct"),
                })
        for row in exits:
            fill = next((item for item in order_rows if item[2] == "ORDER_FILLED" and item[3] == row["exit_order_id"] and item[4] == "SELL"), None)
            if fill is None:
                row.update({"exit_ts": row["submit_ts"], "exit_fill_price": None, "exit_fill_qty": None, "exit_fill_status": "not_confirmed"})
            else:
                row.update({"exit_ts": fill[1], "exit_fill_price": fill[5], "exit_fill_qty": fill[6], "exit_fill_status": "official_fill"})
            prior_buys = [item for item in order_rows if item[2] == "ORDER_FILLED" and item[4] == "BUY" and item[7] == row["coin"] and item[1] <= row["exit_ts"]]
            if prior_buys:
                buy = prior_buys[-1]
                row.update({"entry_ts": buy[1], "entry_price": buy[5], "entry_qty": buy[6], "entry_order_id": buy[3]})
            else:
                row.update({"entry_ts": None, "entry_price": None, "entry_qty": None, "entry_order_id": None})
            after_buys = [item for item in order_rows if item[2] == "ORDER_FILLED" and item[4] == "BUY" and item[7] == row["coin"] and item[1] > row["exit_ts"]]
            if after_buys:
                buy = after_buys[0]
                exit_at, reentry_at = _timestamp(row["exit_ts"]), _timestamp(buy[1])
                row["first_reentry"] = {
                    "fill_ts": buy[1], "fill_price": buy[5], "quantity": buy[6],
                    "seconds_after_exit": ((reentry_at - exit_at).total_seconds() if exit_at and reentry_at else None),
                }
            else:
                row["first_reentry"] = None

        events: list[tuple[str, str, dict[str, Any]]] = []
        placeholders = ",".join("?" for _ in RISK_EVENTS)
        for ts, event_type, raw in conn.execute(
            f"SELECT ts,event_type,payload_json FROM strategy_events WHERE event_type IN ({placeholders}) ORDER BY id", RISK_EVENTS
        ):
            events.append((str(ts), str(event_type), _payload(raw)))

        for row in exits:
            exit_at = _timestamp(row["exit_ts"])
            if exit_at is None:
                row["pre_exit_monitor"] = {"status": "invalid_exit_timestamp"}
                continue
            earliest = exit_at - timedelta(seconds=monitor_window_sec)
            monitor = [payload for ts, event_type, payload in events
                       if event_type == "OUTCOME_MARKET_RISK_MONITOR_SHADOW"
                       and payload.get("outcome_id") == row["outcome_id"]
                       and payload.get("coin") == row["coin"]
                       and payload.get("period") == period
                       and (observed := _timestamp(ts)) is not None and earliest <= observed <= exit_at]
            velocities = [_number((item.get("bid_velocity_bps") or {}).get("30")) for item in monitor if isinstance(item.get("bid_velocity_bps"), dict)]
            depths = [_number(item.get("top3_depth")) for item in monitor]
            spreads = [_number(item.get("spread_bps")) for item in monitor]
            velocities = [value for value in velocities if value is not None]
            depths = [value for value in depths if value is not None]
            spreads = [value for value in spreads if value is not None]
            row["pre_exit_monitor"] = {
                "status": "available" if monitor else "insufficient_monitor_coverage",
                "samples": len(monitor),
                "states": sorted({str(item.get("state") or "") for item in monitor if item.get("state")}),
                "reversal_states": sorted({str(item.get("reversal_state") or "") for item in monitor if item.get("reversal_state")}),
                "bid_velocity_30_bps": _summary(velocities),
                "bid_velocity_sign_flips": _sign_flips(velocities),
                "top3_depth": _summary(depths),
                "spread_bps": _summary(spreads),
                "replay_label": _chop_label(samples=len(monitor), velocities=velocities, depths=depths, spreads=spreads),
            }

        holding_paths: dict[str, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
        monitor_paths: dict[str, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
        for ts, event_type, payload in events:
            if payload.get("period") != period:
                continue
            entry_lifecycle_id = str(payload.get("entry_lifecycle_id") or "")
            observed = _timestamp(ts)
            if not entry_lifecycle_id or observed is None:
                continue
            if event_type == "OUTCOME_HOLDING_PATH_OBSERVATION":
                holding_paths[entry_lifecycle_id].append((observed, payload))
            elif event_type in {"OUTCOME_MARKET_RISK_MONITOR_SHADOW", "OUTCOME_CRASH_CIRCUIT_SHADOW"}:
                monitor_paths[entry_lifecycle_id].append((observed, payload))
        for values in holding_paths.values():
            values.sort(key=lambda item: item[0])
        for values in monitor_paths.values():
            values.sort(key=lambda item: item[0])

        # Reuse the existing full-depth boundary records.  This does not scan
        # raw L2, mutate the DB, or reconstruct missing path points.
        holding_risk_paths: dict[str, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
        for ts, event_type, payload in events:
            if event_type != "OUTCOME_HOLDING_RISK_DECISION_SHADOW" or payload.get("period") != period:
                continue
            lifecycle_id = str(payload.get("entry_lifecycle_id") or "")
            observed = _timestamp(ts)
            if lifecycle_id and observed is not None:
                holding_risk_paths[lifecycle_id].append((observed, payload))
        for lifecycle_id, rows in holding_risk_paths.items():
            rows.sort(key=lambda item: item[0])
            # Official lifecycle ids end in the immutable BUY fill id, which
            # is the FIFO table's open_trade_id.  Do not fall back to outcome
            # id or coin when this identity is absent.
            parts = lifecycle_id.rsplit(":", 1)
            final_pnl = realized_by_open_trade.get(parts[1]) if len(parts) == 2 else None
            case = _recovery_probe_case(lifecycle_id, rows, final_pnl=final_pnl)
            if case is not None:
                result["recovery_probe_cases"].append(case)

        control_inputs: list[tuple[dict[str, str], list[tuple[datetime, dict[str, Any]]], datetime | None, str | None, dict[str, Any]]] = []
        for control in CONTROL_LIFECYCLES:
            if control_cases is not None and control["case"] not in control_cases:
                continue
            lifecycle_id = control["entry_lifecycle_id"]
            observations = holding_paths.get(lifecycle_id, [])
            selected = _target_observation(
                observations, entry_lifecycle_id=lifecycle_id, narrow_exits=exits,
            )
            if selected is None:
                control_inputs.append((control, observations, None, None, {}))
                continue
            target, selection_basis = selected
            nearest = next((payload for observed, payload in reversed(observations) if observed <= target), {})
            control_inputs.append((control, observations, target, selection_basis, nearest))

        # The raw recorder table is multi-GB and indexes only event type.  One
        # bounded OR query is therefore materially safer than six individual
        # scans.  This remains read-only and loads only the six predeclared
        # ninety-second windows.
        raw_windows = [
            (str(nearest.get("coin") or ""), target - timedelta(seconds=90), target)
            for _, _, target, _, nearest in control_inputs if target is not None and nearest.get("coin")
        ]
        raw_by_window: dict[tuple[str, datetime], list[tuple[datetime, float, float, float]]] = defaultdict(list)
        if include_control_raw and raw_windows:
            # First locate the narrow id bands without reading any large JSON
            # payload.  ``strategy_events`` has no ts index, while ``id`` is
            # the primary key; this prevents a raw-payload scan for every
            # control window on a multi-GB live journal.
            id_windows: list[tuple[int, int]] = []
            for _, start, end in raw_windows:
                bounds = conn.execute(
                    "SELECT MIN(id),MAX(id) FROM strategy_events WHERE ts >= ? AND ts <= ?",
                    (start.isoformat(), end.isoformat()),
                ).fetchone()
                if bounds is not None and bounds[0] is not None and bounds[1] is not None:
                    id_windows.append((int(bounds[0]), int(bounds[1])))
            clauses = " OR ".join("(id >= ? AND id <= ?)" for _ in id_windows)
            if not clauses:
                id_windows = []
            parameters = [value for start_id, end_id in id_windows for value in (start_id, end_id)]
            raw_rows = conn.execute(
                f"SELECT ts,payload_json FROM strategy_events WHERE event_type='OUTCOME_WS_L2_BOOK' AND ({clauses}) ORDER BY id",
                parameters,
            ).fetchall() if id_windows else []
            for ts, raw in raw_rows:
                payload = _payload(raw)
                data = payload.get("raw", {}).get("data", {}) if isinstance(payload.get("raw"), dict) else {}
                levels = data.get("levels") if isinstance(data, dict) else None
                recorded_at = _timestamp(str(ts))
                if not isinstance(levels, list) or len(levels) < 2 or recorded_at is None:
                    continue
                try:
                    bid, ask = float(levels[0][0]["px"]), float(levels[1][0]["px"])
                    depth = sum(float(level["sz"]) for level in levels[0][:3])
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                coin = str(data.get("coin") or "")
                if bid <= 0 or ask <= bid or depth <= 0:
                    continue
                for target_coin, start, end in raw_windows:
                    if coin == target_coin and start <= recorded_at <= end:
                        raw_by_window[(coin, end)].append((recorded_at, bid, ask, depth))

        control_results: list[dict[str, Any]] = []
        for control, observations, target, selection_basis, nearest in control_inputs:
            lifecycle_id = control["entry_lifecycle_id"]
            row: dict[str, Any] = {
                "case": control["case"], "entry_lifecycle_id": lifecycle_id,
                "expected": control["expected"], "holding_observation_count": len(observations),
            }
            if target is None:
                row.update({"classification": "insufficient_data", "reason": "no_exact_hard_boundary_in_holding_path"})
                control_results.append(row)
                continue
            coin = str(nearest.get("coin") or "")
            raw = raw_by_window.get((coin, target), [])
            bids = [item[1] for item in raw]
            depths = [item[3] for item in raw]
            spreads = [(item[2] / item[1] - 1.0) * 10_000 for item in raw]
            entry_side = next((item.get("entry_side_index") for _, item in observations if item.get("entry_side_index") in (0, 1)), None)
            monitor = next((
                item for observed, item in reversed(monitor_paths.get(lifecycle_id, []))
                if observed <= target and (target - observed).total_seconds() <= 15
            ), {})
            thesis = _thesis_state(monitor, side_index=entry_side)
            label = _control_label(bids=bids, depths=depths, spreads=spreads, thesis_state=thesis)
            expected = control["expected"]
            matches = (label == "persistent_or_unresolved_deterioration" if expected == "persistent"
                       else label == "chop_recovery_candidate")
            row.update({
                "target_ts": target.isoformat(), "selection_basis": selection_basis,
                "coin": coin or None, "raw_l2_samples": len(raw),
                "trend_efficiency": _trend_efficiency(bids),
                "bid_direction_flips": _sign_flips([current - prior for prior, current in zip(bids, bids[1:])]),
                "top3_depth": _summary(depths), "spread_bps": _summary(spreads),
                "thesis_state": thesis, "classification": label, "matches_expected": matches,
            })
            control_results.append(row)
        result["control_cases"] = control_results
        result["control_validation"] = {
            "status": (
                "passed" if control_results and all(item.get("matches_expected") is True for item in control_results)
                else "not_passed"
            ),
            "required": "all exact tail and recovery controls must match before a shadow veto is considered",
            "raw_l2_required": True,
        }
        if include_raw_ws:
            raw_outcomes = _raw_l2_outcomes(conn, exits=exits, horizon_sec=raw_ws_horizon_sec)
            for row in exits:
                row["post_exit_raw_ws"] = raw_outcomes.get(row["exit_order_id"], {"raw_ws_status": "missing"})
        result["episodes"] = exits
        result["episode_count"] = len(exits)
        probe_cases = result["recovery_probe_cases"]
        result["recovery_probe_summary"] = {
            "probe_sec": RECOVERY_PROBE_SEC,
            "checkpoint_tolerance_sec": RECOVERY_PROBE_TOLERANCE_SEC,
            "hard_candidates": len(probe_cases),
            "initial_recovery_building": sum(bool(item["recovery_building_at_initial_hard"]) for item in probe_cases),
            "would_hold_after_probe": sum(
                item["proposed_60s_action"] == "HOLD_FOR_RECOVERY_AFTER_60S_COUNTERFACTUAL"
                for item in probe_cases
            ),
            "would_ioc_after_probe": sum(
                item["proposed_60s_action"] == "HARD_IOC_AFTER_60S_PROBE_COUNTERFACTUAL"
                for item in probe_cases
            ),
            "lost_capacity_or_missing_checkpoint": sum(
                item["proposed_60s_action"] in {
                    "RECOVERY_PROBE_LOST_CAP_OR_DEPTH_CAPACITY",
                    "RECOVERY_PROBE_INSUFFICIENT_CHECKPOINT_DATA",
                }
                for item in probe_cases
            ),
        }
        if not exits:
            result["blockers"].append("no_narrow_hard_failure_ioc_submits")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome hard-exit decision replay")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--monitor-window-sec", type=int, default=90)
    parser.add_argument("--include-raw-ws", action="store_true")
    parser.add_argument("--raw-ws-horizon-sec", type=int, default=180)
    parser.add_argument("--include-control-raw", action="store_true",
                        help="Read bounded raw-L2 windows for the fixed exact-lifecycle control set.")
    parser.add_argument("--control-case", action="append", default=None,
                        help="One fixed control-case name; repeatable, useful for bounded offline reads.")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period, monitor_window_sec=args.monitor_window_sec,
                            include_raw_ws=args.include_raw_ws, raw_ws_horizon_sec=args.raw_ws_horizon_sec,
                            include_control_raw=args.include_control_raw,
                            control_cases=set(args.control_case) if args.control_case else None), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
