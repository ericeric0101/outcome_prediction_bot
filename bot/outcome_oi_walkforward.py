"""X4: purged, market-instance walk-forward comparison for Outcome 1d research.

This is deliberately an offline report.  It never imports execution code and
cannot select a live side.  Its first target is the *future executable YES
long markout* at five minutes, rather than a reconstructed midpoint or an
unverified settlement outcome.  The only question it answers is whether OI
features add out-of-sample explanatory value beyond the Outcome book itself.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from bot.outcome_oi_features import FEATURE_SCHEMA_VERSION

LABEL_HORIZON_SEC = 300
PURGE_SEC = LABEL_HORIZON_SEC
MIN_MARKET_INSTANCES = 5
MIN_TRAIN_ROWS = 100
MIN_TEST_ROWS = 20
MIN_OOS_ROWS = 200
# This is explicitly provisional: 30m blocks and small counts let a running
# daily market produce diagnostics in hours, not days.  X4 final keeps the
# much stronger independent-market requirements above.
INTRADAY_BLOCK_SEC = 1800
INTRADAY_MIN_TRAIN_ROWS = 18
INTRADAY_MIN_TEST_ROWS = 3
INTRADAY_MIN_OOS_ROWS = 18

BASELINE_FEATURES = (
    "yes_mid", "yes_spread", "yes_depth_imbalance", "yes_probability_distance", "time_left_fraction",
)
OI_FEATURES = (
    "btc_mark_return_300s_bps", "btc_mark_return_900s_bps", "btc_mark_return_3600s_bps",
    "oi_return_300s_bps", "oi_return_900s_bps", "oi_return_3600s_bps",
    "oi_acceleration_5m_vs_15m_bps", "price_oi_divergence_5m_bps", "oi_zscore_1h", "taker_imbalance",
)


@dataclass(frozen=True)
class _Row:
    market_instance: int
    timestamp_ms: int
    baseline: tuple[float, ...]
    extended: tuple[float, ...]
    target: float


@dataclass(frozen=True)
class X4Fold:
    test_market_instance: int
    train_market_instances: int
    train_rows: int
    purged_train_rows: int
    test_rows: int
    baseline_rmse: float
    oi_extended_rmse: float
    baseline_mae: float
    oi_extended_mae: float


@dataclass(frozen=True)
class X4WalkForwardReport:
    feature_schema_version: int
    label_horizon_sec: int
    purge_sec: int
    eligible_rows: int
    market_instances: int
    folds: tuple[X4Fold, ...]
    oos_rows: int
    baseline_rmse: float | None
    oi_extended_rmse: float | None
    rmse_improvement: float | None
    baseline_mae: float | None
    oi_extended_mae: float | None
    mae_improvement: float | None
    incremental_evidence: bool
    ready_for_x5: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class X4IntradayHorizonReport:
    label_horizon_sec: int
    block_sec: int
    sampled_rows: int
    chronological_blocks: int
    folds: tuple[X4Fold, ...]
    oos_rows: int
    baseline_rmse: float | None
    oi_extended_rmse: float | None
    rmse_improvement: float | None
    baseline_mae: float | None
    oi_extended_mae: float | None
    mae_improvement: float | None
    provisional_incremental_signal: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class X4IntradayReport:
    """Fast, explicitly non-independent evidence while a 1d market runs."""
    feature_schema_version: int
    provisional_only: bool
    horizons: tuple[X4IntradayHorizonReport, ...]
    ready_for_x5: bool


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Small pivoted Gaussian solver; avoids adding an unreviewed ML dependency."""
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


