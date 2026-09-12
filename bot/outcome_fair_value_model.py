"""E2/E3: offline executable-bid model and read-only live shadow scorer."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from bot.outcome_calendar_features import market_session_calendar_features
from bot.outcome_oi_features import FEATURE_SCHEMA_VERSION

MODEL_SCHEMA_VERSION = 2
FEATURE_NAMES = (
    "side_mid", "side_spread", "side_probability_distance", "time_left_fraction",
    "signed_mark_5m_bps", "signed_mark_15m_bps", "signed_mark_60m_bps", "oi_activity_5m_bps",
    "market_session_is_weekend", "market_session_weekday_sin", "market_session_weekday_cos",
    "weekend_side_spread", "weekend_signed_mark_5m_bps",
)


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _vector(features: Mapping[str, Any], side_index: int, *, timestamp_ms: int | None = None) -> tuple[float, ...] | None:
    prefix = "yes" if side_index == 0 else "no"
    bid, ask = _finite(features.get(f"{prefix}_bid")), _finite(features.get(f"{prefix}_ask"))
    time_left = _finite(features.get("time_left_sec"))
    marks = tuple(_finite(features.get(f"btc_mark_return_{seconds}s_bps")) for seconds in (300, 900, 3600))
    oi = _finite(features.get("oi_return_300s_bps"))
    if bid is None or ask is None or time_left is None or any(value is None for value in marks) or oi is None:
        return None
    if not 0 < bid < ask < 1 or time_left < 0:
        return None
    direction = 1.0 if side_index == 0 else -1.0
    mid = (bid + ask) / 2
    raw_timestamp = timestamp_ms if timestamp_ms is not None else _finite(features.get("observation_timestamp_ms"))
    if raw_timestamp is None:
        return None
    calendar = market_session_calendar_features(int(raw_timestamp))
    weekend = 1.0 if calendar["market_session_is_weekend"] else 0.0
    signed_mark_5m = direction * marks[0]
    return (
        mid, ask - bid, mid - 0.5, min(time_left, 86_400.0) / 86_400.0,
        signed_mark_5m, direction * marks[1], direction * marks[2], abs(oi),
        weekend, float(calendar["market_session_weekday_sin"]), float(calendar["market_session_weekday_cos"]),
        weekend * (ask - bid), weekend * signed_mark_5m,
    )


@dataclass(frozen=True)
class _Row:
    outcome_id: int
    timestamp_ms: int
    side_index: int
    vector: tuple[float, ...]
    future_bid: float


def _rows(db_path: str | Path, horizon_sec: int) -> list[_Row]:
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
               WHERE feature_schema_version=? AND period='1d' AND oi_backfilled=0
                 AND oi_observation_id IS NOT NULL ORDER BY snapshot_timestamp_ms""",
            (FEATURE_SCHEMA_VERSION,),
        ).fetchall()
    output: list[_Row] = []
    last_by_market_side: dict[tuple[int, int], int] = {}
    for outcome_id, timestamp_ms, raw_features, raw_labels in source:
        try:
            features, labels = json.loads(raw_features), json.loads(raw_labels)
        except (TypeError, json.JSONDecodeError):
            continue
        label = labels.get(f"future_{horizon_sec}s", {}) if isinstance(labels, dict) else {}
        if not isinstance(features, dict) or not isinstance(label, dict) or label.get("available") is not True:
            continue
        for side_index, prefix in ((0, "yes"), (1, "no")):
            vector = _vector(features, side_index, timestamp_ms=int(timestamp_ms))
            future_bid = _finite(label.get(f"{prefix}_future_bid"))
            key = (int(outcome_id), side_index)
            previous = last_by_market_side.get(key)
            if vector is None or future_bid is None or not 0 < future_bid < 1:
                continue
            # One row per label horizon prevents a five-second stream from
            # pretending to contain independent five-minute outcomes.
            if previous is not None and int(timestamp_ms) - previous < horizon_sec * 1000:
                continue
            output.append(_Row(int(outcome_id), int(timestamp_ms), side_index, vector, future_bid))
            last_by_market_side[key] = int(timestamp_ms)
    return output


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda index: abs(augmented[index][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(n):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [value - factor * pivot_value for value, pivot_value in zip(augmented[row], augmented[column])]
    return [augmented[index][-1] for index in range(n)]


def _fit(rows: list[_Row]) -> dict[str, Any] | None:
    if not rows:
        return None
    width = len(FEATURE_NAMES)
    means = [sum(row.vector[i] for row in rows) / len(rows) for i in range(width)]
    scales = [math.sqrt(sum((row.vector[i] - means[i]) ** 2 for row in rows) / len(rows)) for i in range(width)]
    scales = [value if value > 1e-12 else 1.0 for value in scales]
    design = [[1.0] + [(value - means[i]) / scales[i] for i, value in enumerate(row.vector)] for row in rows]
    target = [row.future_bid for row in rows]
    dimensions = width + 1
    gram = [[sum(row[a] * row[b] for row in design) for b in range(dimensions)] for a in range(dimensions)]
    for index in range(1, dimensions):
        gram[index][index] += 1e-3
    rhs = [sum(row[i] * value for row, value in zip(design, target)) for i in range(dimensions)]
    coefficients = _solve(gram, rhs)
    if coefficients is None:
        return None
    predictions = [coefficients[0] + sum(coefficients[i + 1] * design_row[i + 1] for i in range(width)) for design_row in design]
    rmse = math.sqrt(sum((prediction - value) ** 2 for prediction, value in zip(predictions, target)) / len(target))
    return {"means": means, "scales": scales, "coefficients": coefficients, "training_rmse": rmse}


def _predict(model: Mapping[str, Any], vector: tuple[float, ...]) -> float:
    means, scales, coefficients = model["means"], model["scales"], model["coefficients"]
    value = float(coefficients[0]) + sum(
        float(coefficients[index + 1]) * ((feature - float(means[index])) / float(scales[index]))
        for index, feature in enumerate(vector)
    )
    return min(0.99999, max(0.00001, value))


def train_report(db_path: str | Path, *, horizon_sec: int = 300, artifact_path: str | Path | None = None) -> dict[str, Any]:
    rows = _rows(db_path, horizon_sec)
    markets = sorted({row.outcome_id for row in rows}, key=lambda value: min(row.timestamp_ms for row in rows if row.outcome_id == value))
    predictions: list[float] = []
    actuals: list[float] = []
    folds: list[dict[str, Any]] = []
    for position in range(2, len(markets)):
        train = [row for row in rows if row.outcome_id in markets[:position]]
        test = [row for row in rows if row.outcome_id == markets[position]]
        model = _fit(train)
        if model is None or len(train) < 50 or len(test) < 4:
            continue
        fold_predictions = [_predict(model, row.vector) for row in test]
        rmse = math.sqrt(sum((pred - row.future_bid) ** 2 for pred, row in zip(fold_predictions, test)) / len(test))
        persistence_rmse = math.sqrt(sum((row.vector[0] - row.future_bid) ** 2 for row in test) / len(test))
        predictions.extend(fold_predictions)
        actuals.extend(row.future_bid for row in test)
        folds.append({"test_outcome_id": markets[position], "train_rows": len(train), "test_rows": len(test),
                      "model_rmse": rmse, "mid_persistence_rmse": persistence_rmse})
    fitted = _fit(rows)
    blockers: list[str] = []
    if len(markets) < 5:
        blockers.append("insufficient_independent_daily_markets")
    if len(rows) < 100:
        blockers.append("insufficient_non_overlapping_rows")
    if not folds:
        blockers.append("purged_market_walk_forward_unavailable")
    oos_rmse = math.sqrt(sum((pred - value) ** 2 for pred, value in zip(predictions, actuals)) / len(actuals)) if actuals else None
    artifact = None
    if fitted is not None:
        artifact = {
            "model_schema_version": MODEL_SCHEMA_VERSION, "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "period": "1d", "horizon_sec": horizon_sec, "feature_names": list(FEATURE_NAMES),
            "trained_at": datetime.now(timezone.utc).isoformat(), "training_rows": len(rows),
            "market_instances": len(markets), "trained_through_ms": max((row.timestamp_ms for row in rows), default=None),
            **fitted, "live_authority": False,
        }
        if artifact_path is not None:
            Path(artifact_path).write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "report": "outcome_executable_fair_value", "schema_version": 1, "horizon_sec": horizon_sec,
        "rows": len(rows), "market_instances": len(markets), "folds": folds, "oos_rows": len(actuals),
        "oos_rmse": oos_rmse, "artifact_written": str(artifact_path) if artifact is not None and artifact_path else None,
        "shadow_model_available": artifact is not None, "ready_for_live": False,
        "blockers": blockers + ["milestone_a_shadow_only"],
    }


class OutcomeFairValueShadow:
    """Loads a frozen offline artifact and emits bounded, non-authoritative scores."""

    def __init__(self, artifact_path: str | Path = "logs/outcome_fair_value_model.json") -> None:
        self.path = Path(artifact_path)
        self.artifact: dict[str, Any] | None = None
        try:
            candidate = json.loads(self.path.read_text(encoding="utf-8"))
            if candidate.get("model_schema_version") == MODEL_SCHEMA_VERSION and candidate.get("feature_names") == list(FEATURE_NAMES):
                self.artifact = candidate
        except (OSError, TypeError, json.JSONDecodeError):
            pass

    def score(self, *, side_index: int, features: Mapping[str, Any], entry_bid: float) -> dict[str, Any]:
        if self.artifact is None:
            return {"available": False, "reason": "frozen_fair_value_artifact_unavailable", "live_authority": False}
        vector = _vector(features, side_index)
        if vector is None:
            return {"available": False, "reason": "fair_value_features_incomplete", "live_authority": False}
        prediction = _predict(self.artifact, vector)
        rmse = float(self.artifact.get("training_rmse") or 0.0)
        edge = prediction - float(entry_bid)
        lower_edge = edge - 1.645 * rmse
        confidence = "high" if lower_edge > 0 else "medium" if edge > 0 else "none"
        return {
            "available": True, "reason": "shadow_score_observed", "side_index": side_index,
            "predicted_future_executable_bid": prediction, "entry_bid": float(entry_bid),
            "predicted_gross_edge_per_share": edge, "one_sided_95pct_lower_edge": lower_edge,
            "confidence": confidence, "horizon_sec": self.artifact["horizon_sec"],
            "artifact_trained_through_ms": self.artifact.get("trained_through_ms"), "live_authority": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train/report the shadow executable fair-value model")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--horizon-sec", type=int, default=300)
    parser.add_argument("--artifact", default=None)
    args = parser.parse_args()
    print(json.dumps(train_report(args.db, horizon_sec=args.horizon_sec, artifact_path=args.artifact), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
