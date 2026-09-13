"""Read-only market-risk state for an already-owned Outcome lifecycle.

It deliberately has no account, gateway, SDK, controller, or mutation import.
Its output is research authorization only; the existing exit lifecycle remains
the sole owner of cancel/rebook/IOC execution.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class OutcomeMarketRiskObservation:
    lifecycle_id: str
    outcome_id: int
    coin: str
    period: str
    timestamp: float
    entry_price: Decimal
    position_size: Decimal
    best_bid: Decimal
    best_ask: Decimal
    top1_depth: Decimal
    top3_depth: Decimal
    side_index: int
    time_left_sec: float | None
    entry_time_left_sec: float | None
    spot_strike_bps: Decimal | None
    mark_return_bps: Decimal | None
    oi_return_bps: Decimal | None
    oi_age_ms: int | None
    reversal_state: str | None
    independent_confirmation_count: int
    full_inventory_executable_vwap: Decimal | None = None


class OutcomeMarketRiskMonitor:
    """Bounded in-memory monitor; every returned decision is shadow-only."""

    _LOOKBACK_SEC = 90.0
    _HORIZONS = (5, 10, 30)

    def __init__(self) -> None:
        self._samples: dict[str, deque[tuple[float, Decimal, Decimal]]] = {}
        self._dislocation_started: dict[str, float] = {}

    @staticmethod
    def _prior(samples: deque[tuple[float, Decimal, Decimal]], now: float, horizon: int) -> tuple[Decimal, Decimal] | None:
        candidates = [row for row in samples if row[0] <= now - horizon]
        if not candidates:
            return None
        _, bid, depth = candidates[-1]
        return bid, depth

    @staticmethod
    def _bps(current: Decimal, prior: Decimal | None) -> Decimal | None:
        if prior is None or prior <= 0:
            return None
        return (current / prior - Decimal("1")) * Decimal("10000")

    def observe(self, item: OutcomeMarketRiskObservation) -> dict[str, Any]:
        samples = self._samples.setdefault(item.lifecycle_id, deque())
        samples.append((item.timestamp, item.best_bid, item.top3_depth))
        while samples and samples[0][0] < item.timestamp - self._LOOKBACK_SEC:
            samples.popleft()
        velocity, ratios = {}, {}
        for horizon in self._HORIZONS:
            previous = self._prior(samples, item.timestamp, horizon)
            prior_bid, prior_depth = previous if previous is not None else (None, None)
            velocity[str(horizon)] = self._bps(item.best_bid, prior_bid)
            ratios[str(horizon)] = item.top3_depth / prior_depth if prior_depth and prior_depth > 0 else None
        executable_price = item.full_inventory_executable_vwap or item.best_bid
        executable_return = executable_price / item.entry_price - Decimal("1") if item.entry_price > 0 else None
        direction = Decimal("1") if item.side_index == 0 else Decimal("-1")
        thesis_still_same_side = (
            item.spot_strike_bps is not None and direction * item.spot_strike_bps > 0
        )
        v30, d30 = velocity["30"], ratios["30"]
        dislocated = bool(executable_return is not None and executable_return <= Decimal("-0.05") and thesis_still_same_side)
        if dislocated:
            started = self._dislocation_started.setdefault(item.lifecycle_id, item.timestamp)
        else:
            self._dislocation_started.pop(item.lifecycle_id, None)
            started = None
        severe = bool(
            executable_return is not None and executable_return <= Decimal("-0.10")
            and v30 is not None and v30 <= Decimal("-250")
            and d30 is not None and d30 <= Decimal("0.70")
        )
        state = "HARD_CAPITAL_PROTECTION_SHADOW" if severe else (
            "RISK_COMPRESSION_SHADOW" if dislocated else "NORMAL"
        )
        return {
            "schema_version": 1, "read_only": True, "live_authority": False,
            "execution_submitted": False, "outcome_id": item.outcome_id,
            "period": item.period, "coin": item.coin, "entry_lifecycle_id": item.lifecycle_id,
            "entry_price": str(item.entry_price), "position_size": str(item.position_size),
            "entry_time_left_sec": item.entry_time_left_sec, "current_time_left_sec": item.time_left_sec,
            "full_inventory_executable_vwap": str(item.full_inventory_executable_vwap) if item.full_inventory_executable_vwap is not None else None,
            "executable_return_pct": str(executable_return) if executable_return is not None else None,
            "best_bid": str(item.best_bid), "best_ask": str(item.best_ask),
            "spread_bps": str((item.best_ask / item.best_bid - Decimal("1")) * Decimal("10000")) if item.best_bid > 0 else None,
            "top1_depth": str(item.top1_depth), "top3_depth": str(item.top3_depth),
            "bid_velocity_bps": {key: str(value) if value is not None else None for key, value in velocity.items()},
            "depth_ratio_vs_baseline": {key: str(value) if value is not None else None for key, value in ratios.items()},
            "spot_strike_bps": str(item.spot_strike_bps) if item.spot_strike_bps is not None else None,
            "mark_return_bps": str(item.mark_return_bps) if item.mark_return_bps is not None else None,
            "oi_return_bps": str(item.oi_return_bps) if item.oi_return_bps is not None else None,
            "oi_age_ms": item.oi_age_ms, "reversal_state": item.reversal_state,
            "independent_confirmation_count": item.independent_confirmation_count,
            "dislocation_start_ts": started,
            "dislocation_duration_sec": (item.timestamp - started) if started is not None else 0.0,
            "state": state,
            "would_compress_target": state != "NORMAL",
            "would_aggressively_reprice": state != "NORMAL",
            "would_freeze_additional_exposure": state != "NORMAL",
            "limits": ["shadow only", "no mutation authority", "full depth is null when WS only exposes top-of-book"],
        }
