"""B1: compact, leak-free decision rows for the active Outcome challenger.

The source is the derived X3 table, never the live exchange.  Dense quote
snapshots are sampled at a fixed event-time cadence and labels use only
future executable bids already attached by the X3 builder.  Path labels are
therefore discrete-horizon observations, not claims about an unobserved
continuous path or maker queue fills.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from bot.outcome_oi_features import FEATURE_SCHEMA_VERSION, LABEL_HORIZONS_SEC


ACTIVE_DATASET_SCHEMA_VERSION = 1
ACTIVE_FEATURE_NAMES = (
    "side_mid",
    "side_spread",
    "side_probability_distance",
    "time_left_fraction",
    "signed_mark_5m_bps",
    "signed_mark_15m_bps",
    "signed_mark_60m_bps",
    "oi_activity_5m_bps",
)


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def feature_vector(features: Mapping[str, object], side_index: int) -> tuple[float, ...] | None:
    prefix = "yes" if side_index == 0 else "no"
    bid, ask = _finite(features.get(f"{prefix}_bid")), _finite(features.get(f"{prefix}_ask"))
    time_left = _finite(features.get("time_left_sec"))
    marks = tuple(_finite(features.get(f"btc_mark_return_{seconds}s_bps")) for seconds in (300, 900, 3600))
    oi = _finite(features.get("oi_return_300s_bps"))
    if bid is None or ask is None or time_left is None or oi is None or any(value is None for value in marks):
        return None
    if not 0 < bid < ask < 1 or time_left < 0:
        return None
    direction = 1.0 if side_index == 0 else -1.0
    midpoint = (bid + ask) / 2.0
    return (
        midpoint,
        ask - bid,
        midpoint - 0.5,
        min(time_left, 86_400.0) / 86_400.0,
        direction * float(marks[0]),
        direction * float(marks[1]),
        direction * float(marks[2]),
        abs(oi),
    )


@dataclass(frozen=True)
class ActiveDecisionRow:
    outcome_id: int
    timestamp_ms: int
    side_index: int
    bid: float
    ask: float
    vector: tuple[float, ...]
    targets: dict[str, float | int | bool | None]


def _targets(features: Mapping[str, object], labels: Mapping[str, object], side_index: int) -> dict[str, float | int | bool | None]:
    prefix = "yes" if side_index == 0 else "no"
    ask = _finite(features.get(f"{prefix}_ask"))
    if ask is None or ask <= 0:
        return {}
    future: list[tuple[int, float]] = []
    result: dict[str, float | int | bool | None] = {}
    for horizon in LABEL_HORIZONS_SEC:
        label = labels.get(f"future_{horizon}s")
        label = label if isinstance(label, Mapping) else {}
        bid = _finite(label.get(f"{prefix}_future_bid")) if label.get("available") is True else None
        result[f"future_bid_{horizon}s"] = bid
        if bid is not None:
            future.append((horizon, bid / ask - 1.0))
    if not future:
        return result
    returns = [value for _, value in future]
    result["observed_horizon_mfe"] = max(returns)
    result["observed_horizon_mae"] = min(returns)
    for threshold in (1, 2, 5):
        hit = next((horizon for horizon, value in future if value >= threshold / 100.0), None)
        result[f"hit_plus_{threshold}pct_1h"] = hit is not None
        result[f"time_to_plus_{threshold}pct_sec"] = hit
    for threshold in (5, 10, 20):
        breach_index = next((index for index, (_, value) in enumerate(future) if value <= -threshold / 100.0), None)
        result[f"breach_minus_{threshold}pct_1h"] = breach_index is not None
        result[f"time_to_minus_{threshold}pct_sec"] = future[breach_index][0] if breach_index is not None else None
        if breach_index is not None:
            result[f"recovered_after_minus_{threshold}pct_1h"] = any(value >= 0 for _, value in future[breach_index + 1 :])
        else:
            result[f"recovered_after_minus_{threshold}pct_1h"] = None
    return result


def load_decision_rows(
    db_path: str | Path,
    *,
    period: str = "1d",
    sample_interval_sec: int = 60,
) -> list[ActiveDecisionRow]:
    path = Path(db_path)
    if not path.exists():
        return []
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "outcome_oi_feature_rows" not in tables:
            return []
        source = conn.execute(
            """SELECT outcome_id,snapshot_timestamp_ms,features_json,labels_json
               FROM outcome_oi_feature_rows
               WHERE feature_schema_version=? AND period=? AND oi_backfilled=0
                 AND oi_observation_id IS NOT NULL
               ORDER BY outcome_id,snapshot_timestamp_ms""",
            (FEATURE_SCHEMA_VERSION, period),
        ).fetchall()
    output: list[ActiveDecisionRow] = []
    last: dict[tuple[int, int], int] = {}
    interval_ms = max(1, int(sample_interval_sec)) * 1000
    for raw_outcome_id, raw_timestamp, raw_features, raw_labels in source:
        try:
            outcome_id, timestamp_ms = int(raw_outcome_id), int(raw_timestamp)
            features, labels = json.loads(raw_features), json.loads(raw_labels)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(features, dict) or not isinstance(labels, dict):
            continue
        for side_index, prefix in ((0, "yes"), (1, "no")):
            key = (outcome_id, side_index)
            if timestamp_ms - last.get(key, -interval_ms) < interval_ms:
                continue
            vector = feature_vector(features, side_index)
            bid, ask = _finite(features.get(f"{prefix}_bid")), _finite(features.get(f"{prefix}_ask"))
            if vector is None or bid is None or ask is None:
                continue
            targets = _targets(features, labels, side_index)
            if targets.get("future_bid_300s") is None:
                continue
            output.append(ActiveDecisionRow(outcome_id, timestamp_ms, side_index, bid, ask, vector, targets))
            last[key] = timestamp_ms
    return output


def dataset_report(db_path: str | Path, *, period: str = "1d", sample_interval_sec: int = 60) -> dict[str, Any]:
    rows = load_decision_rows(db_path, period=period, sample_interval_sec=sample_interval_sec)
    return {
        "report": "outcome_active_decision_dataset",
        "schema_version": ACTIVE_DATASET_SCHEMA_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "period": period,
        "sample_interval_sec": sample_interval_sec,
        "rows": len(rows),
        "market_instances": len({row.outcome_id for row in rows}),
        "sides": {str(side): sum(row.side_index == side for row in rows) for side in (0, 1)},
        "label_coverage": {
            name: sum(row.targets.get(name) is not None for row in rows)
            for name in ("future_bid_300s", "future_bid_900s", "future_bid_1800s", "future_bid_3600s", "observed_horizon_mfe", "observed_horizon_mae")
        },
        "limitations": [
            "Rows are event-time sampled and grouped by independent daily market for validation.",
            "MFE/MAE and threshold labels are observed only at X3 label horizons, not continuously.",
            "No maker touch is treated as a fill and no settlement label is fabricated.",
        ],
        "live_authority": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build/report B1 active-decision rows")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--sample-interval-sec", type=int, default=60)
    args = parser.parse_args()
    print(json.dumps(dataset_report(args.db, period=args.period, sample_interval_sec=args.sample_interval_sec), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
