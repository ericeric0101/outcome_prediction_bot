"""Conservative entry-size ceiling from currently visible exit liquidity.

This is not a forecast of future liquidity.  It applies explicit depth
haircuts to the current L2 book and can only reduce a requested position.
Disabled is the default; no strategy threshold is changed by importing it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class OutcomeStressExitabilityPolicy:
    enabled: bool = False
    depth_haircut_10pct: Decimal = Decimal("0.60")
    depth_haircut_15pct: Decimal = Decimal("0.45")
    version: str = "stress_exitability_v1"

    @classmethod
    def from_env(cls) -> "OutcomeStressExitabilityPolicy":
        enabled = os.environ.get("OUTCOME_STRESS_EXITABILITY_ENABLED", "0").strip() == "1"
        try:
            h10 = Decimal(os.environ.get("OUTCOME_STRESS_EXITABILITY_DEPTH_HAIRCUT_10PCT", "0.60"))
            h15 = Decimal(os.environ.get("OUTCOME_STRESS_EXITABILITY_DEPTH_HAIRCUT_15PCT", "0.45"))
        except Exception:
            # Invalid safety configuration is fail-closed once the feature is enabled.
            h10, h15 = Decimal("0"), Decimal("0")
        return cls(enabled=enabled, depth_haircut_10pct=h10, depth_haircut_15pct=h15)


@dataclass(frozen=True)
class OutcomeStressExitabilityDecision:
    stress_safe_shares: Decimal
    allowed: bool
    reason: str
    audit: dict[str, object]


def _level_size(level: object) -> Decimal | None:
    try:
        row = level if isinstance(level, dict) else {}
        size = Decimal(str(row.get("size", row.get("sz", "0"))))
        return size if size >= 0 else None
    except Exception:
        return None


class OutcomeStressExitabilitySizer:
    """Calculate the conservative minimum across stress depth scenarios."""

    def __init__(self, policy: OutcomeStressExitabilityPolicy | None = None) -> None:
        self.policy = policy or OutcomeStressExitabilityPolicy.from_env()

    def evaluate(self, *, bid_levels: object, desired_shares: Decimal, venue_minimum_shares: Decimal) -> OutcomeStressExitabilityDecision:
        if not self.policy.enabled:
            return OutcomeStressExitabilityDecision(
                desired_shares, True, "stress_exitability_disabled",
                {"policy_version": self.policy.version, "enabled": False, "desired_shares": str(desired_shares)},
            )
        if desired_shares <= 0 or venue_minimum_shares <= 0:
            return OutcomeStressExitabilityDecision(Decimal("0"), False, "invalid_stress_sizing_input", {})
        try:
            levels = list(bid_levels)  # type: ignore[arg-type]
        except TypeError:
            levels = []
        raw = sum((value for value in (_level_size(level) for level in levels) if value is not None), Decimal("0"))
        h10, h15 = self.policy.depth_haircut_10pct, self.policy.depth_haircut_15pct
        if not (Decimal("0") < h15 <= h10 <= Decimal("1")):
            return OutcomeStressExitabilityDecision(Decimal("0"), False, "invalid_stress_depth_haircut", {
                "policy_version": self.policy.version, "enabled": True,
            })
        safe = min(desired_shares, raw * h10, raw * h15).to_integral_value(rounding="ROUND_FLOOR")
        allowed = safe >= venue_minimum_shares
        return OutcomeStressExitabilityDecision(
            safe, allowed, "stress_exitability_pass" if allowed else "stress_exitability_below_venue_minimum",
            {
                "policy_version": self.policy.version, "enabled": True,
                "desired_shares": str(desired_shares), "visible_bid_depth_shares": str(raw),
                "stress_safe_shares_10pct": str((raw * h10).to_integral_value(rounding="ROUND_FLOOR")),
                "stress_safe_shares_15pct": str((raw * h15).to_integral_value(rounding="ROUND_FLOOR")),
                "stress_safe_shares": str(safe), "depth_haircut_10pct": str(h10),
                "depth_haircut_15pct": str(h15),
            },
        )
