"""B2: multi-target active-decision models with market-level walk-forward.

The models are deliberately small and inspectable.  They predict executable
future bids and discrete path outcomes; they do not submit orders and every
artifact is permanently marked ``live_authority=false``.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from bot.outcome_active_dataset import ACTIVE_DATASET_SCHEMA_VERSION, ACTIVE_FEATURE_NAMES, ActiveDecisionRow, load_decision_rows


ACTIVE_MODEL_SCHEMA_VERSION = 1
FAIR_TARGETS = ("future_bid_300s", "future_bid_900s", "future_bid_1800s", "future_bid_3600s")
PROBABILITY_TARGETS = (
    "hit_plus_1pct_1h",
    "hit_plus_2pct_1h",
    "hit_plus_5pct_1h",
    "breach_minus_5pct_1h",
    "breach_minus_10pct_1h",
    "breach_minus_20pct_1h",
    "recovered_after_minus_10pct_1h",
)


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction)))
    return ordered[index]


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
            augmented[row] = [value - factor * value_at_pivot for value, value_at_pivot in zip(augmented[row], augmented[column])]
    return [augmented[index][-1] for index in range(n)]


def _target_value(row: ActiveDecisionRow, target: str) -> float | None:
    raw = row.targets.get(target)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return float(raw)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _fit(rows: Iterable[ActiveDecisionRow], target: str) -> dict[str, Any] | None:
    eligible = [(row, value) for row in rows if (value := _target_value(row, target)) is not None]
    if len(eligible) < max(10, len(ACTIVE_FEATURE_NAMES) + 2):
        return None
    width = len(ACTIVE_FEATURE_NAMES)
    means = [sum(row.vector[index] for row, _ in eligible) / len(eligible) for index in range(width)]
    scales = [math.sqrt(sum((row.vector[index] - means[index]) ** 2 for row, _ in eligible) / len(eligible)) for index in range(width)]
    scales = [value if value > 1e-12 else 1.0 for value in scales]
    design = [[1.0] + [(value - means[index]) / scales[index] for index, value in enumerate(row.vector)] for row, _ in eligible]
    values = [value for _, value in eligible]
    dimensions = width + 1
    gram = [[sum(row[left] * row[right] for row in design) for right in range(dimensions)] for left in range(dimensions)]
    for index in range(1, dimensions):
        gram[index][index] += 1e-3
    rhs = [sum(row[index] * value for row, value in zip(design, values)) for index in range(dimensions)]
    coefficients = _solve(gram, rhs)
    if coefficients is None:
        return None
    predictions = [coefficients[0] + sum(coefficients[index + 1] * design_row[index + 1] for index in range(width)) for design_row in design]
    residuals = [actual - predicted for actual, predicted in zip(values, predictions)]
    return {
        "target": target,
        "kind": "probability" if target in PROBABILITY_TARGETS else "continuous",
        "sample_count": len(eligible),
        "means": means,
        "scales": scales,
        "coefficients": coefficients,
        "residual_quantiles": {
            "q10": _quantile(residuals, 0.10),
            "q50": _quantile(residuals, 0.50),
            "q90": _quantile(residuals, 0.90),
        },
    }


def _predict(model: Mapping[str, Any], vector: tuple[float, ...]) -> float:
    means, scales, coefficients = model["means"], model["scales"], model["coefficients"]
    prediction = float(coefficients[0]) + sum(
        float(coefficients[index + 1]) * ((value - float(means[index])) / float(scales[index]))
        for index, value in enumerate(vector)
    )
    if model.get("kind") == "probability":
        return min(1.0, max(0.0, prediction))
    return min(0.99999, max(0.00001, prediction))


def _maker_fill_summary(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {"submits": 0, "fills": 0, "smoothed_probability": 0.5, "by_spread_bucket": {}}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        submits = conn.execute(
            """SELECT venue_order_id,
                      CAST(json_extract(payload_json,'$.audit.entry_spread_bps') AS REAL)
               FROM order_events
               WHERE event_type='ORDER_SUBMIT' AND side='BUY'
                 AND venue_order_id IS NOT NULL"""
        ).fetchall()
        fill_ids = {str(row[0]) for row in conn.execute(
            "SELECT DISTINCT venue_order_id FROM order_events WHERE event_type='ORDER_FILLED' AND side='BUY' AND venue_order_id IS NOT NULL"
        )}
    buckets: dict[str, list[int]] = {}
    for order_id, raw_spread in submits:
        try:
            spread = float(raw_spread)
        except (TypeError, ValueError):
            bucket = "unknown"
        else:
            bucket = "0_50" if spread <= 50 else "50_100" if spread <= 100 else "100_125" if spread <= 125 else "over_125"
        values = buckets.setdefault(bucket, [0, 0])
        values[0] += 1
        values[1] += int(str(order_id) in fill_ids)
    fills = sum(str(order_id) in fill_ids for order_id, _ in submits)
    return {
        "submits": len(submits),
        "fills": fills,
        "smoothed_probability": (fills + 1) / (len(submits) + 2),
        "by_spread_bucket": {
            key: {"submits": values[0], "fills": values[1], "smoothed_probability": (values[1] + 1) / (values[0] + 2)}
            for key, values in sorted(buckets.items())
        },
    }


def fit_artifact(rows: list[ActiveDecisionRow], *, maker_fill_model: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    models = {target: model for target in (*FAIR_TARGETS, *PROBABILITY_TARGETS) if (model := _fit(rows, target)) is not None}
    if not all(target in models for target in FAIR_TARGETS):
        return None
    return {
        "model_schema_version": ACTIVE_MODEL_SCHEMA_VERSION,
        "dataset_schema_version": ACTIVE_DATASET_SCHEMA_VERSION,
        "feature_names": list(ACTIVE_FEATURE_NAMES),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_rows": len(rows),
        "market_instances": len({row.outcome_id for row in rows}),
        "trained_through_ms": max((row.timestamp_ms for row in rows), default=None),
        "models": models,
        "maker_fill_model": dict(maker_fill_model or {"smoothed_probability": 0.5, "by_spread_bucket": {}}),
        "live_authority": False,
    }


def _metric(target: str, predicted: list[float], actual: list[float]) -> dict[str, Any]:
    if not actual:
        return {"rows": 0}
    mse = sum((left - right) ** 2 for left, right in zip(predicted, actual)) / len(actual)
    result: dict[str, Any] = {"rows": len(actual)}
    if target in PROBABILITY_TARGETS:
        result["brier"] = mse
        result["positive_rate"] = sum(actual) / len(actual)
        result["predicted_rate"] = sum(predicted) / len(predicted)
    else:
        result["rmse"] = math.sqrt(mse)
        result["mae"] = sum(abs(left - right) for left, right in zip(predicted, actual)) / len(actual)
    return result


def train_report(
    db_path: str | Path,
    *,
    period: str = "1d",
    sample_interval_sec: int = 60,
    artifact_path: str | Path | None = None,
) -> dict[str, Any]:
    rows = load_decision_rows(db_path, period=period, sample_interval_sec=sample_interval_sec)
    by_market: dict[int, list[ActiveDecisionRow]] = {}
    for row in rows:
        by_market.setdefault(row.outcome_id, []).append(row)
    markets = sorted(by_market, key=lambda market: min(row.timestamp_ms for row in by_market[market]))
    collected: dict[str, tuple[list[float], list[float]]] = {
        target: ([], []) for target in (*FAIR_TARGETS, *PROBABILITY_TARGETS)
    }
    # The future-bid model is only useful if it beats the obvious persistence
    # forecast (future executable bid == current executable bid).  Keep the
    # baseline on the exact same out-of-sample rows/folds.
    persistence: dict[str, list[float]] = {target: [] for target in FAIR_TARGETS}
    folds: list[dict[str, Any]] = []
    for position in range(2, len(markets)):
        train = [row for market in markets[:position] for row in by_market[market]]
        test = by_market[markets[position]]
        fold_targets: list[str] = []
        for target in (*FAIR_TARGETS, *PROBABILITY_TARGETS):
            model = _fit(train, target)
            if model is None:
                continue
            predicted, actual = collected[target]
            for row in test:
                value = _target_value(row, target)
                if value is not None:
                    predicted.append(_predict(model, row.vector))
                    actual.append(value)
                    if target in persistence:
                        persistence[target].append(row.bid)
            fold_targets.append(target)
        folds.append({"test_outcome_id": markets[position], "train_markets": position, "train_rows": len(train), "test_rows": len(test), "targets": fold_targets})
    fill_model = _maker_fill_summary(db_path)
    artifact = fit_artifact(rows, maker_fill_model=fill_model)
    if artifact is not None and artifact_path is not None:
        Path(artifact_path).write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    blockers: list[str] = []
    if len(markets) < 5:
        blockers.append("insufficient_independent_daily_markets")
    if not folds:
        blockers.append("market_walk_forward_unavailable")
    if artifact is None:
        blockers.append("active_model_artifact_unavailable")
    metrics = {target: _metric(target, *values) for target, values in collected.items()}
    for target, baseline in persistence.items():
        actual = collected[target][1]
        if actual and len(actual) == len(baseline):
            baseline_metric = _metric(target, baseline, actual)
            metrics[target]["persistence_rmse"] = baseline_metric.get("rmse")
            model_rmse = metrics[target].get("rmse")
            baseline_rmse = baseline_metric.get("rmse")
            metrics[target]["rmse_improvement_vs_persistence"] = (
                (float(baseline_rmse) - float(model_rmse)) / float(baseline_rmse)
                if model_rmse is not None and baseline_rmse not in (None, 0)
                else None
            )
    if any(
        metrics.get(target, {}).get("rmse_improvement_vs_persistence") is not None
        and float(metrics[target]["rmse_improvement_vs_persistence"]) <= 0
        for target in FAIR_TARGETS
    ):
        blockers.append("future_bid_model_does_not_beat_persistence_baseline")
    return {
        "report": "outcome_active_multi_target_model",
        "schema_version": ACTIVE_MODEL_SCHEMA_VERSION,
        "period": period,
        "rows": len(rows),
        "market_instances": len(markets),
        "folds": folds,
        "oos_metrics": metrics,
        "maker_fill_model": fill_model,
        "artifact_written": str(artifact_path) if artifact is not None and artifact_path is not None else None,
        "shadow_model_available": artifact is not None,
        "ready_for_live": False,
        "blockers": blockers + ["milestone_b_shadow_only", "b6_requires_separate_operator_authorization"],
    }


class OutcomeActiveModel:
    def __init__(self, artifact_path: str | Path = "logs/outcome_active_model.json", *, artifact: Mapping[str, Any] | None = None) -> None:
        candidate: Mapping[str, Any] | None = artifact
        if candidate is None:
            try:
                candidate = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
            except (OSError, TypeError, json.JSONDecodeError):
                candidate = None
        self.artifact = dict(candidate) if (
            isinstance(candidate, Mapping)
            and candidate.get("model_schema_version") == ACTIVE_MODEL_SCHEMA_VERSION
            and candidate.get("feature_names") == list(ACTIVE_FEATURE_NAMES)
            and candidate.get("live_authority") is False
        ) else None

    def score(self, vector: tuple[float, ...], *, spread_bps: float | None = None) -> dict[str, Any]:
        if self.artifact is None:
            return {"available": False, "reason": "frozen_active_artifact_unavailable", "live_authority": False}
        outputs: dict[str, Any] = {}
        for target, model in self.artifact.get("models", {}).items():
            prediction = _predict(model, vector)
            residual = model.get("residual_quantiles", {})
            if model.get("kind") == "continuous":
                outputs[target] = {
                    "mean": prediction,
                    "q10": min(0.99999, max(0.00001, prediction + float(residual.get("q10") or 0))),
                    "q50": min(0.99999, max(0.00001, prediction + float(residual.get("q50") or 0))),
                    "q90": min(0.99999, max(0.00001, prediction + float(residual.get("q90") or 0))),
                }
            else:
                outputs[target] = prediction
        fill = self.artifact.get("maker_fill_model", {})
        bucket = "unknown" if spread_bps is None else "0_50" if spread_bps <= 50 else "50_100" if spread_bps <= 100 else "100_125" if spread_bps <= 125 else "over_125"
        bucket_model = fill.get("by_spread_bucket", {}).get(bucket, {}) if isinstance(fill, Mapping) else {}
        maker_fill_probability = bucket_model.get("smoothed_probability", fill.get("smoothed_probability", 0.5) if isinstance(fill, Mapping) else 0.5)
        return {
            "available": True,
            "reason": "active_multi_target_score_observed",
            "outputs": outputs,
            "maker_fill_probability": float(maker_fill_probability),
            "spread_bucket": bucket,
            "artifact_trained_through_ms": self.artifact.get("trained_through_ms"),
            "live_authority": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train/report B2 active-decision models")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--sample-interval-sec", type=int, default=60)
    parser.add_argument("--artifact", default="logs/outcome_active_model.json")
    args = parser.parse_args()
    print(json.dumps(train_report(args.db, period=args.period, sample_interval_sec=args.sample_interval_sec, artifact_path=args.artifact), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
