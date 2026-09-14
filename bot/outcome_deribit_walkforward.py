"""D3 read-only baseline-vs-Deribit daily-market walk-forward comparison."""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from bot.outcome_deribit_features import DERIBIT_FEATURE_SCHEMA_VERSION


LABEL_HORIZON_SEC = 300
PURGE_SEC = LABEL_HORIZON_SEC
MIN_MARKET_INSTANCES = 5
MIN_TRAIN_ROWS = 100
MIN_TEST_ROWS = 20
MIN_OOS_ROWS = 200

BASELINE_FEATURES = (
    "side_mid", "side_spread", "side_depth_imbalance", "side_probability_distance", "time_left_fraction",
)
DERIBIT_FEATURES = (
    "deribit_mid_return_5s_bps", "deribit_index_return_5s_bps", "deribit_spread_bps",
    "deribit_top_imbalance", "deribit_funding_8h", "deribit_trade_flow_imbalance_1s",
    "deribit_trade_flow_present_1s",
)


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


@dataclass(frozen=True)
class _Row:
    market: int
    timestamp_ms: int
    baseline: tuple[float, ...]
    extended: tuple[float, ...]
    target: float


@dataclass(frozen=True)
class DeribitWalkForwardFold:
    test_market_instance: int
    train_market_instances: int
    train_rows: int
    purged_train_rows: int
    test_rows: int
    baseline_rmse: float
    deribit_rmse: float
    baseline_mae: float
    deribit_mae: float


@dataclass(frozen=True)
class DeribitWalkForwardReport:
    feature_schema_version: int
    label_horizon_sec: int
    purge_sec: int
    eligible_rows: int
    market_instances: int
    folds: tuple[DeribitWalkForwardFold, ...]
    oos_rows: int
    baseline_rmse: float | None
    deribit_rmse: float | None
    rmse_improvement: float | None
    baseline_mae: float | None
    deribit_mae: float | None
    mae_improvement: float | None
    incremental_evidence: bool
    ready_for_live: bool
    blockers: tuple[str, ...]


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
            augmented[row] = [value - factor * other for value, other in zip(augmented[row], augmented[column])]
    return [augmented[index][-1] for index in range(n)]


def _predict(train: list[_Row], test: list[_Row], *, extended: bool) -> list[float]:
    values = [row.extended if extended else row.baseline for row in train]
    test_values = [row.extended if extended else row.baseline for row in test]
    width = len(values[0])
    means = [sum(row[index] for row in values) / len(values) for index in range(width)]
    scales = [math.sqrt(sum((row[index] - means[index]) ** 2 for row in values) / len(values)) for index in range(width)]
    scales = [scale if scale > 1e-12 else 1.0 for scale in scales]
    design = [[1.0] + [(value - means[index]) / scales[index] for index, value in enumerate(row)] for row in values]
    target = [row.target for row in train]
    dimensions = width + 1
    gram = [[sum(row[left] * row[right] for row in design) for right in range(dimensions)] for left in range(dimensions)]
    for index in range(1, dimensions):
        gram[index][index] += 1e-3
    rhs = [sum(row[index] * value for row, value in zip(design, target)) for index in range(dimensions)]
    coefficients = _solve(gram, rhs)
    if coefficients is None:
        return [sum(target) / len(target)] * len(test)
    return [
        coefficients[0] + sum(coefficients[index + 1] * ((value - means[index]) / scales[index]) for index, value in enumerate(row))
        for row in test_values
    ]


def _errors(predictions: Iterable[float], rows: Iterable[_Row]) -> tuple[float, float]:
    pairs = [(prediction, row.target) for prediction, row in zip(predictions, rows)]
    return (
        math.sqrt(sum((prediction - target) ** 2 for prediction, target in pairs) / len(pairs)),
        sum(abs(prediction - target) for prediction, target in pairs) / len(pairs),
    )


def _row(features: dict[str, Any], labels: dict[str, Any], market: int, timestamp_ms: int) -> _Row | None:
    label = labels.get(f"future_{LABEL_HORIZON_SEC}s", {})
    target = _finite(label.get("yes_long_markout_ps")) if label.get("available") is True else None
    bid, ask = _finite(features.get("yes_bid")), _finite(features.get("yes_ask"))
    bid_size, ask_size = _finite(features.get("yes_bid_size")), _finite(features.get("yes_ask_size"))
    time_left = _finite(features.get("time_left_sec"))
    if None in (target, bid, ask, bid_size, ask_size, time_left) or ask <= bid or time_left < 0:
        return None
    depth = bid_size + ask_size
    if depth <= 0:
        return None
    derived = dict(features)
    derived.update({
        "side_mid": (bid + ask) / 2, "side_spread": ask - bid,
        "side_depth_imbalance": (bid_size - ask_size) / depth,
        "side_probability_distance": (bid + ask) / 2 - 0.5,
        "time_left_fraction": min(time_left, 86_400.0) / 86_400.0,
    })
    baseline = tuple(_finite(derived.get(name)) for name in BASELINE_FEATURES)
    deribit = tuple(_finite(derived.get(name)) for name in DERIBIT_FEATURES)
    if any(value is None for value in baseline + deribit):
        return None
    return _Row(market, timestamp_ms, baseline, baseline + deribit, target)


