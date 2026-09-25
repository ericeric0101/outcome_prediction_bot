"""B2: multi-target active-decision models with market-level walk-forward.

The models are deliberately small and inspectable.  They predict executable
future bids and discrete path outcomes; they do not submit orders and every
artifact is permanently marked ``live_authority=false``.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from bot.outcome_active_dataset import ACTIVE_DATASET_SCHEMA_VERSION, ACTIVE_FEATURE_NAMES, ActiveDecisionRow, load_decision_rows
from bot.outcome_oi_features import FEATURE_SCHEMA_VERSION
from bot.outcome_settlement_probability import estimate_settlement_probability


# Calendar columns alter the feature-vector contract.  Reject older frozen
# artifacts rather than silently scoring them against a shifted vector.
ACTIVE_MODEL_SCHEMA_VERSION = 2
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
SETTLEMENT_CHECKPOINTS_SEC = (3600, 10_800, 21_600, 43_200)


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _logit(value: float) -> float:
    clipped = min(1.0 - 1e-6, max(1e-6, value))
    return math.log(clipped / (1.0 - clipped))


def _fit_logistic(rows: list[dict[str, Any]]) -> tuple[list[float], list[float], list[float]] | None:
    """Small L2 logistic model; all scaling is estimated from earlier markets only."""
    if len(rows) < 8 or len({int(row["label"]) for row in rows}) < 2:
        return None
    width = len(rows[0]["model_features"])
    means = [sum(row["model_features"][i] for row in rows) / len(rows) for i in range(width)]
    scales = [math.sqrt(sum((row["model_features"][i] - means[i]) ** 2 for row in rows) / len(rows)) for i in range(width)]
    scales = [scale if scale > 1e-9 else 1.0 for scale in scales]
    xs = [[1.0] + [(float(v) - means[i]) / scales[i] for i, v in enumerate(row["model_features"])] for row in rows]
    ys = [float(row["label"]) for row in rows]
    weights = [0.0] * (width + 1)
    for _ in range(1200):
        grad = [0.0] * len(weights)
        for x, y in zip(xs, ys):
            linear = max(-30.0, min(30.0, sum(w * v for w, v in zip(weights, x))))
            probability = 1.0 / (1.0 + math.exp(-linear))
            for i, v in enumerate(x):
                grad[i] += (probability - y) * v / len(rows)
        for i in range(1, len(grad)):
            grad[i] += 0.1 * weights[i] / len(rows)
        step = 0.08
        updated = [w - step * g for w, g in zip(weights, grad)]
        if max(abs(a - b) for a, b in zip(updated, weights)) < 1e-8:
            weights = updated
            break
        weights = updated
    return means, scales, weights


def _predict_logistic(model: tuple[list[float], list[float], list[float]], values: list[float]) -> float:
    means, scales, weights = model
    linear = weights[0] + sum(weights[i + 1] * ((value - means[i]) / scales[i]) for i, value in enumerate(values))
    linear = max(-30.0, min(30.0, linear))
    return 1.0 / (1.0 + math.exp(-linear))


def _binary_metrics(predictions: list[float], labels: list[int]) -> dict[str, Any]:
    if not labels:
        return {"rows": 0, "brier": None, "log_loss": None}
    eps = 1e-12
    return {
        "rows": len(labels),
        "brier": sum((p - y) ** 2 for p, y in zip(predictions, labels)) / len(labels),
        "log_loss": -sum(y * math.log(max(eps, p)) + (1 - y) * math.log(max(eps, 1 - p)) for p, y in zip(predictions, labels)) / len(labels),
        "positive_rate": sum(labels) / len(labels),
        "predicted_rate": sum(predictions) / len(predictions),
    }


def settlement_probability_report(db_path: str | Path) -> dict[str, Any]:
    """Official-resolution probability comparison at fixed time-left checkpoints.

    Every method is scored on the exact same out-of-sample market/checkpoint
    rows. No settlement information enters features; only earlier settled
    markets can train the regularized external-feature challenger.
    """
    path = Path(db_path)
    empty = {"report": "outcome_settlement_probability_walk_forward", "checkpoints": {}, "live_authority": False}
    if not path.exists():
        return {**empty, "blockers": ["database_missing"]}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"strategy_events", "outcome_oi_feature_rows", "outcome_market_settlement_registry", "binance_oi_observations"}
        if not required.issubset(tables):
            return {**empty, "blockers": ["required_existing_tables_missing"]}
        settled = conn.execute(
            """SELECT outcome_id,winning_side_index,recorded_at FROM outcome_market_settlement_registry
               WHERE winning_side_index IN (0,1)
                 AND settlement_source LIKE 'official_sdk_settled_outcome%'"""
        ).fetchall()
        if not settled:
            return {**empty, "blockers": ["no_official_settlement_labels"]}
        outcome_ids = [int(row[0]) for row in settled]
        placeholders = ",".join("?" for _ in outcome_ids)
        source = conn.execute(
            f"""SELECT outcome_id,snapshot_timestamp_ms,features_json FROM outcome_oi_feature_rows
                WHERE feature_schema_version=? AND period='1d' AND oi_backfilled=0 AND outcome_id IN ({placeholders})
                ORDER BY outcome_id,snapshot_timestamp_ms""",
            (FEATURE_SCHEMA_VERSION, *outcome_ids),
        ).fetchall()
        marks = conn.execute(
            """SELECT exchange_timestamp_ms,local_received_at_ms,mark_price FROM binance_oi_observations
               WHERE symbol='BTCUSDT' AND backfilled=0 AND mark_price IS NOT NULL
               ORDER BY exchange_timestamp_ms"""
        ).fetchall()
        online_rows = conn.execute(
            """SELECT payload_json FROM strategy_events
               WHERE event_type='OUTCOME_SETTLEMENT_PROBABILITY_SHADOW' ORDER BY id"""
        ).fetchall()
    labels_by_market = {int(oid): (int(side), str(recorded)) for oid, side, recorded in settled}
    rows_by_market: dict[int, list[dict[str, Any]]] = {}
    for oid, ts, raw_features in source:
        try:
            features = json.loads(raw_features)
            if not isinstance(features, dict):
                continue
            rows_by_market.setdefault(int(oid), []).append({"timestamp_ms": int(ts), "features": features})
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    mark_points: list[tuple[int, int, float]] = []
    for exchange_ts, received_ts, raw_price in marks:
        try:
            price = float(raw_price)
            if price > 0:
                mark_points.append((int(exchange_ts), int(received_ts), price))
        except (TypeError, ValueError):
            continue
    mark_times = [point[0] for point in mark_points]
    online_by_market: dict[int, list[dict[str, Any]]] = {}
    for (raw,) in online_rows:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("status") != "available":
                continue
            market_id = int(payload["outcome_id"])
            if market_id not in labels_by_market:
                continue
            online_by_market.setdefault(market_id, []).append(payload)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
    report_checkpoints: dict[str, Any] = {}
    for horizon in SETTLEMENT_CHECKPOINTS_SEC:
        candidates: list[dict[str, Any]] = []
        missing = {"checkpoint_row": 0, "structural_inputs": 0, "external_inputs": 0, "volatility_history": 0}
        for outcome_id, points in rows_by_market.items():
            winner = labels_by_market.get(outcome_id)
            if winner is None:
                continue
            eligible = []
            for point in points:
                feature = point["features"]
                time_left = _finite(feature.get("time_left_sec"))
                bid, ask = _finite(feature.get("yes_bid")), _finite(feature.get("yes_ask"))
                if time_left is None or abs(time_left - horizon) > 180 or bid is None or ask is None or not 0 < bid < ask < 1:
                    continue
                eligible.append((abs(time_left - horizon), point))
            if not eligible:
                missing["checkpoint_row"] += 1
                continue
            point = min(eligible, key=lambda item: (item[0], item[1]["timestamp_ms"]))[1]
            feature, timestamp = point["features"], point["timestamp_ms"]
            spot, strike = _finite(feature.get("binance_mark_price")), _finite(feature.get("strike"))
            time_left = _finite(feature.get("time_left_sec"))
            if spot is None or strike is None or time_left is None or spot <= 0 or strike <= 0 or time_left <= 0:
                missing["structural_inputs"] += 1
                continue
            start = bisect.bisect_left(mark_times, timestamp - 3_600_000)
            stop = bisect.bisect_right(mark_times, timestamp)
            history = [item for item in mark_points[start:stop] if item[1] <= timestamp]
            estimate = estimate_settlement_probability(
                spot_price=spot, strike=strike, time_left_sec=time_left, as_of_ms=timestamp,
                price_points=((item[0], item[2]) for item in history),
            )
            if estimate.get("status") != "available":
                missing["volatility_history"] += 1
                continue
            simple_probability = float(estimate["probability_up"])
            z = float(estimate["z_score"])
            market_probability = (float(feature["yes_bid"]) + float(feature["yes_ask"])) / 2.0
            try:
                external = [
                    _logit(market_probability), z,
                    float(feature["btc_mark_return_300s_bps"]) / 100.0,
                    float(feature["btc_mark_return_900s_bps"]) / 100.0,
                    float(feature["oi_return_300s_bps"]),
                    float(feature["taker_imbalance"]),
                ]
                if not all(math.isfinite(value) for value in external):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                missing["external_inputs"] += 1
                continue
            try:
                settlement_ms = int(datetime.fromisoformat(winner[1].replace("Z", "+00:00")).timestamp() * 1000)
            except (ValueError, OverflowError):
                continue
            candidates.append({
                "outcome_id": outcome_id, "decision_timestamp_ms": timestamp,
                "settlement_timestamp_ms": settlement_ms, "time_left_sec": time_left,
                "label": int(winner[0] == 0), "market_probability": market_probability,
                "simple_probability": simple_probability, "model_features": external,
            })
        scored: list[dict[str, Any]] = []
        folds: list[dict[str, Any]] = []
        ordered = sorted(candidates, key=lambda row: row["decision_timestamp_ms"])
        for test in ordered:
            train = [row for row in ordered if row["outcome_id"] != test["outcome_id"] and row["settlement_timestamp_ms"] < test["decision_timestamp_ms"]]
            model = _fit_logistic(train)
            if model is None:
                continue
            scored.append({**test, "external_probability": _predict_logistic(model, test["model_features"]), "train_markets": len({row["outcome_id"] for row in train})})
            folds.append({"test_outcome_id": test["outcome_id"], "train_markets": len({row["outcome_id"] for row in train}), "train_rows": len(train)})
        labels = [int(row["label"]) for row in scored]
        online_candidates: list[dict[str, Any]] = []
        online_missing = 0
        for outcome_id, (winning_side, recorded_at) in labels_by_market.items():
            eligible_online = []
            for payload in online_by_market.get(outcome_id, []):
                time_left = _finite(payload.get("time_left_sec"))
                p_up = _finite(payload.get("probability_up"))
                market_mid = _finite(payload.get("market_up_probability_mid"))
                timestamp = _finite(payload.get("decision_timestamp_ms"))
                if (payload.get("capture_quality_status") != "accepted"
                        or time_left is None or abs(time_left - horizon) > 180 or p_up is None
                        or market_mid is None or timestamp is None or not 0 < market_mid < 1
                        or not 0 < p_up < 1):
                    continue
                eligible_online.append((abs(time_left - horizon), int(timestamp), payload))
            if not eligible_online:
                online_missing += 1
                continue
            _, timestamp, payload = min(eligible_online, key=lambda item: (item[0], item[1]))
            try:
                settled_ms = int(datetime.fromisoformat(recorded_at.replace("Z", "+00:00")).timestamp() * 1000)
            except (ValueError, OverflowError):
                online_missing += 1
                continue
            if timestamp >= settled_ms:
                online_missing += 1
                continue
            online_candidates.append({
                "outcome_id": outcome_id, "decision_timestamp_ms": timestamp,
                "label": int(winning_side == 0),
                "market_probability": float(payload["market_up_probability_mid"]),
                "online_spot_probability": float(payload["probability_up"]),
                "training_markets": 0,
            })
        online_labels = [int(row["label"]) for row in online_candidates]
        report_checkpoints[str(horizon)] = {
            "target": "official_outcome_settlement_winning_side_yes",
            "candidate_markets_with_all_features": len(candidates),
            "expanding_walk_forward_oos_markets": len(scored),
            "checkpoint_match_tolerance_sec": 180,
            "minimum_volatility_history": {"valid_returns": 20, "covered_seconds": 900, "max_gap_sec": 60},
            "common_oos_metrics": {
                "market_midpoint": _binary_metrics([row["market_probability"] for row in scored], labels),
                "simple_spot_strike_realized_vol": _binary_metrics([row["simple_probability"] for row in scored], labels),
                "external_increment_logistic": _binary_metrics([row["external_probability"] for row in scored], labels),
            },
            "online_spot_shadow_common_checkpoint_metrics": {
                "markets": len(online_candidates),
                "market_midpoint": _binary_metrics([row["market_probability"] for row in online_candidates], online_labels),
                "hyperliquid_spot_strike_realized_vol": _binary_metrics([row["online_spot_probability"] for row in online_candidates], online_labels),
                "unavailable_or_unmatched_settled_markets": online_missing,
                "checkpoint_match_tolerance_sec": 180,
                "note": "deterministic live shadow forecast vs market at identical eligible checkpoints; descriptive until enough independent official settlements",
            },
            "oos_predictions": [
                {
                    "outcome_id": row["outcome_id"],
                    "decision_timestamp_ms": row["decision_timestamp_ms"],
                    "official_yes_label": row["label"],
                    "market_midpoint": row["market_probability"],
                    "simple_spot_strike_realized_vol": row["simple_probability"],
                    "external_increment_logistic": row["external_probability"],
                    "prior_settled_training_markets": row["train_markets"],
                }
                for row in scored
            ],
            "folds": folds,
            "unavailable_counts": missing,
            "live_authority": False,
        }
    return {
        "report": "outcome_settlement_probability_walk_forward",
        "method": "fixed_time_left_checkpoint; market midpoint vs zero-drift lognormal spot/strike with trailing realized variance vs expanding logistic increment",
        "official_label_source": "outcome_market_settlement_registry.winning_side_index",
        "checkpoints": report_checkpoints,
        "limitations": [
            "All three forecasts use identical market/checkpoint rows that have a trainable prior-market external model.",
            "Settlement labels are official outcome labels; settlement time is used only to gate prior-label availability.",
            "Structural model assumes zero drift, lognormal returns, and trailing realized variance; jumps, skew, funding carry, and boundary effects are omitted.",
            "External model is a small regularized exploratory logistic fit; few independent settled daily markets make scores high-variance.",
            "The official spot settlement definition and exact market expiry mapping must be independently confirmed before interpreting as edge.",
            "This is read-only research; no runtime, admission, exit, sizing, or order authority is granted.",
            "The live Hyperliquid spot shadow comparison uses only records with available as-of spot-volatility history and official labels; it is not available until those forecasts settle.",
        ],
        "blockers": ["insufficient_independent_settled_markets_for_reliable_model_selection", "shadow_only"],
        "live_authority": False,
    }


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
        "settlement_probability": settlement_probability_report(db_path),
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
