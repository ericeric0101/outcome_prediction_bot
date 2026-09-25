"""Leak-free zero-drift lognormal settlement-probability baseline.

This is a research-only estimator shared by the live shadow recorder and the
offline walk-forward evaluator. It deliberately returns unavailable instead
of interpolating missing/stale prices or extrapolating from a short window.
"""
from __future__ import annotations

import math
from typing import Iterable


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def estimate_settlement_probability(
    *,
    spot_price: float,
    strike: float,
    time_left_sec: float,
    as_of_ms: int,
    price_points: Iterable[tuple[int, float]],
    max_spot_age_ms: int | None = None,
    min_valid_returns: int = 20,
    min_coverage_sec: float = 900.0,
    max_gap_sec: float = 60.0,
    lookback_sec: float = 3600.0,
) -> dict[str, object]:
    """Estimate P(terminal BTC > strike) from trailing realized variance.

    ``price_points`` are (observation_timestamp_ms, price), not receipt order.
    Only observations at or before ``as_of_ms`` are used. Gaps longer than the
    configured maximum are omitted without interpolation.
    """
    if not all(math.isfinite(float(v)) for v in (spot_price, strike, time_left_sec)):
        return {"status": "unavailable", "reason": "non_finite_input"}
    if spot_price <= 0 or strike <= 0 or time_left_sec <= 0:
        return {"status": "unavailable", "reason": "invalid_spot_strike_or_time_left"}

    start_ms = int(as_of_ms - lookback_sec * 1000)
    points: list[tuple[int, float]] = []
    for raw_ts, raw_price in price_points:
        try:
            ts, price = int(raw_ts), float(raw_price)
        except (TypeError, ValueError, OverflowError):
            continue
        if start_ms <= ts <= as_of_ms and price > 0 and math.isfinite(price):
            points.append((ts, price))
    points.sort(key=lambda item: item[0])
    deduped: list[tuple[int, float]] = []
    for point in points:
        if deduped and point[0] == deduped[-1][0]:
            deduped[-1] = point
        else:
            deduped.append(point)
    if not deduped:
        return {"status": "unavailable", "reason": "volatility_history_missing", "history_points": 0,
                "valid_returns": 0, "coverage_sec": 0.0}
    spot_age_ms = int(as_of_ms - deduped[-1][0])
    if max_spot_age_ms is not None and (spot_age_ms < 0 or spot_age_ms > max_spot_age_ms):
        return {"status": "unavailable", "reason": "spot_observation_stale", "history_points": len(deduped),
                "latest_spot_age_ms": spot_age_ms}

    squared_log_returns = 0.0
    coverage_sec = 0.0
    valid_returns = 0
    excluded_gaps = 0
    for (left_ts, left_price), (right_ts, right_price) in zip(deduped, deduped[1:]):
        gap_sec = (right_ts - left_ts) / 1000.0
        if gap_sec <= 0:
            continue
        if gap_sec > max_gap_sec:
            excluded_gaps += 1
            continue
        log_return = math.log(right_price / left_price)
        squared_log_returns += log_return * log_return
        coverage_sec += gap_sec
        valid_returns += 1

    common = {
        "history_points": len(deduped), "valid_returns": valid_returns,
        "coverage_sec": round(coverage_sec, 3), "excluded_gaps": excluded_gaps,
        "latest_spot_age_ms": spot_age_ms,
    }
    if valid_returns < min_valid_returns or coverage_sec < min_coverage_sec:
        return {"status": "unavailable", "reason": "volatility_history_insufficient", **common,
                "required_valid_returns": min_valid_returns, "required_coverage_sec": min_coverage_sec}

    variance_per_sec = squared_log_returns / coverage_sec
    sigma_remaining = math.sqrt(max(variance_per_sec * time_left_sec, 1e-12))
    z = math.log(spot_price / strike) / sigma_remaining
    p_up = min(1.0 - 1e-6, max(1e-6, normal_cdf(z)))
    return {
        "status": "available", "reason": None, **common,
        "spot_price": spot_price, "strike": strike, "time_left_sec": time_left_sec,
        "variance_per_sec": variance_per_sec, "sigma_remaining": sigma_remaining, "z_score": z,
        "probability_up": p_up, "probability_down": 1.0 - p_up,
        "method": "zero_drift_lognormal_trailing_realized_variance",
        "live_authority": False, "execution_enabled": False,
    }
