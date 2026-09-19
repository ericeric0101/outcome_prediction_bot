"""Stable value types shared by Outcome runtime orchestration services."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec


@dataclass(frozen=True)
class LiveExecutionResult:
    state: str
    detail: str
    order_id: str | None = None


@dataclass(frozen=True)
class OutcomeRuntimeTickSnapshot:
    """Immutable authoritative facts shared by one strategy decision."""

    market: OutcomeMarketSpec
    report: Any
    active: tuple[Any, ...]
    pending_owned_entry: bool
    entry_side_index: int | None
    entry_reason: str
    reduce_only: bool
    observed_monotonic: float
    # Immutable caller-provided, as-of market context. It is observational
    # only: execution services must not treat it as mutation truth.
    market_context: dict[str, object] | None = None
    # Timestamp at which the upstream S0 gate evaluated this candidate.  It
    # lets admission reject a decision that began before a just-completed
    # owned exit, rather than treating a queued pre-exit signal as new risk.
    entry_decision_at_ms: int | None = None
