"""E1: read-only capital-time and admission-funnel report for Outcome live."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from bot.outcome_execution_quality_report import report as execution_quality_report


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError, TypeError):
        return None


def _reason_bucket(payload: dict[str, Any]) -> str:
    reason = str(payload.get("final_reason") or "unknown")
    admission = payload.get("admission_inputs")
    active = admission.get("active_current_market_count", 0) if isinstance(admission, dict) else 0
    if active or reason.startswith("exit ") or "protective" in reason or "existing Outcome" in reason:
        return "capital_committed"
    if payload.get("execution_submitted") is True:
        return "buy_submitted"
    if "directional_confirmation_not_met" in reason:
        return "no_directional_edge"
    if any(token in reason for token in ("spread_exceeds", "depth", "safe_capacity", "price drift")):
        return "execution_quality_rejected"
    if any(token in reason for token in ("no-trade band", "target exceeds", "minimum")):
        return "pricing_rejected"
    if any(token in reason for token in (
        "market-data gate", "account recovery", "portfolio", "cooldown", "reentry", "rollover", "reduce-only",
    )):
        return "safety_blocked"
    if "entry requote" in reason or "buy_resting" in reason:
        return "buy_resting"
    return "other_idle"


def report(db_path: str | Path, *, period: str = "1d", recent_event_limit: int = 100_000) -> dict[str, Any]:
    """Attribute observed strategy time without counting process downtime."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        latest = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM strategy_events").fetchone()[0])
        rows = conn.execute(
            """SELECT id,ts,payload_json FROM strategy_events
               WHERE id>=? AND event_type='OUTCOME_ENTRY_ADMISSION_DECISION' ORDER BY id""",
            (max(1, latest - recent_event_limit + 1),),
        ).fetchall()

    observations: list[tuple[int, datetime, dict[str, Any]]] = []
    for event_id, ts, raw in rows:
        payload = _payload(raw)
        if payload.get("period") != period:
            continue
        try:
            observations.append((int(event_id), datetime.fromisoformat(str(ts)), payload))
        except ValueError:
            continue
    seconds_by_bucket: dict[str, float] = defaultdict(float)
    observations_by_bucket: dict[str, int] = defaultdict(int)
    for index, (_, ts, payload) in enumerate(observations):
        bucket = _reason_bucket(payload)
        observations_by_bucket[bucket] += 1
        if index + 1 >= len(observations):
            continue
        next_ts, next_payload = observations[index + 1][1], observations[index + 1][2]
        if next_payload.get("outcome_id") != payload.get("outcome_id"):
            continue
        # Long gaps are process downtime or unavailable evidence, not a known
        # strategy state.  Cap attribution to two ordinary 15-second WS ages.
        seconds_by_bucket[bucket] += max(0.0, min(30.0, (next_ts - ts).total_seconds()))

    execution = execution_quality_report(db_path, period=period)
    entries = execution["entries"]
    submitted = len(entries)
    filled = [row for row in entries if (_number(row.get("filled_shares")) or Decimal("0")) > 0]
    closed = [row for row in entries if row.get("lifecycle_state") == "closed"]
    holding = [float(row["holding_sec"]) for row in closed if row.get("holding_sec") is not None]
    capital_hours = Decimal("0")
    realized = Decimal("0")
    for row in closed:
        cost = _number(row.get("canonical_realized_cost"))
        pnl = _number(row.get("canonical_realized_net_usdc"))
        age = _number(row.get("holding_sec"))
        if cost is not None and pnl is not None:
            realized += pnl
        if cost is not None and age is not None:
            capital_hours += cost * age / Decimal("3600")
    total_attributed = sum(seconds_by_bucket.values())
    return {
        "report": "outcome_capital_efficiency", "schema_version": 1, "period": period,
        "event_id_window": {"first": observations[0][0] if observations else None, "last": observations[-1][0] if observations else None},
        "admission_observations": len(observations),
        "time_funnel": [
            {"state": key, "observations": observations_by_bucket[key],
             "attributed_seconds": round(seconds_by_bucket[key], 3),
             "attributed_pct": round(seconds_by_bucket[key] / total_attributed * 100, 3) if total_attributed else None}
            for key in sorted(observations_by_bucket)
        ],
        "execution": {
            "f5_buy_submits": submitted, "filled_entries": len(filled),
            "submit_to_fill_rate": len(filled) / submitted if submitted else None,
            "closed_lifecycles": len(closed),
            "median_holding_sec": median(holding) if holding else None,
            "mean_holding_sec": sum(holding) / len(holding) if holding else None,
            "realized_net_usdc": str(realized), "realized_capital_hours": str(capital_hours),
            "net_usdc_per_capital_hour": str(realized / capital_hours) if capital_hours > 0 else None,
        },
        "limits": [
            "Admission observations are correlated operational ticks, not independent trades.",
            "Attribution caps each observed interval at 30 seconds so process downtime is not invented as strategy idle time.",
            "Capital-hour efficiency includes only canonical closed FIFO lifecycles with known holding time.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome capital-efficiency report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
