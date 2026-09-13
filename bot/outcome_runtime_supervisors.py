"""Narrow orchestration components for the Outcome live runtime.

These supervisors own ordering, not exchange primitives.  The runtime remains
the port that exposes already-tested atomic operations; concentrating the
ordering here makes safety precedence independently testable and prevents
research, holding and entry concerns from growing one monolithic tick method.
"""
from __future__ import annotations

from typing import Any

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_live_strategy import OutcomeLiveStrategyConfig
from bot.outcome_runtime_types import OutcomeRuntimeTickSnapshot


class OutcomeResearchSupervisor:
    """Run mutation-free observers without letting them own control flow."""

    def observe_entry(
        self, runtime: Any, *, market: OutcomeMarketSpec, entry_side_index: int | None,
        entry_reason: str, entry_evidence: dict[str, object],
        market_context: dict[str, object] | None, admission: dict[str, object],
    ) -> None:
        admission["market_regime_shadow"] = runtime._observe_market_regime_shadow(
            market=market, entry_side_index=entry_side_index,
            entry_evidence=entry_evidence, market_context=market_context,
        )
        admission["confidence_entry_shadow"] = runtime._observe_confidence_entry_shadow(
            market=market, market_context=market_context,
        )
        admission["active_challenger_shadow"] = runtime._observe_active_challenger_shadow(
            market=market, market_context=market_context,
            production_side_index=entry_side_index, production_reason=entry_reason,
            regime=(admission["market_regime_shadow"]
                    if isinstance(admission.get("market_regime_shadow"), dict) else None),
        )
        runtime._capture_trend_continuation_path(
            market=market, entry_evidence=entry_evidence, market_context=market_context,
        )


class OutcomeHoldingSupervisor:
    """Preserve the protective-sell → observation → bounded-exit ordering."""

    def __init__(self) -> None:
        self._last_reversal_observation_at: dict[tuple[int, str], float] = {}
        self._last_path_capture_at: dict[tuple[int, str], float] = {}

    def manage_before_stream_gate(
        self, runtime: Any, *, snapshot: OutcomeRuntimeTickSnapshot,
        config: OutcomeLiveStrategyConfig,
    ) -> Any | None:
        if len(snapshot.active) != 1:
            return None
        finding = snapshot.active[0]
        result = runtime.holding_execution_service.protect_after_fill(
            market=snapshot.market, finding=finding,
        )
        if result is not None:
            return result
        # Entry preflight is not reached while inventory exists.  Preserve a
        # separate durable alarm for that operationally more urgent case.
        runtime.audit_safety_for_existing_holding(outcome_id=snapshot.market.outcome_id)

        # All research follows protection.  None of these observers can
        # authorize a mutation.
        runtime._observe_toxic_fill_shadow(market=snapshot.market, finding=finding)
        holding_key = (snapshot.market.outcome_id, str(getattr(finding, "coin", "")))
        now = snapshot.observed_monotonic
        reversal_observed = False
        if now - self._last_reversal_observation_at.get(holding_key, float("-inf")) >= runtime._REVERSAL_RISK_MIN_INTERVAL_SEC:
            reversal_observed = runtime._observe_holding_reversal_ws(market=snapshot.market, finding=finding)
            if reversal_observed:
                self._last_reversal_observation_at[holding_key] = now
        if now - self._last_path_capture_at.get(holding_key, float("-inf")) >= runtime._HOLDING_PATH_MIN_INTERVAL_SEC:
            runtime._capture_holding_path(
                market=snapshot.market, finding=finding, update_reversal=not reversal_observed,
            )
            self._last_path_capture_at[holding_key] = now

        result = runtime.holding_risk_service.maybe_fast_failure(market=snapshot.market, finding=finding)
        if result is not None:
            return result
        result = runtime.holding_risk_service.maybe_emergency(market=snapshot.market, finding=finding)
        if result is not None:
            return result
        if not snapshot.reduce_only:
            return runtime.entry_execution_service.manage_resting_buy(
                snapshot=snapshot, config=config,
            )
        return None

    def manage_after_stream_gate(self, runtime: Any, *, snapshot: OutcomeRuntimeTickSnapshot) -> Any | None:
        if len(snapshot.active) != 1:
            return None
        finding = snapshot.active[0]
        result = runtime.exit_requote_service.maybe_requote(market=snapshot.market, finding=finding)
        if result is not None:
            return result
        return runtime.holding_execution_service.advance_persisted_exit(
            market=snapshot.market, finding=finding,
        )


class OutcomeEntrySupervisor:
    """Own fail-closed barriers before any new entry construction."""

    def preflight(
        self, runtime: Any, *, snapshot: OutcomeRuntimeTickSnapshot,
        admission: dict[str, object], config: OutcomeLiveStrategyConfig,
    ) -> Any | None:
        return runtime.entry_execution_service.preflight(
            snapshot=snapshot, admission=admission, config=config,
        )
