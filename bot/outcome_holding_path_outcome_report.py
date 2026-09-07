"""Read-only lifecycle-level Outcome holding-path outcome report.

Version-1 telemetry was keyed only by (outcome, coin), so it could not tell
whether a later rebound belonged to a particular entry.  This report accepts
only version-2 observations bound to one immutable official BUY trade id.
Ambiguous legacy or composite inventory is counted as excluded, never guessed.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


_TAIPEI = ZoneInfo("Asia/Taipei")
_THRESHOLDS = (Decimal("-0.05"), Decimal("-0.10"), Decimal("-0.20"))
_TWO_HOURS_SEC = 2 * 60 * 60


def _time_left_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 2 * 60 * 60:
        return "under_2h"
    if seconds < 6 * 60 * 60:
        return "2h_to_6h"
    if seconds < 12 * 60 * 60:
        return "6h_to_12h"
    return "12h_plus"


def _same_side_reconfirmed(payload: dict[str, Any]) -> bool:
    try:
        side = int(payload["entry_side_index"])
        variants = payload.get("oi_evidence", {}).get("gate_variants", {})
    except (TypeError, ValueError, KeyError):
        return False
    for name in ("spot_mark_oi", "spot_mark"):
        item = variants.get(name)
        if isinstance(item, dict) and item.get("eligible") is True and int(item.get("side_index")) == side:
            return True
    return False


def _close_results(conn: sqlite3.Connection) -> dict[str, tuple[Decimal, Decimal]]:
    results: dict[str, tuple[Decimal, Decimal]] = {}
    for open_trade_id, cost, pnl in conn.execute(
        """SELECT open_trade_id, SUM(CAST(cost_usdc AS REAL)), SUM(CAST(realized_net_usdc AS REAL))
           FROM outcome_realized_pnl_lots GROUP BY open_trade_id"""
    ):
        try:
            results[str(open_trade_id)] = (Decimal(str(cost)), Decimal(str(pnl)))
        except (ValueError, ArithmeticError):
            continue
    return results


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            """SELECT ts, payload_json FROM strategy_events
               WHERE event_type='OUTCOME_HOLDING_PATH_OBSERVATION' ORDER BY id"""
        ).fetchall()
        closes = _close_results(conn)

    paths: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    excluded_legacy = 0
    for ts, raw in rows:
        try:
            payload = json.loads(raw)
            if payload.get("period") != period:
                continue
            lifecycle_id = str(payload.get("entry_lifecycle_id") or "")
            trade_id = str(payload.get("entry_trade_id") or "")
            if not lifecycle_id or not trade_id:
                excluded_legacy += 1
                continue
            Decimal(str(payload["net_exit_vs_entry_pct"]))
            paths[lifecycle_id].append((str(ts), payload))
        except (TypeError, ValueError, ArithmeticError, json.JSONDecodeError, KeyError):
            excluded_legacy += 1

    lifecycle_rows: list[dict[str, Any]] = []
    buckets: dict[tuple[str, str, str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for lifecycle_id, observations in paths.items():
        observations.sort(key=lambda item: item[0])
        first = observations[0][1]
        returns = [Decimal(str(item[1]["net_exit_vs_entry_pct"])) for item in observations]
        target = None
        try:
            target = Decimal(str(first["entry_target_return_pct"]))
        except (TypeError, ValueError, ArithmeticError, KeyError):
            pass
        try:
            filled_at = datetime.fromisoformat(str(first["entry_filled_at"])).astimezone(_TAIPEI)
            day_type = "weekend" if filled_at.weekday() >= 5 else "weekday"
            entry_date = filled_at.date().isoformat()
        except (TypeError, ValueError, KeyError):
            day_type, entry_date = "unknown", None
        threshold_rows: dict[str, dict[str, Any]] = {}
        final_cost_pnl = closes.get(str(first.get("entry_trade_id") or ""))
        final_return = None
        if final_cost_pnl is not None and final_cost_pnl[0] > 0:
            final_return = final_cost_pnl[1] / final_cost_pnl[0]
        for threshold in _THRESHOLDS:
            breach_indices = [i for i, value in enumerate(returns) if value <= threshold]
            breached = bool(breach_indices)
            after_breach = observations[breach_indices[0] + 1:] if breached else []
            later_returns = [Decimal(str(item[1]["net_exit_vs_entry_pct"])) for item in after_breach]
            recovered_cost = bool(breached and (any(value >= 0 for value in later_returns) or (final_return is not None and final_return >= 0)))
            reached_target = bool(
                breached and target is not None
                and (any(value >= target for value in later_returns) or (final_return is not None and final_return >= target))
            )
            threshold_rows[f"{int(abs(threshold) * 100)}%"] = {
                "ever_breached": breached,
                "first_breach_ts": observations[breach_indices[0]][0] if breached else None,
                "breached_after_two_hours": any(
                    Decimal(str(payload["net_exit_vs_entry_pct"])) <= threshold
                    and float(payload.get("holding_age_sec", 0)) >= _TWO_HOURS_SEC
                    for _, payload in observations
                ),
                "recovered_to_cost_after_breach": recovered_cost,
                "reached_target_after_breach": reached_target,
            }
        reconfirmed = any(
            float(payload.get("holding_age_sec", 0)) >= _TWO_HOURS_SEC and _same_side_reconfirmed(payload)
            for _, payload in observations
        )
        if final_return is None:
            final_status = "open_or_unreconciled"
        elif final_return < 0:
            final_status = "closed_loss"
        elif target is not None and final_return >= target:
            final_status = "closed_target_or_better"
        else:
            final_status = "closed_nonnegative_below_target"
        time_bucket = _time_left_bucket(first.get("entry_time_left_sec"))
        tier = str(first.get("entry_tier") or "unknown")
        row = {
            "entry_lifecycle_id": lifecycle_id,
            "outcome_id": int(first["outcome_id"]), "coin": str(first["coin"]),
            "entry_order_id": str(first.get("entry_order_id") or ""),
            "entry_trade_id": str(first.get("entry_trade_id") or ""),
            "entry_date_taipei": entry_date, "entry_day_type": day_type,
            "entry_tier": tier, "entry_time_left_bucket": time_bucket,
            "entry_time_left_sec": first.get("entry_time_left_sec"),
            "observations": len(observations), "mae_pct": str(min(returns)), "mfe_pct": str(max(returns)),
            "same_side_reconfirmed_after_two_hours": reconfirmed,
            "thresholds": threshold_rows, "final_return_pct": str(final_return) if final_return is not None else None,
            "final_status": final_status,
        }
        lifecycle_rows.append(row)
        bucket = (day_type, tier, "reconfirmed" if reconfirmed else "not_reconfirmed", time_bucket)
        aggregate = buckets[bucket]
        aggregate["lifecycles"] += 1
        aggregate[final_status] += 1
        for label, values in threshold_rows.items():
            if values["ever_breached"]:
                aggregate[f"breached_{label}"] += 1
            if values["recovered_to_cost_after_breach"]:
                aggregate[f"recovered_cost_after_{label}"] += 1
            if values["reached_target_after_breach"]:
                aggregate[f"reached_target_after_{label}"] += 1
    return {
        "period": period,
        "schema": "holding_path_outcome_v2",
        "lifecycle_count": len(lifecycle_rows),
        "legacy_or_ambiguous_observations_excluded": excluded_legacy,
        "lifecycles": lifecycle_rows,
        "buckets": [
            {"entry_day_type": key[0], "entry_tier": key[1], "same_side_reconfirmed_after_two_hours": key[2],
             "entry_time_left_bucket": key[3], **dict(value)}
            for key, value in sorted(buckets.items())
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome lifecycle-level holding-path outcome report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