def _ridge_predict(train: list[_Row], test: list[_Row], *, extended: bool, ridge: float = 1e-3) -> list[float]:
    raw_train = [row.extended if extended else row.baseline for row in train]
    raw_test = [row.extended if extended else row.baseline for row in test]
    width = len(raw_train[0])
    means = [sum(row[index] for row in raw_train) / len(raw_train) for index in range(width)]
    scales = [math.sqrt(sum((row[index] - means[index]) ** 2 for row in raw_train) / len(raw_train)) for index in range(width)]
    scales = [scale if scale > 1e-12 else 1.0 for scale in scales]
    design = [[1.0] + [(value - means[index]) / scales[index] for index, value in enumerate(row)] for row in raw_train]
    target = [row.target for row in train]
    dimensions = width + 1
    gram = [[sum(row[left] * row[right] for row in design) for right in range(dimensions)] for left in range(dimensions)]
    for index in range(1, dimensions):
        gram[index][index] += ridge
    rhs = [sum(row[index] * value for row, value in zip(design, target)) for index in range(dimensions)]
    coefficients = _solve(gram, rhs)
    if coefficients is None:
        return [sum(target) / len(target)] * len(test)
    return [coefficients[0] + sum(coefficients[index + 1] * ((value - means[index]) / scales[index]) for index, value in enumerate(row)) for row in raw_test]


def _errors(predictions: Iterable[float], rows: Iterable[_Row]) -> tuple[float, float]:
    pairs = [(prediction, row.target) for prediction, row in zip(predictions, rows)]
    return (
        math.sqrt(sum((prediction - target) ** 2 for prediction, target in pairs) / len(pairs)),
        sum(abs(prediction - target) for prediction, target in pairs) / len(pairs),
    )


def _row(features: dict[str, Any], labels: dict[str, Any], outcome_id: int, timestamp_ms: int, *, label_horizon_sec: int) -> _Row | None:
    label = labels.get(f"future_{label_horizon_sec}s", {})
    target = _finite(label.get("yes_long_markout_ps")) if label.get("available") is True else None
    yes_bid, yes_ask = _finite(features.get("yes_bid")), _finite(features.get("yes_ask"))
    bid_size, ask_size = _finite(features.get("yes_bid_size")), _finite(features.get("yes_ask_size"))
    time_left = _finite(features.get("time_left_sec"))
    if None in (target, yes_bid, yes_ask, bid_size, ask_size, time_left) or yes_ask <= yes_bid or time_left < 0:
        return None
    depth = bid_size + ask_size
    if depth <= 0:
        return None
    derived = dict(features)
    derived.update({
        "yes_mid": (yes_bid + yes_ask) / 2,
        "yes_spread": yes_ask - yes_bid,
        "yes_depth_imbalance": (bid_size - ask_size) / depth,
        "yes_probability_distance": (yes_bid + yes_ask) / 2 - 0.5,
        "time_left_fraction": min(time_left, 86_400.0) / 86_400.0,
    })
    baseline = tuple(_finite(derived.get(name)) for name in BASELINE_FEATURES)
    oi = tuple(_finite(derived.get(name)) for name in OI_FEATURES)
    if any(value is None for value in baseline + oi):
        return None
    return _Row(outcome_id, timestamp_ms, baseline, baseline + oi, target)


def _load_rows(db_path: str | Path, *, label_horizon_sec: int = LABEL_HORIZON_SEC) -> list[_Row]:
    path = Path(db_path)
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "outcome_oi_feature_rows" not in tables:
            return []
        source = conn.execute("""
            SELECT outcome_id,snapshot_timestamp_ms,features_json,labels_json
            FROM outcome_oi_feature_rows
            WHERE feature_schema_version=? AND period='1d' AND oi_backfilled=0 AND oi_observation_id IS NOT NULL
            ORDER BY snapshot_timestamp_ms
        """, (FEATURE_SCHEMA_VERSION,)).fetchall()
    output = []
    for outcome_id, timestamp_ms, raw_features, raw_labels in source:
        try:
            features, labels = json.loads(raw_features), json.loads(raw_labels)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(features, dict) and isinstance(labels, dict):
            parsed = _row(features, labels, int(outcome_id), int(timestamp_ms), label_horizon_sec=label_horizon_sec)
            if parsed is not None:
                output.append(parsed)
    return output


