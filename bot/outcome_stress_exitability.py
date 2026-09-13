"""Conservative entry-size ceiling from price-capped visible exit liquidity.

This is not a forecast. For explicit -10%/-15% loss scenarios it counts only
current valid L2 bids at or above each stressed price floor, then applies a
separate depth haircut. It can only reduce a requested position.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from bot.outcome_emergency_exit import executable_shares_at_or_above, parse_bid_levels


@dataclass(frozen=True)
class OutcomeStressExitabilityPolicy:
    enabled: bool = False
    depth_haircut_10pct: Decimal = Decimal("0.60")
    depth_haircut_15pct: Decimal = Decimal("0.45")
    spread_widening_bps: Decimal = Decimal("0")
    version: str = "stress_exitability_v2_price_capped"

    @classmethod
    def from_env(cls) -> "OutcomeStressExitabilityPolicy":
        enabled = os.environ.get("OUTCOME_STRESS_EXITABILITY_ENABLED", "0").strip() == "1"
        try:
            return cls(
                enabled=enabled,
                depth_haircut_10pct=Decimal(os.environ.get("OUTCOME_STRESS_EXITABILITY_DEPTH_HAIRCUT_10PCT", "0.60")),
                depth_haircut_15pct=Decimal(os.environ.get("OUTCOME_STRESS_EXITABILITY_DEPTH_HAIRCUT_15PCT", "0.45")),
                spread_widening_bps=Decimal(os.environ.get("OUTCOME_STRESS_EXITABILITY_SPREAD_WIDENING_BPS", "0")),
            )
        except Exception:
            return cls(enabled=enabled, depth_haircut_10pct=Decimal("0"), depth_haircut_15pct=Decimal("0"), spread_widening_bps=Decimal("-1"))


@dataclass(frozen=True)
class OutcomeStressExitabilityDecision:
    stress_safe_shares: Decimal
    allowed: bool
    reason: str
    audit: dict[str, object]


class OutcomeStressExitabilitySizer:
    """Price-capped full-depth stress capacity; no exchange mutation."""

    def __init__(self, policy: OutcomeStressExitabilityPolicy | None = None) -> None:
        self.policy = policy or OutcomeStressExitabilityPolicy.from_env()

    def evaluate(
        self, *, bid_levels: object, desired_shares: Decimal, venue_minimum_shares: Decimal,
        entry_price: Decimal | None = None,
    ) -> OutcomeStressExitabilityDecision:
        if not self.policy.enabled:
            return OutcomeStressExitabilityDecision(
                desired_shares, True, "stress_exitability_disabled",
                {"policy_version": self.policy.version, "enabled": False, "desired_shares": str(desired_shares)},
            )
        if desired_shares <= 0 or venue_minimum_shares <= 0 or entry_price is None or not Decimal("0") < entry_price < Decimal("1"):
            return OutcomeStressExitabilityDecision(Decimal("0"), False, "invalid_stress_sizing_input", {})
        h10, h15, widening = self.policy.depth_haircut_10pct, self.policy.depth_haircut_15pct, self.policy.spread_widening_bps
        if not (Decimal("0") < h15 <= h10 <= Decimal("1")) or not (Decimal("0") <= widening < Decimal("10000")):
            return OutcomeStressExitabilityDecision(Decimal("0"), False, "invalid_stress_exitability_policy", {
                "policy_version": self.policy.version, "enabled": True,
            })
        # A positive widening buffer raises the required bid, deliberately
        # reducing capacity rather than pretending a future wider spread helps.
        buffer = entry_price * widening / Decimal("10000")
        price_10 = entry_price * Decimal("0.90") + buffer
        price_15 = entry_price * Decimal("0.85") + buffer
        bids = parse_bid_levels({"bids": bid_levels})
        if bids is None:
            return OutcomeStressExitabilityDecision(Decimal("0"), False, "missing_or_invalid_full_depth_bids", {
                "policy_version": self.policy.version, "enabled": True,
            })
        raw10 = executable_shares_at_or_above(bids, minimum_price=price_10)
        raw15 = executable_shares_at_or_above(bids, minimum_price=price_15)
        safe10 = (raw10 * h10).to_integral_value(rounding=ROUND_FLOOR)
        safe15 = (raw15 * h15).to_integral_value(rounding=ROUND_FLOOR)
        safe = min(desired_shares, safe10, safe15)
        allowed = safe >= venue_minimum_shares
        return OutcomeStressExitabilityDecision(
            safe, allowed, "stress_exitability_pass" if allowed else "stress_exitability_below_venue_minimum",
            {
                "policy_version": self.policy.version, "enabled": True, "desired_shares": str(desired_shares),
                "entry_price": str(entry_price), "stress_price_floor_10pct": str(price_10),
                "stress_price_floor_15pct": str(price_15), "spread_widening_bps": str(widening),
                "full_depth_shares_at_10pct_floor": str(raw10), "full_depth_shares_at_15pct_floor": str(raw15),
                "stress_safe_shares_10pct": str(safe10), "stress_safe_shares_15pct": str(safe15),
                "stress_safe_shares": str(safe), "depth_haircut_10pct": str(h10), "depth_haircut_15pct": str(h15),
            },
        )
