"""Reproducible D.4 evidence report; it never changes live policy."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime


WINDOWS = (12, 24, 36, 48, 168)
MARKOUT_CONTEXT_SCHEMA_VERSION = 2


def _adverse(payload: dict) -> float:
    return max(0.0, -float(payload.get("signed_markout_ps") or 0.0))


def _summary(observations) -> dict[str, float | int] | None:
    """Summarize one first-maker-fill markout observation per market.

    A short markout is available before settlement, but D.4 cannot select a
    live policy until the corresponding market is settled.  Keep both counts
    visible rather than treating legacy or still-open markets as OOS evidence.
    """
    if not observations:
        return None
    values = [_adverse(payload) for _ts, payload in observations]
    ordered = sorted(values)
    cap = ordered[max(0, math.ceil(len(ordered) * 0.90) - 1)]
    return {
        "markout_sample_count": len(values),
        "settled_sample_count": sum(1 for _ts, payload in observations if payload["settled"]),
        "adverse_markout_per_share": sum(min(value, cap) for value in values) / len(values),
        "raw_mean_adverse_markout_per_share": sum(values) / len(values),
        "winsor_cap_per_share": cap,
    }


def _first_per_market(observations, *, cutoff: float, horizon: int):
    """Prevent several fills/ticks in one 15-minute market becoming samples."""
    first = {}
    for ts, payload in sorted(observations, key=lambda item: item[0]):
        slug = str(payload.get("slug") or "")
        if not slug or ts.timestamp() < cutoff or int(payload.get("horizon_sec") or 0) != horizon:
            continue
        first.setdefault(slug, (ts, payload))
    return list(first.values())


def _settled_slugs(conn: sqlite3.Connection, slugs: set[str]) -> set[str]:
    """Return markets with a journaled settlement after a D.4 candidate fill.

    Settlement payloads are JSON and the historical journal has no slug index,
    so filter the small result set in Python.  This remains read-only and avoids
    relying on an undocumented payload shape beyond the existing ``slug`` key.
    """
    if not slugs:
        return set()
    rows = conn.execute(
        "select payload_json from strategy_events where event_type='MARKET_SETTLEMENT'"
    ).fetchall()
    settled = set()
    for (raw,) in rows:
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            continue
        slug = str(payload.get("slug") or "") if isinstance(payload, dict) else ""
        if slug in slugs:
            settled.add(slug)
    return settled


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="./logs/trade_journal.db")
    parser.add_argument("--min-samples", type=int, default=30)
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute(
        "select ts, payload_json from order_events where event_type='FILL_MARKOUT' and side='BUY'"
    ).fetchall()
    observations = []
    for ts, raw in rows:
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            continue
        if (
            payload.get("liquidity_class") != "maker"
            or int(payload.get("markout_context_schema_version") or 0) != MARKOUT_CONTEXT_SCHEMA_VERSION
            or int(payload.get("horizon_sec") or 0) not in (10, 30)
        ):
            continue
        observations.append((datetime.fromisoformat(ts), payload))
    if not observations:
        print(json.dumps({"status": "no_maker_buy_markouts"}, indent=2))
        return 1
    settled_slugs = _settled_slugs(conn, {str(payload.get("slug") or "") for _ts, payload in observations})
    observations = [
        (ts, {**payload, "settled": str(payload.get("slug") or "") in settled_slugs})
        for ts, payload in observations
    ]
    latest = max(ts for ts, _ in observations)
    report = {
        "markout_context_schema_version": MARKOUT_CONTEXT_SCHEMA_VERSION,
        "latest_observation": latest.isoformat(),
        "candidate_windows": {},
        "weekday_weekend": {},
    }
    for hours in WINDOWS:
        cutoff = latest.timestamp() - hours * 3600
        report["candidate_windows"][str(hours)] = {
            str(horizon): _summary(_first_per_market(observations, cutoff=cutoff, horizon=horizon))
            for horizon in (10, 30)
        }
    for weekend in (False, True):
        regime_observations = [
            (ts, payload)
            for ts, payload in _first_per_market(observations, cutoff=float("-inf"), horizon=10)
            if bool(payload.get("entry_is_weekend_utc")) == weekend
        ]
        report["weekday_weekend"]["weekend" if weekend else "weekday"] = _summary(regime_observations)
    viable = [
        hours
        for hours in (12, 24, 36, 48)
        if (report["candidate_windows"][str(hours)]["10"] or {}).get("settled_sample_count", 0)
        >= args.min_samples
    ]
    report["selection"] = {
        "selected_window_hours": None,
        "reason": "insufficient_schema_v2_settled_samples" if not viable else "requires_out_of_sample_review",
        "minimum_samples": args.min_samples,
        "eligible_candidates": viable,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