def x4_walk_forward_report(db_path: str | Path) -> X4WalkForwardReport:
    rows = _load_rows(db_path)
    grouped: dict[int, list[_Row]] = {}
    for row in rows:
        grouped.setdefault(row.market_instance, []).append(row)
    ordered_instances = sorted(grouped, key=lambda instance: min(row.timestamp_ms for row in grouped[instance]))
    blockers: list[str] = []
    if len(ordered_instances) < MIN_MARKET_INSTANCES:
        blockers.append("insufficient_independent_daily_market_instances")
    folds: list[X4Fold] = []
    baseline_predictions: list[float] = []
    extended_predictions: list[float] = []
    targets: list[_Row] = []
    for position in range(2, len(ordered_instances)):
        test_instance = ordered_instances[position]
        train_instances = ordered_instances[:position]
        raw_train = [row for instance in train_instances for row in grouped[instance]]
        # Purge each source market's trailing label horizon.  Splitting by
        # full market instance already prevents cross-instance overlap; this
        # additionally makes the rule explicit and auditable.
        train = []
        for instance in train_instances:
            cutoff = max(row.timestamp_ms for row in grouped[instance]) - PURGE_SEC * 1000
            train.extend(row for row in grouped[instance] if row.timestamp_ms <= cutoff)
        test = grouped[test_instance]
        if len(train) < MIN_TRAIN_ROWS or len(test) < MIN_TEST_ROWS:
            continue
        base = _ridge_predict(train, test, extended=False)
        extended = _ridge_predict(train, test, extended=True)
        base_rmse, base_mae = _errors(base, test)
        ext_rmse, ext_mae = _errors(extended, test)
        folds.append(X4Fold(test_instance, len(train_instances), len(train), len(raw_train) - len(train), len(test), base_rmse, ext_rmse, base_mae, ext_mae))
        baseline_predictions.extend(base)
        extended_predictions.extend(extended)
        targets.extend(test)
    if not folds:
        blockers.append("insufficient_purged_walk_forward_rows")
    if len(targets) < MIN_OOS_ROWS:
        blockers.append("insufficient_out_of_sample_rows")
    baseline_rmse = baseline_mae = extended_rmse = extended_mae = None
    if targets:
        baseline_rmse, baseline_mae = _errors(baseline_predictions, targets)
        extended_rmse, extended_mae = _errors(extended_predictions, targets)
    rmse_improvement = baseline_rmse - extended_rmse if baseline_rmse is not None and extended_rmse is not None else None
    mae_improvement = baseline_mae - extended_mae if baseline_mae is not None and extended_mae is not None else None
    incremental = bool(not blockers and rmse_improvement is not None and mae_improvement is not None and rmse_improvement > 0 and mae_improvement > 0)
    if not incremental:
        blockers.append("oi_incremental_oos_improvement_not_established")
    # X4 is evidence only.  X5 additionally requires P0/P2/P3 gates and an
    # independently frozen policy, so a report can never authorize trading.
    return X4WalkForwardReport(FEATURE_SCHEMA_VERSION, LABEL_HORIZON_SEC, PURGE_SEC, len(rows), len(ordered_instances), tuple(folds), len(targets), baseline_rmse, extended_rmse, rmse_improvement, baseline_mae, extended_mae, mae_improvement, incremental, False, tuple(blockers))


def _non_overlapping_rows(rows: list[_Row], horizon_sec: int) -> list[_Row]:
    """One decision per horizon per market; prevents snapshot overlap inflation."""
    selected: list[_Row] = []
    last_by_market: dict[int, int] = {}
    for row in rows:
        last = last_by_market.get(row.market_instance)
        if last is None or row.timestamp_ms - last >= horizon_sec * 1000:
            selected.append(row)
            last_by_market[row.market_instance] = row.timestamp_ms
    return selected


