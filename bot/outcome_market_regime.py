"""Compact, shadow-only market-regime and post-fill execution-risk observer.

This module deliberately has no exchange client, journal, or order authority.
It classifies public, as-of inputs for research alongside the live strategy;
callers may record its output, but it cannot admit, cancel, or close an order.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Deque


class OutcomeMarketRegime(StrEnum):
    UNKNOWN = "UNKNOWN"
    TREND = "TREND"
    TRANSITION = "TRANSITION"
    RANGE = "RANGE"


class OutcomeExecutionRisk(StrEnum):
    NORMAL = "NORMAL"
    DETERIORATING = "DETERIORATING"
    TOXIC_FILL = "TOXIC_FILL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OutcomeMarketRegimeInput:
    outcome_id: int
    now_ts: float
    yes_midpoint: Decimal | None
    spot_strike_bps: Decimal | None
    mark_5m_bps: Decimal | None
    mark_15m_bps: Decimal | None
    mark_60m_bps: Decimal | None
    candidate_side_index: int | None


@dataclass(frozen=True)
class OutcomeMarketRegimeDecision:
    state: OutcomeMarketRegime
    reason: str
    confirmed_crosses_2h: int
    last_cross_at: float | None
    current_zone: str | None


@dataclass(frozen=True)
class OutcomeToxicFillInput:
    outcome_id: int
    coin: str
    now_ts: float
    holding_age_sec: float
    fill_vwap: Decimal | None
    best_bid: Decimal | None
    top3_bid_depth: Decimal | None


@dataclass(frozen=True)
class OutcomeToxicFillDecision:
    state: OutcomeExecutionRisk
    reason: str
    observation_count: int
    duration_sec: float
    executable_return_pct: Decimal | None
    bid_drift_bps: Decimal | None
    depth_ratio: Decimal | None


@dataclass
class _RegimeMemory:
    candidate_zone: str | None = None
    candidate_started_at: float | None = None
    confirmed_zone: str | None = None
    crossings: Deque[float] | None = None
    last_cross_at: float | None = None


@dataclass
class _ToxicMemory:
    baseline_bid: Decimal
    baseline_depth: Decimal
    first_risk_at: float | None = None
    last_risk_at: float | None = None
    risk_count: int = 0


class OutcomeMarketRegimeShadow:
    """Stateful, in-memory classifier used only for shadow observations.

    A crossover is intentionally not a tick through 50%.  The YES midpoint
    must move from a durably confirmed <=48% zone to >=52%, or vice versa,
    and remain there for ten minutes.  This avoids treating one-book noise as
    a regime transition.  A restart resets this research memory rather than
    inventing continuity from a prior process.
    """

    lower_zone = Decimal("0.48")
    upper_zone = Decimal("0.52")
    min_zone_duration_sec = 10 * 60.0
    transition_settle_sec = 30 * 60.0
    crossover_window_sec = 2 * 60 * 60.0
    range_cross_count = 2
    mark_min_bps = Decimal("5")

    toxic_max_age_sec = 2 * 60.0
    toxic_loss_pct = Decimal("-0.05")
    toxic_bid_drift_bps = Decimal("100")
    toxic_depth_ratio = Decimal("0.70")
    toxic_min_observations = 3
    toxic_min_duration_sec = 5.0
    toxic_min_sample_interval_sec = 2.0

    def __init__(self) -> None:
        self._regimes: dict[int, _RegimeMemory] = {}
        self._toxic: dict[tuple[int, str], _ToxicMemory] = {}

    @classmethod
    def _zone(cls, midpoint: Decimal | None) -> str | None:
        if midpoint is None or not Decimal("0") < midpoint < Decimal("1"):
            return None
        if midpoint <= cls.lower_zone:
            return "DOWN"
        if midpoint >= cls.upper_zone:
            return "UP"
        return None

    @staticmethod
    def _direction(side_index: int | None) -> Decimal | None:
        if side_index == 0:
            return Decimal("1")
        if side_index == 1:
            return Decimal("-1")
        return None

    def observe_market(self, item: OutcomeMarketRegimeInput) -> OutcomeMarketRegimeDecision:
        memory = self._regimes.setdefault(item.outcome_id, _RegimeMemory(crossings=deque()))
        assert memory.crossings is not None
        zone = self._zone(item.yes_midpoint)
        if zone is None:
            memory.candidate_zone = None
            memory.candidate_started_at = None
        elif zone != memory.candidate_zone:
            memory.candidate_zone, memory.candidate_started_at = zone, item.now_ts
        elif memory.candidate_started_at is not None and item.now_ts - memory.candidate_started_at >= self.min_zone_duration_sec:
            if memory.confirmed_zone is None:
                memory.confirmed_zone = zone
            elif memory.confirmed_zone != zone:
                memory.confirmed_zone = zone
                memory.crossings.append(item.now_ts)
                memory.last_cross_at = item.now_ts
        while memory.crossings and item.now_ts - memory.crossings[0] > self.crossover_window_sec:
            memory.crossings.popleft()
        crosses = len(memory.crossings)
        if crosses >= self.range_cross_count:
            return OutcomeMarketRegimeDecision(OutcomeMarketRegime.RANGE, "two_durable_midpoint_crosses_within_2h", crosses, memory.last_cross_at, zone)

        direction = self._direction(item.candidate_side_index)
        values = (item.spot_strike_bps, item.mark_5m_bps, item.mark_15m_bps, item.mark_60m_bps)
        if direction is None or any(value is None for value in values):
            return OutcomeMarketRegimeDecision(OutcomeMarketRegime.UNKNOWN, "insufficient_multihorizon_evidence", crosses, memory.last_cross_at, zone)
        assert all(value is not None for value in values)
        spot, mark5, mark15, mark60 = values  # type: ignore[misc]
        assert spot is not None and mark5 is not None and mark15 is not None and mark60 is not None
        aligned = (
            direction * spot >= self.mark_min_bps
            and direction * mark5 >= self.mark_min_bps
            and direction * mark15 >= self.mark_min_bps
            and direction * mark60 >= self.mark_min_bps
        )
        conflict = (
            direction * spot <= -self.mark_min_bps
            or direction * mark5 <= -self.mark_min_bps
            or direction * mark15 <= -self.mark_min_bps
            or direction * mark60 <= -self.mark_min_bps
        )
        if conflict:
            return OutcomeMarketRegimeDecision(OutcomeMarketRegime.TRANSITION, "multihorizon_direction_conflict", crosses, memory.last_cross_at, zone)
        if memory.last_cross_at is not None and item.now_ts - memory.last_cross_at < self.transition_settle_sec:
            return OutcomeMarketRegimeDecision(OutcomeMarketRegime.TRANSITION, "post_crossover_settling_window", crosses, memory.last_cross_at, zone)
        if aligned:
            return OutcomeMarketRegimeDecision(OutcomeMarketRegime.TREND, "spot_and_5m_15m_60m_aligned", crosses, memory.last_cross_at, zone)
        return OutcomeMarketRegimeDecision(OutcomeMarketRegime.UNKNOWN, "multihorizon_alignment_not_established", crosses, memory.last_cross_at, zone)

    def observe_toxic_fill(self, item: OutcomeToxicFillInput) -> OutcomeToxicFillDecision:
        key = (item.outcome_id, item.coin)
        if (
            item.fill_vwap is None or item.best_bid is None or item.top3_bid_depth is None
            or item.fill_vwap <= 0 or item.best_bid <= 0 or item.top3_bid_depth <= 0
            or item.holding_age_sec < 0 or item.holding_age_sec > self.toxic_max_age_sec
        ):
            self._toxic.pop(key, None)
            return OutcomeToxicFillDecision(OutcomeExecutionRisk.UNKNOWN, "outside_toxic_fill_observation_window_or_invalid_book", 0, 0.0, None, None, None)
        memory = self._toxic.get(key)
        if memory is None:
            self._toxic[key] = _ToxicMemory(item.best_bid, item.top3_bid_depth)
            return OutcomeToxicFillDecision(OutcomeExecutionRisk.NORMAL, "toxic_fill_baseline_recorded", 0, 0.0, Decimal("0"), Decimal("0"), Decimal("1"))
        executable_return = item.best_bid / item.fill_vwap - Decimal("1")
        bid_drift = max(Decimal("0"), (memory.baseline_bid - item.best_bid) / memory.baseline_bid * Decimal("10000"))
        depth_ratio = item.top3_bid_depth / memory.baseline_depth if memory.baseline_depth > 0 else None
        deteriorating = (
            executable_return <= self.toxic_loss_pct
            and bid_drift >= self.toxic_bid_drift_bps
            and depth_ratio is not None and depth_ratio <= self.toxic_depth_ratio
        )
        if not deteriorating:
            memory.first_risk_at = None
            memory.last_risk_at = None
            memory.risk_count = 0
            return OutcomeToxicFillDecision(OutcomeExecutionRisk.NORMAL, "toxic_fill_conditions_not_jointly_met", 0, 0.0, executable_return, bid_drift, depth_ratio)
        if memory.first_risk_at is None:
            memory.first_risk_at = memory.last_risk_at = item.now_ts
            memory.risk_count = 1
        elif memory.last_risk_at is not None and item.now_ts - memory.last_risk_at >= self.toxic_min_sample_interval_sec:
            memory.last_risk_at = item.now_ts
            memory.risk_count += 1
        duration = max(0.0, item.now_ts - (memory.first_risk_at or item.now_ts))
        state = OutcomeExecutionRisk.TOXIC_FILL if (
            memory.risk_count >= self.toxic_min_observations and duration >= self.toxic_min_duration_sec
        ) else OutcomeExecutionRisk.DETERIORATING
        reason = "toxic_fill_shadow_confirmed" if state is OutcomeExecutionRisk.TOXIC_FILL else "toxic_fill_shadow_pending_confirmation"
        return OutcomeToxicFillDecision(state, reason, memory.risk_count, duration, executable_return, bid_drift, depth_ratio)
