"""P2 read-only complete-set parity calculations for default-binary markets."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _walk(levels: list[Mapping[str, Any]], shares: Decimal) -> Optional[Decimal]:
    """Return total executable USDC for shares, or None if depth is insufficient."""
    remaining, total = shares, Decimal("0")
    for level in levels:
        if remaining <= 0:
            break
        available = _decimal(level["sz"])
        taken = min(remaining, available)
        total += taken * _decimal(level["px"])
        remaining -= taken
    return total if remaining <= 0 else None


@dataclass(frozen=True)
class OutcomeParitySnapshot:
    outcome_id: int
    requested_shares: Decimal
    buy_complete_set_cost: Optional[Decimal]
    buy_complete_set_edge: Optional[Decimal]
    sell_complete_set_proceeds: Optional[Decimal]
    sell_complete_set_edge: Optional[Decimal]
    fee_status: str = "unverified_excluded"
    fee_rate: Optional[Decimal] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "requested_shares": self.requested_shares,
            "buy_complete_set_cost": self.buy_complete_set_cost,
            "buy_complete_set_edge": self.buy_complete_set_edge,
            "sell_complete_set_proceeds": self.sell_complete_set_proceeds,
            "sell_complete_set_edge": self.sell_complete_set_edge,
            "fee_status": self.fee_status,
            "fee_rate": self.fee_rate,
            "research_only": True,
            "conversion_submission_disabled": True,
        }


class OutcomeParityAnalyzer:
    """Calculate executable two-leg prices; it has no venue client dependency."""

    def __init__(self, requested_shares: Decimal = Decimal("10"), fee_rate: Optional[Decimal] = None) -> None:
        if requested_shares <= 0:
            raise ValueError("requested_shares must be positive")
        self.requested_shares = requested_shares
        self.fee_rate = fee_rate

    def analyze(
        self,
        market: OutcomeMarketSpec,
        yes_book: Mapping[str, Any],
        no_book: Mapping[str, Any],
    ) -> OutcomeParitySnapshot:
        yes_levels = yes_book.get("levels", [[], []])
        no_levels = no_book.get("levels", [[], []])
        yes_bids, yes_asks = yes_levels[0], yes_levels[1]
        no_bids, no_asks = no_levels[0], no_levels[1]
        buy_cost_yes = _walk(yes_asks, self.requested_shares)
        buy_cost_no = _walk(no_asks, self.requested_shares)
        sell_yes = _walk(yes_bids, self.requested_shares)
        sell_no = _walk(no_bids, self.requested_shares)
        buy_cost = (buy_cost_yes + buy_cost_no) if buy_cost_yes is not None and buy_cost_no is not None else None
        sell_proceeds = (sell_yes + sell_no) if sell_yes is not None and sell_no is not None else None
        return OutcomeParitySnapshot(
            outcome_id=market.outcome_id,
            requested_shares=self.requested_shares,
            buy_complete_set_cost=buy_cost,
            buy_complete_set_edge=(self.requested_shares - buy_cost) if buy_cost is not None else None,
            sell_complete_set_proceeds=sell_proceeds,
            sell_complete_set_edge=(sell_proceeds - self.requested_shares) if sell_proceeds is not None else None,
            fee_status="verified_included" if self.fee_rate is not None else "unverified_excluded",
            fee_rate=self.fee_rate,
        )
