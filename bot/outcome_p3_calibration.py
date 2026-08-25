"""Bounded, non-directional P3 maker-fill calibration policy.

This module deliberately decides *sampling side*, not trade direction.  It is
used only by the explicit calibration runtime and never by SignalEngine.
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
    target_return_pct: Decimal = Decimal("0.10")

    @classmethod
    def from_env(cls) -> "OutcomeP3CalibrationConfig":
        maximum = int(os.environ.get("OUTCOME_P3_CALIBRATION_MAX_DAILY_ENTRIES", "10"))
        target = Decimal(os.environ.get("OUTCOME_P3_CALIBRATION_TARGET_RETURN_PCT", "0.10"))
        if maximum < 1 or maximum > 10:
            raise ValueError("OUTCOME_P3_CALIBRATION_MAX_DAILY_ENTRIES must be between 1 and 10")
        if target < 0 or target >= Decimal("1"):
            raise ValueError("OUTCOME_P3_CALIBRATION_TARGET_RETURN_PCT must be in [0, 1)")
        return cls(maximum, target)


def take_profit_price(*, entry_price: Decimal, target_return_pct: Decimal, maker_close_fee_rate: Decimal) -> Decimal | None:
    """Return a net-of-maker-close-fee target, or None if HIP-4 price bounds forbid it."""
    if entry_price <= 0 or not Decimal("0") <= maker_close_fee_rate < Decimal("1"):
        return None
    target = entry_price * (Decimal("1") + target_return_pct) / (Decimal("1") - maker_close_fee_rate)
    return target if target < Decimal("0.99999") else None


def choose_balanced_calibration_side(
    *,
    bids: Mapping[int, Decimal],
    maker_fill_counts: Mapping[int, int],
    target_return_pct: Decimal,
    maker_close_fee_rate: Decimal,
    tie_breaker: int,
) -> int | None:
    """Pick the least-sampled feasible side, never consulting a directional signal."""
    feasible = [
        side for side, bid in bids.items()
        if Decimal("0") < bid < Decimal("1")
        and take_profit_price(entry_price=bid, target_return_pct=target_return_pct, maker_close_fee_rate=maker_close_fee_rate) is not None
        and whole_share_size(bid) > 0
    ]
    if not feasible:
        return None
    # The outcome id (or other durable integer) only resolves an equal-count
    # tie.  This alternates expected sampling over recurring market instances.
    return min(feasible, key=lambda side: (int(maker_fill_counts.get(side, 0)), (side - tie_breaker) % 2))
