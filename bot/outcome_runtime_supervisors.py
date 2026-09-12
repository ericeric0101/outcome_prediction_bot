"""Narrow orchestration components for the Outcome live runtime.

These supervisors own ordering, not exchange primitives.  The runtime remains
the port that exposes already-tested atomic operations; concentrating the
ordering here makes safety precedence independently testable and prevents
research, holding and entry concerns from growing one monolithic tick method.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_live_strategy import OutcomeLiveStrategyConfig


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
        result = runtime._cancel_filled_entry_and_place_protection(
            market=snapshot.market, finding=finding,
        )
        if result is not None:
            return result

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

        result = runtime._maybe_fast_failure_exit(market=snapshot.market, finding=finding)
        if result is not None:
            return result
        result = runtime._maybe_emergency_exit(market=snapshot.market, finding=finding)
        if result is not None:
            return result
        if not snapshot.reduce_only:
            return runtime._maybe_requote_entry_buy(
                market=snapshot.market, finding=finding,
                entry_side_index=snapshot.entry_side_index,
                entry_reason=snapshot.entry_reason, config=config,
            )
        return None

    def manage_after_stream_gate(self, runtime: Any, *, snapshot: OutcomeRuntimeTickSnapshot) -> Any | None:
        if len(snapshot.active) != 1:
            return None
        finding = snapshot.active[0]
        result = runtime._maybe_requote_p3_exit(market=snapshot.market, finding=finding)
        if result is not None:
            return result
        return runtime._advance_persisted_p3_exit(market=snapshot.market, finding=finding)


class OutcomeEntrySupervisor:
    """Own fail-closed barriers before any new entry construction."""

    def preflight(
        self, runtime: Any, *, snapshot: OutcomeRuntimeTickSnapshot,
        admission: dict[str, object], config: OutcomeLiveStrategyConfig,
    ) -> Any | None:
        if not bool(getattr(snapshot.report, "safe_for_new_entry", False)):
            return runtime._result("blocked", f"account recovery blocked live strategy: {getattr(snapshot.report, 'reason', 'unknown')}")
        if snapshot.active:
            admission["account_gate"] = "existing_outcome_inventory_or_order"
            return runtime._result("blocked", "live strategy has existing Outcome inventory or order")

        store = runtime.entry_lifecycle_store
        if store is not None:
            pending = store.pending_ambiguous_submit(
                wallet=runtime.recovery.wallet, outcome_id=snapshot.market.outcome_id,
            )
            if pending is not None:
                try:
                    open_orders = runtime.recovery.account.get_open_orders_sync(runtime.recovery.wallet)
                    adopted = None
                    for coin in (snapshot.market.yes_coin, snapshot.market.no_coin):
                        candidate = store.recover_or_adopt_audited_submit(
                            wallet=runtime.recovery.wallet, outcome_id=snapshot.market.outcome_id,
                            coin=coin, open_orders=open_orders,
                        )
                        if candidate is not None:
                            adopted = candidate
                            break
                except Exception:
                    adopted = None
                admission["ambiguous_submit_fence"] = {
                    "blocked": True, "intent_id": pending.get("intent_id"),
                    "recovered_order_id": adopted.order_id if adopted is not None else None,
                }
                if adopted is not None:
                    return runtime._result(
                        "blocked", "ambiguous prior entry adopted; refreshing account truth before any new action",
                        adopted.order_id,
                    )
                return runtime._result(
                    "blocked", "ambiguous prior entry submission; reconciliation required before any new entry",
                )
        if snapshot.reduce_only:
            admission["reduce_only_gate"] = "new_entries_prohibited"
            return runtime._result("flat", "reduce-only: no live exposure after entry cancellation")
        if snapshot.entry_side_index not in (0, 1):
            admission["signal_gate"] = "no_directional_signal"
            return runtime._result("flat", f"live strategy no entry: {snapshot.entry_reason}")

        admission["selected_side_index"] = snapshot.entry_side_index
        admission["selected_coin"] = runtime.machine.gateway.outcome_coin(snapshot.market, snapshot.entry_side_index)
        if store is not None:
            cooldown = store.fast_rebook_cooldown_remaining(
                wallet=runtime.recovery.wallet, outcome_id=snapshot.market.outcome_id,
                coin=str(admission["selected_coin"]),
                cooldown_sec=runtime.entry_planner.config.fast_rebook_cooldown_sec,
            )
            admission["entry_fast_rebook_cooldown_remaining_sec"] = round(cooldown, 3)
            if cooldown > 0:
                return runtime._result(
                    "flat", f"live strategy no entry: fast_risk_rebook_cooldown ({cooldown:.1f}s remaining)",
                )
        return None
