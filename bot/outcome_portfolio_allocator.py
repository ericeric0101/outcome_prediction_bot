"""E6: pure shadow allocator for independent Outcome opportunities."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class OutcomePortfolioCandidate:
    candidate_id: str
    outcome_id: int
    coin: str
    correlation_group: str
    lower_edge_per_share: Decimal
    expected_holding_sec: int
    requested_notional: Decimal
    safe_capacity_notional: Decimal
    existing_market_exposure: Decimal = Decimal("0")


@dataclass(frozen=True)
class OutcomeAllocation:
    candidate_id: str
    outcome_id: int
    coin: str
    allocated_notional: Decimal
    score: Decimal
    reason: str


class OutcomePortfolioAllocator:
    """Allocate a global research budget without wallet/order authority.

    Correlated contracts share a code-owned group cap.  The allocator never
    splits a rejected remainder into follow-up orders and never submits.
    """

    def __init__(
        self, *, global_notional_cap: Decimal, per_market_cap: Decimal,
        correlation_group_cap: Decimal, max_active_markets: int, minimum_notional: Decimal = Decimal("10"),
    ) -> None:
        if min(global_notional_cap, per_market_cap, correlation_group_cap, minimum_notional) <= 0:
            raise ValueError("allocator notional limits must be positive")
        if max_active_markets <= 0:
            raise ValueError("max_active_markets must be positive")
        self.global_cap = global_notional_cap
        self.market_cap = per_market_cap
        self.group_cap = correlation_group_cap
        self.max_active = max_active_markets
        self.minimum = minimum_notional

    @staticmethod
    def _score(candidate: OutcomePortfolioCandidate) -> Decimal:
        hours = Decimal(max(1, candidate.expected_holding_sec)) / Decimal("3600")
        return candidate.lower_edge_per_share / hours

    def allocate(
        self, candidates: tuple[OutcomePortfolioCandidate, ...], *,
        existing_total_exposure: Decimal = Decimal("0"),
        existing_group_exposure: dict[str, Decimal] | None = None,
    ) -> tuple[OutcomeAllocation, ...]:
        remaining = max(Decimal("0"), self.global_cap - existing_total_exposure)
        group_used = dict(existing_group_exposure or {})
        selected: list[OutcomeAllocation] = []
        seen_markets: set[int] = set()
        ordered = sorted(candidates, key=lambda item: (self._score(item), item.candidate_id), reverse=True)
        for candidate in ordered:
            score = self._score(candidate)
            if candidate.outcome_id in seen_markets:
                selected.append(OutcomeAllocation(candidate.candidate_id, candidate.outcome_id, candidate.coin, Decimal("0"), score, "duplicate_market"))
                continue
            if candidate.lower_edge_per_share <= 0 or candidate.expected_holding_sec <= 0:
                selected.append(OutcomeAllocation(candidate.candidate_id, candidate.outcome_id, candidate.coin, Decimal("0"), score, "non_positive_capital_time_edge"))
                continue
            if len(seen_markets) >= self.max_active:
                selected.append(OutcomeAllocation(candidate.candidate_id, candidate.outcome_id, candidate.coin, Decimal("0"), score, "active_market_limit"))
                continue
            group_remaining = max(Decimal("0"), self.group_cap - group_used.get(candidate.correlation_group, Decimal("0")))
            market_remaining = max(Decimal("0"), self.market_cap - candidate.existing_market_exposure)
            allocation = min(remaining, group_remaining, market_remaining, candidate.requested_notional, candidate.safe_capacity_notional)
            if allocation < self.minimum:
                selected.append(OutcomeAllocation(candidate.candidate_id, candidate.outcome_id, candidate.coin, Decimal("0"), score, "capacity_below_minimum"))
                continue
            selected.append(OutcomeAllocation(candidate.candidate_id, candidate.outcome_id, candidate.coin, allocation, score, "shadow_allocated"))
            remaining -= allocation
            group_used[candidate.correlation_group] = group_used.get(candidate.correlation_group, Decimal("0")) + allocation
            seen_markets.add(candidate.outcome_id)
        return tuple(selected)
