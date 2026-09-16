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
)
NARROW_EXECUTION_TYPE = "narrow_hard_failure_price_protected_fak_ioc"


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
) -> dict[str, Any]:
    """Build factual exit/re-entry and pre-exit risk summaries from one journal."""
    path = Path(db_path)
    result: dict[str, Any] = {
        "report": "outcome_exit_decision_replay_v1",
        "live_authority": False,
        "period": period,
        "monitor_window_sec": monitor_window_sec,
        "raw_ws_requested": include_raw_ws,
        "episodes": [],
        "blockers": [],
        "limits": [
            "This report labels observed conditions; it never promotes a live exit threshold.",
            "A post-exit recovery is factual only when raw WS coverage is available; UI or settlement prices are not substituted.",
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
        if include_raw_ws:
            raw_outcomes = _raw_l2_outcomes(conn, exits=exits, horizon_sec=raw_ws_horizon_sec)
            for row in exits:
                row["post_exit_raw_ws"] = raw_outcomes.get(row["exit_order_id"], {"raw_ws_status": "missing"})
        result["episodes"] = exits
        result["episode_count"] = len(exits)
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
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period, monitor_window_sec=args.monitor_window_sec,
                            include_raw_ws=args.include_raw_ws, raw_ws_horizon_sec=args.raw_ws_horizon_sec), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
