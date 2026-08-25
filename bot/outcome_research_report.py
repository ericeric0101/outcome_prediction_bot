"""Read-only P2/P3 research readiness reports from the Outcome journal."""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bot.outcome_p2_quality import is_eligible_p2_snapshot


@dataclass(frozen=True)
class P2PeriodReport:
    period: str
    snapshot_count: int
    executable_buy_count: int
    executable_sell_count: int
    positive_buy_edge_count: int
    fee_evidence_complete: bool
    ready: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class P3BucketReport:
    period: str
    horizon_sec: int
    bucket: str
    actual_maker_fill_count: int
    fee_adjusted_mean_ev_per_share: float | None
    fee_adjusted_lcb95_per_share: float | None
    ready: bool
    blockers: tuple[str, ...]


def p2_report(db_path: str | Path, *, periods: tuple[str, ...], min_snapshots: int = 100) -> tuple[P2PeriodReport, ...]:
    path = Path(db_path)
    if not path.exists():
        return tuple(P2PeriodReport(period, 0, 0, 0, 0, False, False, ("journal_missing",)) for period in periods)
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_P2_PARITY_SNAPSHOT'").fetchall()
    data: list[dict[str, Any]] = []
    for (raw,) in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("venue") == "hyperliquid_outcome" and is_eligible_p2_snapshot(payload):
            data.append(payload)
    reports = []
    for period in periods:
        samples = [item for item in data if item.get("period") == period]
        buy = [item for item in samples if item.get("buy_complete_set_cost") is not None]
        sell = [item for item in samples if item.get("sell_complete_set_proceeds") is not None]
        positive = [item for item in buy if float(item.get("buy_complete_set_edge") or 0) > 0]
        fee_complete = bool(samples) and all(item.get("fee_status") == "verified_included" and item.get("fee_evidence") != "unverified_conversion_cost_excluded" for item in samples)
        blockers = []
        if len(samples) < min_snapshots:
            blockers.append(f"need_{min_snapshots}_snapshots_found_{len(samples)}")
        if not fee_complete:
            blockers.append("fee_or_conversion_cost_evidence_incomplete")
        if not buy or not sell:
            blockers.append("insufficient_two_sided_executable_depth")
        reports.append(P2PeriodReport(period, len(samples), len(buy), len(sell), len(positive), fee_complete, not blockers, tuple(blockers)))
    return tuple(reports)


def _bootstrap_lcb95(values: list[float], *, draws: int = 1000) -> float | None:
    if not values:
        return None
    rng = random.Random(0)
    means = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws))
    return means[max(0, int(draws * 0.025) - 1)]


def p3_report(db_path: str | Path, *, periods: tuple[str, ...], min_actual_fills: int = 30) -> tuple[P3BucketReport, ...]:
    path = Path(db_path)
    if not path.exists():
        return tuple(P3BucketReport(period, horizon, "none", 0, None, None, False, ("journal_missing",)) for period in periods for horizon in (1, 5, 10, 30))
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT payload_json FROM order_events WHERE event_type='FILL_MARKOUT'").fetchall()
    groups: dict[tuple[str, int, str], list[float]] = {}
    for (raw,) in rows:
        try:
            payload = json.loads(raw)
            if not (payload.get("actual_fill") is True and payload.get("executable_quote") is True and payload.get("counterfactual") is False):
                continue
            period, horizon = str(payload["period"]), int(payload["horizon_sec"])
            value = float(payload["signed_markout_ps"]) - float(payload.get("fee_per_share") or 0)
            groups.setdefault((period, horizon, str(payload.get("entry_regime_bucket") or "unknown")), []).append(value)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    reports: list[P3BucketReport] = []
    for period in periods:
        period_groups = [(key, values) for key, values in groups.items() if key[0] == period]
        if not period_groups:
            reports.extend(P3BucketReport(period, horizon, "none", 0, None, None, False, ("no_actual_maker_markouts",)) for horizon in (1, 5, 10, 30))
            continue
        for (_, horizon, bucket), values in sorted(period_groups):
            lcb = _bootstrap_lcb95(values)
            blockers = []
            if len(values) < min_actual_fills:
                blockers.append(f"need_{min_actual_fills}_actual_fills_found_{len(values)}")
            if lcb is None or lcb <= 0:
                blockers.append("fee_adjusted_lcb95_not_positive")
            reports.append(P3BucketReport(period, horizon, bucket, len(values), sum(values) / len(values), lcb, not blockers, tuple(blockers)))
    return tuple(reports)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome P2 research readiness report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--periods", default="15m,1d,1h,daily,24h")
    parser.add_argument("--min-snapshots", type=int, default=100)
    args = parser.parse_args()
    periods = tuple(p.strip() for p in args.periods.split(",") if p.strip())
    print(json.dumps({"p2": [asdict(item) for item in p2_report(args.db, periods=periods, min_snapshots=args.min_snapshots)], "p3": [asdict(item) for item in p3_report(args.db, periods=periods)]}, indent=2))


if __name__ == "__main__":
    main()
