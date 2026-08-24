"""Venue-side pre-trade risk gates independent of the strategy signal."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable


@dataclass(frozen=True)
class OutcomeRiskLimits:
    # $10 is a venue floor, but whole-share rounding can make a minimum order
    # approach $11 when price is near $1.
    max_entry_notional_usdc: Decimal = Decimal("11")
    max_total_outcome_exposure_usdc: Decimal = Decimal("11")
    max_open_orders: int = 1
    collateral_coins: tuple[str, ...] = ("USDH", "USDC")


@dataclass(frozen=True)
class OutcomeRiskDecision:
    allowed: bool
    reason: str
    entry_notional: Decimal
    available_collateral: Decimal
    current_exposure: Decimal


class OutcomePreTradeRiskGate:
    """Conservative account checks before a new Outcome entry only."""

    def __init__(self, limits: OutcomeRiskLimits = OutcomeRiskLimits()) -> None:
        self.limits = limits

    @staticmethod
    def _decimal(row: dict[str, Any], key: str) -> Decimal:
        return Decimal(str(row.get(key, "0")))

    def evaluate(self, *, balances: Iterable[dict[str, Any]], open_orders: Iterable[dict[str, Any]], price: Decimal, shares: int) -> OutcomeRiskDecision:
        entry = price * Decimal(shares)
        balance_rows = list(balances)
        orders = list(open_orders)
        available = sum(
            (self._decimal(row, "total") - self._decimal(row, "hold"))
            for row in balance_rows if str(row.get("coin")) in self.limits.collateral_coins
        )
        # Outcome shares settle to at most one quote unit, so valuing each
        # outstanding share at $1 is deliberately conservative.
        exposure = sum(self._decimal(row, "total") for row in balance_rows if str(row.get("coin", "")).startswith("#"))
        if entry <= 0:
            return OutcomeRiskDecision(False, "non_positive_entry_notional", entry, available, exposure)
        if entry > self.limits.max_entry_notional_usdc:
            return OutcomeRiskDecision(False, "entry_notional_cap", entry, available, exposure)
        if available < entry:
            return OutcomeRiskDecision(False, "insufficient_available_collateral", entry, available, exposure)
        if len(orders) >= self.limits.max_open_orders:
            return OutcomeRiskDecision(False, "open_order_cap", entry, available, exposure)
        if exposure + entry > self.limits.max_total_outcome_exposure_usdc:
            return OutcomeRiskDecision(False, "outcome_exposure_cap", entry, available, exposure)
        return OutcomeRiskDecision(True, "approved", entry, available, exposure)
