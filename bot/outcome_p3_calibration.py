"""Bounded P3 consensus-entry / maker-exit calibration policy.

The policy deliberately uses the market's own two-sided midpoint consensus;
it never reads ForecastState, BTC trend, or SignalEngine.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from bot.outcome_execution_gateway import whole_share_size


@dataclass(frozen=True)
class OutcomeP3CalibrationConfig:
    max_daily_entries: int = 10
    target_return_pct: Decimal = Decimal("0.05")
    loss_reprice_pct: Decimal = Decimal("0.05")

    @classmethod
    def from_env(cls) -> "OutcomeP3CalibrationConfig":
        maximum = int(os.environ.get("OUTCOME_P3_CALIBRATION_MAX_DAILY_ENTRIES", "10"))
        target = Decimal(os.environ.get("OUTCOME_P3_CALIBRATION_TARGET_RETURN_PCT", "0.05"))
        loss = Decimal(os.environ.get("OUTCOME_P3_CALIBRATION_LOSS_REPRICE_PCT", "0.05"))
        if maximum < 1 or maximum > 10:
            raise ValueError("OUTCOME_P3_CALIBRATION_MAX_DAILY_ENTRIES must be between 1 and 10")
        if target < 0 or target >= Decimal("1"):
            raise ValueError("OUTCOME_P3_CALIBRATION_TARGET_RETURN_PCT must be in [0, 1)")
        if loss < 0 or loss >= Decimal("1"):
            raise ValueError("OUTCOME_P3_CALIBRATION_LOSS_REPRICE_PCT must be in [0, 1)")
        return cls(maximum, target, loss)


def take_profit_price(*, entry_price: Decimal, target_return_pct: Decimal, maker_close_fee_rate: Decimal) -> Decimal | None:
    """Return a net-of-maker-close-fee target, or None if HIP-4 price bounds forbid it."""
    if entry_price <= 0 or not Decimal("0") <= maker_close_fee_rate < Decimal("1"):
        return None
    target = entry_price * (Decimal("1") + target_return_pct) / (Decimal("1") - maker_close_fee_rate)
    return target if target < Decimal("0.99999") else None


def choose_consensus_calibration_side(
    *,
    mids: Mapping[int, Decimal],
    entry_bids: Mapping[int, Decimal],
    target_return_pct: Decimal,
    maker_close_fee_rate: Decimal,
    tie_breaker: int,
) -> int | None:
    """Pick the feasible side with the higher market midpoint consensus.

    This is an explicit consensus-following sampling policy, not a claim of
    alpha.  It deliberately does not force a low-probability complementary
    side merely to balance data collection.
    """
    candidates = [side for side in mids if side in entry_bids and Decimal("0") < entry_bids[side] < Decimal("1")]
    if not candidates:
        return None
    consensus_side = max(candidates, key=lambda side: (mids[side], -((side - tie_breaker) % 2)))
    bid = entry_bids[consensus_side]
    # Never fall back to the complementary low-consensus side merely because
    # the preferred side cannot fit the configured maker take-profit band.
    if take_profit_price(entry_price=bid, target_return_pct=target_return_pct, maker_close_fee_rate=maker_close_fee_rate) is None:
        return None
    return consensus_side if whole_share_size(bid) > 0 else None