def _load_rows(db_path: str | Path) -> list[_Row]:
    path = Path(db_path)
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "outcome_deribit_feature_rows" not in tables:
            return []
        records = conn.execute(
            "SELECT outcome_id,snapshot_timestamp_ms,features_json,labels_json "
            "FROM outcome_deribit_feature_rows WHERE feature_schema_version=? AND period='1d' "
            "AND deribit_valid=1 ORDER BY snapshot_timestamp_ms",
            (DERIBIT_FEATURE_SCHEMA_VERSION,),
        ).fetchall()
    rows = []
    for market, timestamp, raw_features, raw_labels in records:
        try:
            features, labels = json.loads(raw_features), json.loads(raw_labels)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(features, dict) and isinstance(labels, dict):
            parsed = _row(features, labels, int(market), int(timestamp))
            if parsed is not None:
                rows.append(parsed)
    return rows


def deribit_walk_forward_report(db_path: str | Path) -> DeribitWalkForwardReport:
    rows = _load_rows(db_path)
    grouped: dict[int, list[_Row]] = {}
    for row in rows:
        grouped.setdefault(row.market, []).append(row)
    instances = sorted(grouped, key=lambda market: min(row.timestamp_ms for row in grouped[market]))
    blockers: list[str] = []
    if len(instances) < MIN_MARKET_INSTANCES:
        blockers.append("insufficient_independent_daily_market_instances")
    folds: list[DeribitWalkForwardFold] = []
    baseline_predictions: list[float] = []
    deribit_predictions: list[float] = []
    targets: list[_Row] = []
    for position in range(2, len(instances)):
        test_market, train_markets = instances[position], instances[:position]
        raw_train = [row for market in train_markets for row in grouped[market]]
        train = []
        for market in train_markets:
            cutoff = max(row.timestamp_ms for row in grouped[market]) - PURGE_SEC * 1_000
            train.extend(row for row in grouped[market] if row.timestamp_ms <= cutoff)
        test = grouped[test_market]
        if len(train) < MIN_TRAIN_ROWS or len(test) < MIN_TEST_ROWS:
            continue
        baseline = _predict(train, test, extended=False)
        candidate = _predict(train, test, extended=True)
        base_rmse, base_mae = _errors(baseline, test)
        deribit_rmse, deribit_mae = _errors(candidate, test)
        folds.append(DeribitWalkForwardFold(
            test_market, len(train_markets), len(train), len(raw_train) - len(train), len(test),
            base_rmse, deribit_rmse, base_mae, deribit_mae,
        ))
        baseline_predictions.extend(baseline)
        deribit_predictions.extend(candidate)
        targets.extend(test)
    if not folds:
        blockers.append("insufficient_purged_walk_forward_rows")
    if len(targets) < MIN_OOS_ROWS:
        blockers.append("insufficient_out_of_sample_rows")
    if targets:
        baseline_rmse, baseline_mae = _errors(baseline_predictions, targets)
        deribit_rmse, deribit_mae = _errors(deribit_predictions, targets)
        rmse_improvement = baseline_rmse - deribit_rmse
        mae_improvement = baseline_mae - deribit_mae
    else:
        baseline_rmse = deribit_rmse = baseline_mae = deribit_mae = rmse_improvement = mae_improvement = None
    incremental = bool(
        baseline_rmse is not None and deribit_rmse is not None and baseline_mae is not None and deribit_mae is not None
        and deribit_rmse < baseline_rmse and deribit_mae < baseline_mae
        and not blockers
    )
    return DeribitWalkForwardReport(
        DERIBIT_FEATURE_SCHEMA_VERSION, LABEL_HORIZON_SEC, PURGE_SEC, len(rows), len(instances), tuple(folds),
        len(targets), baseline_rmse, deribit_rmse, rmse_improvement, baseline_mae, deribit_mae, mae_improvement,
        incremental, False, tuple(blockers),
    )


def as_json(db_path: str | Path) -> dict[str, Any]:
    return asdict(deribit_walk_forward_report(db_path))
