"""Read-only, WS-speed crash-risk telemetry for an already open Outcome holding.

This is intentionally not a stop-loss controller.  It records the ingredients
needed to calibrate one later: price velocity, top-three bid-depth depletion,
and the contemporaneous spot/mark/OI context.  A compact event is preferable
to retaining raw L2 payloads, and no output from this module has execution
authority.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class OutcomeCrashCircuitObservation:
    lifecycle_id: str
    outcome_id: int
    period: str
    coin: str
    timestamp: float
    fill_vwap: Decimal
    bid: Decimal
    ask: Decimal
    top3_bid_depth: Decimal
    spot_strike_bps: Decimal | None
    mark_return_bps: Decimal | None
    oi_return_bps: Decimal | None
    oi_age_ms: int | None
    regime_state: str | None
    reversal_state: str | None
    # Immutable lifecycle context makes a later thesis-failure analysis
    # attributable to this entry, rather than to a coin-wide price path.
    holding_age_sec: float | None = None
    time_left_sec: float | None = None
    entry_side_index: int | None = None
    entry_tier: str | None = None
    entry_target_return_pct: str | None = None


class OutcomeCrashCircuitShadow:
    """Bounded rolling WS observer, with deliberately non-authoritative labels."""

    _LOOKBACK_SEC = 90.0
    _HORIZONS_SEC = (15, 30, 60)

    def __init__(self) -> None:
        self._samples: dict[str, deque[tuple[float, Decimal, Decimal]]] = {}
        self._episode_active: dict[str, bool] = {}
        self._episode_id: dict[str, int] = {}

    @staticmethod
    def _change_bps(*, current: Decimal, prior: Decimal | None) -> Decimal | None:
        if prior is None or prior <= 0:
            return None
        return (current / prior - Decimal("1")) * Decimal("10000")

    @staticmethod
    def _prior(samples: deque[tuple[float, Decimal, Decimal]], *, now: float, horizon: int) -> tuple[Decimal, Decimal] | None:
        target = now - float(horizon)
        eligible = [row for row in samples if row[0] <= target]
        if not eligible:
            return None
        _, bid, depth = max(eligible, key=lambda row: row[0])
        return bid, depth

    def observe(self, observation: OutcomeCrashCircuitObservation) -> dict[str, Any]:
        """Return journal-safe research facts.  This method has no side effects beyond memory."""
        key = observation.lifecycle_id
        samples = self._samples.setdefault(key, deque())
        samples.append((observation.timestamp, observation.bid, observation.top3_bid_depth))
        cutoff = observation.timestamp - self._LOOKBACK_SEC
        while samples and samples[0][0] < cutoff:
            samples.popleft()

        velocity_bps: dict[str, str | None] = {}
        depth_ratio: dict[str, str | None] = {}
        for horizon in self._HORIZONS_SEC:
            prior = self._prior(samples, now=observation.timestamp, horizon=horizon)
            prior_bid, prior_depth = prior if prior is not None else (None, None)
            velocity = self._change_bps(current=observation.bid, prior=prior_bid)
            ratio = observation.top3_bid_depth / prior_depth if prior_depth is not None and prior_depth > 0 else None
            velocity_bps[str(horizon)] = str(velocity) if velocity is not None else None
            depth_ratio[str(horizon)] = str(ratio) if ratio is not None else None

        gross_return = observation.bid / observation.fill_vwap - Decimal("1") if observation.fill_vwap > 0 else None
        velocity_30 = self._change_bps(
            current=observation.bid,
            prior=(self._prior(samples, now=observation.timestamp, horizon=30) or (None, None))[0],
        )
        depth_30 = (self._prior(samples, now=observation.timestamp, horizon=30) or (None, None))[1]
        depth_ratio_30 = observation.top3_bid_depth / depth_30 if depth_30 is not None and depth_30 > 0 else None

        # These are *research buckets*, not risk limits.  They make it
        # possible to analyse joint losses/velocity/depth later without
        # silently turning an arbitrary number into a live stop-loss.
        rapid_drawdown = bool(
            gross_return is not None and gross_return <= Decimal("-0.05")
            and velocity_30 is not None and velocity_30 <= Decimal("-250")
            and depth_ratio_30 is not None and depth_ratio_30 <= Decimal("0.70")
        )
        drawdown = bool(gross_return is not None and gross_return <= Decimal("-0.02"))
        state = "RAPID_DRAWDOWN_RESEARCH" if rapid_drawdown else ("DRAWDOWN_RESEARCH" if drawdown else "STABLE_RESEARCH")
        was_active = self._episode_active.get(key, False)
        if rapid_drawdown and not was_active:
            self._episode_id[key] = self._episode_id.get(key, 0) + 1
        self._episode_active[key] = rapid_drawdown
        episode = self._episode_id.get(key, 0) or None
        return {
            "schema_version": 1,
            "read_only": True,
            "execution_submitted": False,
            "live_authority": False,
            "venue": "hyperliquid_outcome",
            "outcome_id": observation.outcome_id, "period": observation.period,
            "coin": observation.coin,
            "entry_lifecycle_id": observation.lifecycle_id,
            "fill_vwap": str(observation.fill_vwap),
            "ws_best_bid": str(observation.bid),
            "ws_best_ask": str(observation.ask),
            "ws_top3_bid_depth": str(observation.top3_bid_depth),
            "ws_top_bid_gross_return_pct": str(gross_return) if gross_return is not None else None,
            "bid_velocity_bps": velocity_bps,
            "top3_depth_ratio": depth_ratio,
            "research_state": state,
            "research_episode_id": episode,
            "episode_started": rapid_drawdown and not was_active,
            "spot_strike_bps": str(observation.spot_strike_bps) if observation.spot_strike_bps is not None else None,
            "mark_return_bps": str(observation.mark_return_bps) if observation.mark_return_bps is not None else None,
            "oi_return_bps": str(observation.oi_return_bps) if observation.oi_return_bps is not None else None,
            "oi_age_ms": observation.oi_age_ms,
            "regime_state": observation.regime_state,
            "reversal_state": observation.reversal_state,
            "holding_age_sec": observation.holding_age_sec,
            "time_left_sec": observation.time_left_sec,
            "entry_side_index": observation.entry_side_index,
            "entry_tier": observation.entry_tier,
            "entry_target_return_pct": observation.entry_target_return_pct,
            "limits": [
                "WS top-of-book and top-3 depth are telemetry only, not full-inventory executable depth.",
                "research_state and research_episode_id never submit, cancel, or authorize an IOC.",
            ],
        }