def _intraday_horizon_report(db_path: str | Path, horizon_sec: int) -> X4IntradayHorizonReport:
    sampled = _non_overlapping_rows(_load_rows(db_path, label_horizon_sec=horizon_sec), horizon_sec)
    first_by_market: dict[int, int] = {}
    for row in sampled:
        first_by_market.setdefault(row.market_instance, row.timestamp_ms)
    block_ms = INTRADAY_BLOCK_SEC * 1000
    grouped: dict[tuple[int, int], list[_Row]] = {}
    for row in sampled:
        key = (row.market_instance, (row.timestamp_ms - first_by_market[row.market_instance]) // block_ms)
        grouped.setdefault(key, []).append(row)
    ordered = sorted(grouped, key=lambda key: min(row.timestamp_ms for row in grouped[key]))
    folds: list[X4Fold] = []
    base_predictions: list[float] = []
    extended_predictions: list[float] = []
    targets: list[_Row] = []
    for position in range(1, len(ordered)):
        test_key = ordered[position]
        test = grouped[test_key]
        test_start = min(row.timestamp_ms for row in test)
        raw_train = [row for key in ordered[:position] for row in grouped[key]]
        # Embargo exactly one label horizon before the test block.  Together
        # with non-overlapping sampling this prevents a label spanning the
        # train/test boundary from being counted twice.
        train = [row for row in raw_train if row.timestamp_ms <= test_start - horizon_sec * 1000]
        if len(train) < INTRADAY_MIN_TRAIN_ROWS or len(test) < INTRADAY_MIN_TEST_ROWS:
            continue
        base = _ridge_predict(train, test, extended=False)
        extended = _ridge_predict(train, test, extended=True)
        base_rmse, base_mae = _errors(base, test)
        ext_rmse, ext_mae = _errors(extended, test)
        folds.append(X4Fold(test_key[0], len({row.market_instance for row in train}), len(train), len(raw_train) - len(train), len(test), base_rmse, ext_rmse, base_mae, ext_mae))
        base_predictions.extend(base)
        extended_predictions.extend(extended)
        targets.extend(test)
    blockers: list[str] = []
    if len(ordered) < 2:
        blockers.append("insufficient_intraday_time_blocks")
    if not folds:
        blockers.append("insufficient_purged_intraday_rows")
    if len(targets) < INTRADAY_MIN_OOS_ROWS:
        blockers.append("insufficient_intraday_oos_rows")
    baseline_rmse = baseline_mae = extended_rmse = extended_mae = None
    if targets:
        baseline_rmse, baseline_mae = _errors(base_predictions, targets)
        extended_rmse, extended_mae = _errors(extended_predictions, targets)
    rmse_gain = baseline_rmse - extended_rmse if baseline_rmse is not None and extended_rmse is not None else None
    mae_gain = baseline_mae - extended_mae if baseline_mae is not None and extended_mae is not None else None
    provisional = bool(not blockers and rmse_gain is not None and mae_gain is not None and rmse_gain > 0 and mae_gain > 0)
    if not provisional:
        blockers.append("oi_incremental_intraday_improvement_not_established")
    return X4IntradayHorizonReport(horizon_sec, INTRADAY_BLOCK_SEC, len(sampled), len(ordered), tuple(folds), len(targets), baseline_rmse, extended_rmse, rmse_gain, baseline_mae, extended_mae, mae_gain, provisional, tuple(blockers))


def x4_intraday_report(db_path: str | Path, *, horizons: tuple[int, ...] = (300, 900)) -> X4IntradayReport:
    """Return fast 5m/15m diagnostics, never a live-policy approval."""
    if any(horizon not in (60, 300, 600, 900, 1800, 3600) for horizon in horizons):
        raise ValueError("intraday horizon must be one of the X3 executable label horizons")
    return X4IntradayReport(FEATURE_SCHEMA_VERSION, True, tuple(_intraday_horizon_report(db_path, horizon) for horizon in horizons), False)


def as_dict(db_path: str | Path) -> dict[str, Any]:
    return asdict(x4_walk_forward_report(db_path))


def intraday_as_dict(db_path: str | Path) -> dict[str, Any]:
    return asdict(x4_intraday_report(db_path))
