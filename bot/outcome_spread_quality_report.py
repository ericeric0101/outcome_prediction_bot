"""Read-only spread-quality evidence for Outcome entries and rejected candidates."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.outcome_execution_quality_report import report as execution_report


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _bucket(value: object) -> str:
    bps = _decimal(value)
    if bps is None:
        return "unknown"
    for ceiling, name in ((Decimal("50"), "0_to_50"), (Decimal("100"), "50_to_100"),
                          (Decimal("125"), "100_to_125"), (Decimal("175"), "125_to_175"),
                          (Decimal("250"), "175_to_250")):
        if bps <= ceiling:
            return name
    return "250_plus"


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    """Compare actual submits with sampled wide-spread non-submitted candidates."""
    execution = execution_report(db_path, period=period)
    submitted: dict[str, dict[str, Any]] = defaultdict(lambda: {"submits": 0, "filled_entries": 0, "realized_net_usdc": "0"})
    for row in execution["entries"]:
        bucket = _bucket(row.get("spread_bps"))
        aggregate = submitted[bucket]
        aggregate["submits"] += 1
        if _decimal(row.get("filled_shares")) not in (None, Decimal("0")):
            aggregate["filled_entries"] += 1
        pnl = _decimal(row.get("canonical_realized_net_usdc"))
        if pnl is not None:
            aggregate["realized_net_usdc"] = str(Decimal(aggregate["realized_net_usdc"]) + pnl)

    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        try:
            candidates = conn.execute(
                """SELECT candidate_id, payload_json FROM outcome_wide_spread_candidates
                   WHERE period=? ORDER BY observed_at_ms""", (period,),
            ).fetchall()
            paths = conn.execute(
                """SELECT candidate_id, horizon_sec, best_bid, best_ask, observed_at_ms
                   FROM outcome_wide_spread_candidate_paths"""
            ).fetchall()
            candidate_schema_available = True
        except sqlite3.OperationalError:
            # A running pre-deployment process has not initialised the new
            # schema yet. Report historical submitted evidence normally and
            # make the absence explicit rather than failing or inventing data.
            candidates, paths, candidate_schema_available = [], [], False
    paths_by_candidate: dict[str, dict[str, Any]] = defaultdict(dict)
    for row in paths:
        paths_by_candidate[str(row["candidate_id"])][str(row["horizon_sec"])] = {
            "best_bid": row["best_bid"], "best_ask": row["best_ask"], "observed_at_ms": row["observed_at_ms"],
        }
    candidate_rows: list[dict[str, Any]] = []
    candidate_buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"sampled_candidates": 0, "path_5m": 0, "path_15m": 0, "path_30m": 0})
    for raw in candidates:
        payload = _payload(raw["payload_json"])
        bucket = _bucket(payload.get("entry_spread_bps"))
        candidate_buckets[bucket]["sampled_candidates"] += 1
        path = paths_by_candidate[str(raw["candidate_id"])]
        for horizon, key in (("300", "path_5m"), ("900", "path_15m"), ("1800", "path_30m")):
            if horizon in path:
                candidate_buckets[bucket][key] += 1
        candidate_rows.append({
            "candidate_id": raw["candidate_id"], "spread_bps": payload.get("entry_spread_bps"),
            "spread_bucket": bucket, "entry_tier": payload.get("entry_tier"),
            "time_left_sec": payload.get("time_left_sec"), "regime_state": payload.get("regime_state"),
            "top3_depth_shares": payload.get("top3_depth_shares"),
            "recent_trade_shares_5m": payload.get("recent_trade_shares_5m"), "forward_bbo": path,
        })
    return {
        "report": "outcome_spread_quality", "schema_version": 1, "period": period,
        "submitted_by_spread_bucket": [{"spread_bucket": key, **value} for key, value in sorted(submitted.items())],
        "wide_spread_candidate_by_bucket": [{"spread_bucket": key, **value} for key, value in sorted(candidate_buckets.items())],
        "wide_spread_candidates": candidate_rows,
        "wide_spread_candidate_collection_schema_available": candidate_schema_available,
        "limits": [
            "No spread ceiling is changed by this report.",
            "Candidate rows are read-only samples at most once per coin per five minutes; forward BBOs are observed at approximately 5/15/30 minutes from accepted P2 snapshots.",
            "A future BBO path is execution-quality evidence, not a counterfactual fill or PnL claim.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome spread-quality report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
