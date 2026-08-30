"""Durable-in-process coordination for a daily Outcome market rollover.

Market selection is intentionally separate from account ownership.  At the
daily boundary the new contract may already be tradable while the old one is
still awaiting official settlement.  This coordinator keeps the old contract
in the account-reconciliation set and therefore prevents accidental overlap.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec


@dataclass
class OutcomeRolloverCoordinator:
    """Track the selected market plus recently expired, retiring siblings."""

    retention_seconds: int = 6 * 60 * 60
    _previous: OutcomeMarketSpec | None = None
    _retiring: dict[int, OutcomeMarketSpec] = field(default_factory=dict)

    def observe(
        self,
        *,
        selected: OutcomeMarketSpec,
        discovered: Iterable[OutcomeMarketSpec],
        now: int | None = None,
    ) -> tuple[OutcomeMarketSpec, ...]:
        timestamp = int(time.time()) if now is None else int(now)
        if self._previous and self._previous.outcome_id != selected.outcome_id:
            self._retiring[self._previous.outcome_id] = self._previous

        # This also recovers the retiring daily instance after a process
        # restart, provided Outcome still exposes it in outcomeMeta.
        for market in discovered:
            if market.outcome_id == selected.outcome_id or market.period != selected.period:
                continue
            if market.expiry_timestamp <= timestamp < market.expiry_timestamp + self.retention_seconds:
                self._retiring[market.outcome_id] = market

        self._previous = selected
        stale = [market_id for market_id, market in self._retiring.items()
                 if timestamp >= market.expiry_timestamp + self.retention_seconds]
        for market_id in stale:
            self._retiring.pop(market_id, None)
        return self.retiring_markets

    @property
    def retiring_markets(self) -> tuple[OutcomeMarketSpec, ...]:
        return tuple(sorted(self._retiring.values(), key=lambda item: item.expiry_timestamp))

    def mark_settled(self, outcome_id: int) -> None:
        self._retiring.pop(int(outcome_id), None)

    def reconciliation_markets(self, selected: OutcomeMarketSpec) -> tuple[OutcomeMarketSpec, ...]:
        return (selected, *self.retiring_markets)
