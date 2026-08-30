"""Pure, fail-closed reversal classification for an existing Outcome side."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class OutcomeReversalState(StrEnum):
    HOLD = "HOLD"
    WEAKENING = "WEAKENING"
    REVERSAL_CONFIRMED = "REVERSAL_CONFIRMED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OutcomeReversalInput:
    side_index: int
    fill_vwap: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    spot_strike_bps: Decimal | None
    mark_return_bps: Decimal | None
    oi_return_bps: Decimal | None
    book_healthy: bool
    consecutive_opposite_observations: int


@dataclass(frozen=True)
class OutcomeReversalDecision:
    state: OutcomeReversalState
    reason: str


class OutcomeReversalClassifier:
    """No missing input is treated as a directional fact."""

    def classify(self, item: OutcomeReversalInput) -> OutcomeReversalDecision:
        if item.side_index not in (0, 1) or not item.book_healthy:
            return OutcomeReversalDecision(OutcomeReversalState.UNKNOWN, "invalid_side_or_book")
        values = (item.fill_vwap, item.best_bid, item.best_ask, item.spot_strike_bps, item.mark_return_bps, item.oi_return_bps)
        if any(value is None for value in values):
            return OutcomeReversalDecision(OutcomeReversalState.UNKNOWN, "missing_asof_reversal_inputs")
        assert item.fill_vwap is not None and item.best_bid is not None and item.best_ask is not None
        assert item.spot_strike_bps is not None and item.mark_return_bps is not None and item.oi_return_bps is not None
        if item.fill_vwap <= 0 or item.best_bid <= 0 or item.best_ask <= item.best_bid:
            return OutcomeReversalDecision(OutcomeReversalState.UNKNOWN, "invalid_price_inputs")
        direction = Decimal("1") if item.side_index == 0 else Decimal("-1")
        opposite = direction * item.spot_strike_bps < 0 and direction * item.mark_return_bps < 0 and item.oi_return_bps > 0
        adverse = direction * (item.best_bid / item.fill_vwap - Decimal("1")) < Decimal("-0.02")
        if opposite and adverse and item.consecutive_opposite_observations >= 3:
            return OutcomeReversalDecision(OutcomeReversalState.REVERSAL_CONFIRMED, "three_asof_opposite_observations")
        if opposite or adverse:
            return OutcomeReversalDecision(OutcomeReversalState.WEAKENING, "single_opposite_or_adverse_observation")
        return OutcomeReversalDecision(OutcomeReversalState.HOLD, "no_confirmed_reversal")
