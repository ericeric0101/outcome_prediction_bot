"""Single official execution runtime invoked by a strategy loop.

It is deliberately disabled unless the operator enables all execution gates.
It makes the account-recovery report a precondition for each state-machine
tick, so restart behaviour never depends on stale local order IDs.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

from bot.adapters.outcome_client import OutcomeClient
from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_account_recovery import OutcomeAccountRecovery
from bot.outcome_account_read_cache import OutcomeAccountReadCache
from bot.outcome_coin import normalize_outcome_coin
from bot.outcome_execution_gateway import OutcomeExecutionGateway, whole_share_size
from bot.outcome_maker_state_machine import MakerTickResult, OutcomeMakerStateMachine
from bot.outcome_risk_gate import OutcomePreTradeRiskGate, OutcomeRiskLimits
from bot.outcome_stream_health import OutcomeStreamHealth
from bot.outcome_execution_ledger import OutcomeExecutionLedger
from bot.outcome_research_gate import OutcomeResearchGate
from bot.outcome_p3_calibration import OutcomeP3CalibrationConfig, choose_consensus_calibration_side, take_profit_price
from bot.outcome_live_strategy import OutcomeLiveStrategyConfig
from bot.outcome_exit_target_policy import OutcomeExitTargetPolicy
from bot.outcome_exit_lifecycle import OutcomeExitLifecycle, OutcomeExitLifecycleStore
from bot.outcome_exit_quote_planner import OutcomeExitQuotePlanner, OutcomeExitQuotePlannerConfig
from bot.outcome_exit_requote_controller import OutcomeExitRequoteController
from bot.outcome_entry_lifecycle import OutcomeEntryLifecycle, OutcomeEntryLifecycleStore
from bot.outcome_entry_requote import (
    OutcomeEntryFastRiskTracker,
    OutcomeEntryQuotePlanner,
    OutcomeEntryQuotePlannerConfig,
    OutcomeEntryRequoteController,
)
from bot.outcome_holding_path import OutcomeHoldingPathObservation, OutcomeHoldingPathRecorder
from bot.outcome_exit_continuation import OutcomeExitContinuationObserver
from bot.outcome_trend_continuation import OutcomeTrendContinuationRecorder
from bot.outcome_reversal import OutcomeReversalClassifier, OutcomeReversalInput
from bot.outcome_loss_reentry import OutcomeLossReentryGate
from bot.outcome_tier_b_execution_gate import OutcomeTierBExecutionGate
from bot.outcome_spread_candidate_tracker import OutcomeWideSpreadCandidateTracker
from bot.outcome_portfolio_guard import OutcomePortfolioGuard
from bot.outcome_market_regime import (
    OutcomeMarketRegimeInput,
    OutcomeMarketRegimeShadow,
    OutcomeToxicFillInput,
)
from bot.outcome_emergency_exit import (
    OutcomeEmergencyExitController,
    OutcomeEmergencyExitConfig,
    OutcomeEmergencyExitPolicy,
)
from bot.outcome_efficiency_shadow import OutcomeConfidenceEntryShadow, OutcomeQueueAwarePricingShadow
from bot.outcome_active_shadow import OutcomeActiveChallengerShadow
from bot.outcome_entry_quality_shadow import OutcomeEntryQualityShadow, OutcomePostFillQualityInput
from bot.outcome_crash_circuit_shadow import OutcomeCrashCircuitObservation, OutcomeCrashCircuitShadow
from bot.outcome_market_risk_monitor import OutcomeMarketRiskMonitor, OutcomeMarketRiskObservation
from bot.outcome_structural_collapse_shadow import OutcomeStructuralCollapseShadow
from bot.outcome_entry_readiness_shadow import OutcomeEntryReadinessShadow
from bot.outcome_stress_exitability import OutcomeStressExitabilitySizer
from bot.outcome_runtime_supervisors import (
    OutcomeEntrySupervisor,
    OutcomeHoldingSupervisor,
    OutcomeResearchSupervisor,
)
from bot.outcome_runtime_journal_view import OutcomeRuntimeJournalView
from bot.outcome_runtime_types import LiveExecutionResult, OutcomeRuntimeTickSnapshot
from bot.outcome_exit_recovery_service import OutcomeExitRecoveryService
from bot.outcome_entry_execution_service import OutcomeEntryExecutionService
from bot.outcome_holding_execution_service import OutcomeHoldingExecutionService
from bot.outcome_holding_risk_service import OutcomeHoldingRiskService
from bot.outcome_exit_requote_service import OutcomeExitRequoteService
from bot.outcome_runtime_safety import OutcomeRuntimeSafety, SafetyComponent
from bot.outcome_risk_episode import OutcomeRiskEpisodeStore


class OutcomeLiveExecutionRuntime:
    # Holding-path research is low-frequency evidence, not an execution
    # trigger.  Sampling it every 1.5-second strategy turn used a full REST
    # L2 request even while a managed sell was safely resting.
    _HOLDING_PATH_MIN_INTERVAL_SEC = 30.0
    _REVERSAL_RISK_MIN_INTERVAL_SEC = 5.0
    _CRASH_SHADOW_MIN_INTERVAL_SEC = 10.0
    # V1.2 storage compaction: tick density remains durable, but repeated
    # nested research/account payloads only need a full forensic snapshot on
    # a meaningful state transition or bounded heartbeat.
    _DECISION_TELEMETRY_FULL_HEARTBEAT_SEC = 300.0

    def __init__(self, *, account: OutcomeClient, wallet: str, gateway: OutcomeExecutionGateway | None = None, risk_gate: OutcomePreTradeRiskGate | None = None, stream_health: OutcomeStreamHealth | None = None, ledger: OutcomeExecutionLedger | None = None, research_gate: OutcomeResearchGate | None = None, exit_planner: OutcomeExitQuotePlanner | None = None, exit_lifecycle_store: OutcomeExitLifecycleStore | None = None, exit_requote_controller: OutcomeExitRequoteController | None = None, entry_planner: OutcomeEntryQuotePlanner | None = None, entry_lifecycle_store: OutcomeEntryLifecycleStore | None = None, entry_requote_controller: OutcomeEntryRequoteController | None = None, structural_collapse_shadow: OutcomeStructuralCollapseShadow | None = None, entry_readiness_shadow: OutcomeEntryReadinessShadow | None = None) -> None:
        self._account_reads = OutcomeAccountReadCache(account)
        self.recovery = OutcomeAccountRecovery(account=self._account_reads, wallet=wallet)
        self.machine = OutcomeMakerStateMachine(
            account=self._account_reads, gateway=gateway or OutcomeExecutionGateway(), wallet=wallet,
            journal=ledger.journal if ledger else None,
        )
        self.risk_gate = risk_gate or OutcomePreTradeRiskGate(OutcomeRiskLimits(
            max_entry_notional_usdc=Decimal(os.environ.get("OUTCOME_MAX_ENTRY_NOTIONAL_USDC", "11")),
            max_total_outcome_exposure_usdc=Decimal(os.environ.get("OUTCOME_MAX_OUTCOME_EXPOSURE_USDC", "11")),
            max_open_orders=int(os.environ.get("OUTCOME_MAX_OPEN_ORDERS", "1")),
        ))
        self.stream_health = stream_health
        self.ledger = ledger
        self.runtime_safety = OutcomeRuntimeSafety(
            journal=ledger.journal if ledger else None,
            run_id=ledger.run_id if ledger else None,
            repo_root=Path(__file__).resolve().parent.parent,
        )
        self.research_gate = research_gate or OutcomeResearchGate()
        if exit_planner is None:
            exit_planner = OutcomeExitQuotePlanner(OutcomeExitQuotePlannerConfig())
        self.exit_planner = exit_planner
        self.exit_lifecycle_store = exit_lifecycle_store or (OutcomeExitLifecycleStore(ledger.journal, ledger.run_id) if ledger else None)
        # Initial protective SELL submission happens inside the maker state
        # machine; give it the same durable intent/fence store used by exit
        # replacement and emergency controllers.
        self.machine.exit_lifecycle_store = self.exit_lifecycle_store
        self.exit_requote_controller = exit_requote_controller or (
            OutcomeExitRequoteController(account=self._account_reads, gateway=self.machine.gateway, store=self.exit_lifecycle_store, wallet=wallet)
            if self.exit_lifecycle_store else None
        )
        self.entry_planner = entry_planner or OutcomeEntryQuotePlanner(OutcomeEntryQuotePlannerConfig())
        self.entry_fast_risk_tracker = OutcomeEntryFastRiskTracker()
        self.entry_lifecycle_store = entry_lifecycle_store or (OutcomeEntryLifecycleStore(ledger.journal, ledger.run_id) if ledger else None)
        self.entry_requote_controller = entry_requote_controller or (
            OutcomeEntryRequoteController(account=self._account_reads, gateway=self.machine.gateway, store=self.entry_lifecycle_store, wallet=wallet)
            if self.entry_lifecycle_store else None
        )
        self.holding_path_recorder = OutcomeHoldingPathRecorder(ledger.journal, ledger.run_id) if ledger else None
        self.exit_continuation_observer = OutcomeExitContinuationObserver(ledger.journal, ledger.run_id) if ledger else None
        self.trend_continuation_recorder = OutcomeTrendContinuationRecorder(ledger.journal, ledger.run_id) if ledger else None
        self.reversal_classifier = OutcomeReversalClassifier()
        self.loss_reentry_gate = OutcomeLossReentryGate(ledger.journal, ledger.run_id) if ledger else None
        self.tier_b_execution_gate = OutcomeTierBExecutionGate(ledger.journal.db_path) if ledger else None
        # Disabled by default.  When explicitly enabled it can only lower the
        # current entry-size ceiling using visible bid depth haircuts.
        self.stress_exitability_sizer = OutcomeStressExitabilitySizer()
        self.wide_spread_candidate_tracker = (
            OutcomeWideSpreadCandidateTracker(journal=ledger.journal, run_id=ledger.run_id) if ledger else None
        )
        self.portfolio_guard = OutcomePortfolioGuard(ledger.journal.db_path) if ledger else None
        self.risk_episode_store = (
            OutcomeRiskEpisodeStore(ledger.journal, ledger.run_id)
            if ledger is not None and os.environ.get("OUTCOME_RISK_EPISODE_BUDGET_ENABLED", "0").strip() == "1"
            else None
        )
        # Explicitly defaults to the historical behavior.  A disabled value
        # is an operator-authorized observation mode: it suppresses only
        # loss-triggered IOC exits, never normal TP or lifecycle safety.
        self.loss_exit_enabled = os.environ.get("OUTCOME_LOSS_EXIT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
        # This is intentionally an opt-in $11 canary.  $11 accommodates the
        # venue's $10 minimum after mandatory whole-share rounding; a stale
        # copied .env cannot silently authorize a larger exposure, and the
        # lane cannot run without the shared durable episode budget.
        self.narrow_hard_failure_canary_enabled = bool(
            os.environ.get("OUTCOME_NARROW_HARD_FAILURE_CANARY_ENABLED", "0").strip() == "1"
            and self.risk_episode_store is not None
            and self.risk_gate.limits.max_entry_notional_usdc <= Decimal("11")
            and self.risk_gate.limits.max_total_outcome_exposure_usdc <= Decimal("11")
        )
        # This observer is deliberately shadow-only.  It has no reference to
        # an execution controller and cannot alter S0/S2/S3 authority.
        self.market_regime_shadow = OutcomeMarketRegimeShadow()
        self.confidence_entry_shadow = OutcomeConfidenceEntryShadow()
        self.queue_pricing_shadow = OutcomeQueueAwarePricingShadow()
        # Milestone-B challenger is journal-only.  It owns no account,
        # gateway, controller or key and cannot alter the production action.
        self.active_challenger_shadow = OutcomeActiveChallengerShadow()
        # Phase A/B adverse-selection evidence is a pure decision producer.
        # It is shared with entry supervision only as an evaluator.
        self.entry_quality_shadow = OutcomeEntryQualityShadow()
        # This records fast WS crash features for later calibration.  It is
        # deliberately separate from S3 / fast-failure authority.
        self.crash_circuit_shadow = OutcomeCrashCircuitShadow()
        self.market_risk_monitor = OutcomeMarketRiskMonitor()
        self.structural_collapse_shadow = structural_collapse_shadow
        self.entry_readiness_shadow = entry_readiness_shadow
        self.emergency_exit_policy = OutcomeEmergencyExitPolicy()
        self.emergency_exit_controller = (
            OutcomeEmergencyExitController(
                account=self._account_reads, gateway=self.machine.gateway, store=self.exit_lifecycle_store,
                wallet=wallet, policy=self.emergency_exit_policy,
            ) if self.exit_lifecycle_store else None
        )
        # Live fast-failure is a narrower, earlier sibling of S3.  It shares
        # S3's official SDK cancel-confirm-fresh-depth-IOC controller and its
        # one-shot lifecycle token, but never waits two hours for a passive
        # loss quote once the entry thesis is persistently invalidated.
        self.fast_failure_exit_policy = OutcomeEmergencyExitPolicy(OutcomeEmergencyExitConfig.fast_failure())
        self.fast_failure_exit_controller = (
            OutcomeEmergencyExitController(
                account=self._account_reads, gateway=self.machine.gateway, store=self.exit_lifecycle_store,
                wallet=wallet, policy=self.fast_failure_exit_policy,
            ) if self.exit_lifecycle_store else None
        )
        self.narrow_hard_failure_policy = OutcomeEmergencyExitPolicy(
            OutcomeEmergencyExitConfig.narrow_hard_failure_canary()
        )
        self.narrow_hard_failure_controller = (
            OutcomeEmergencyExitController(
                account=self._account_reads, gateway=self.machine.gateway, store=self.exit_lifecycle_store,
                wallet=wallet, policy=self.narrow_hard_failure_policy,
            ) if self.narrow_hard_failure_canary_enabled and self.exit_lifecycle_store else None
        )
        self._holding_context: dict[int, dict[str, object]] = {}
        self._opposite_observation_counts: dict[tuple[int, str], int] = {}
        # Emergency S3 requires three independently spaced confirmed samples.
        # This state is intentionally reset on restart, which is conservative:
        # a new process must observe the persistent reversal again.
        self._emergency_reversal_windows: dict[tuple[int, str], tuple[float, float, int]] = {}
        # E5 is deliberately process-local.  A restart must never resume a
        # canary against an old position without a new explicit operator run.
        self._e5_canary_eligible_order_ids: set[str] = set()
        self._tick_books: dict[tuple[int, int], dict[str, object]] = {}
        # Cache only immutable, exact-fill provenance.  A missing/ambiguous
        # lookup is intentionally retried on a later observation rather than
        # cached as a fabricated lifecycle identity.
        self._last_regime_shadow_record: dict[int, tuple[str, float]] = {}
        self._latest_regime_shadow: dict[int, dict[str, object]] = {}
        self._last_toxic_shadow_record: dict[tuple[int, str], tuple[str, float]] = {}
        self._last_efficiency_shadow_record: dict[tuple[str, int, int], tuple[str, float]] = {}
        self._last_crash_shadow_record: dict[str, tuple[str, float]] = {}
        self._last_market_risk_record: dict[str, tuple[str, float]] = {}
        self._last_structural_collapse_shadow_record: dict[str, tuple[str, float]] = {}
        self._last_entry_readiness_shadow_record: dict[tuple[int, int], tuple[str, float]] = {}
        self._last_fast_failure_lane_record: dict[str, tuple[str, float]] = {}
        self._last_holding_risk_decision_record: dict[str, tuple[str, float]] = {}
        self._last_postfill_quality_record: dict[str, tuple[str, float]] = {}
        self._last_fill_sync_at = float("-inf")
        # An unresolved, cancelled BUY is unusual.  Its terminal proof needs
        # fresh REST account truth, but a venue/read failure must not turn the
        # normal strategy tick into a retry storm.
        self._last_absent_entry_terminal_reconcile_at: dict[tuple[int, str, str], float] = {}
        self.journal_view = OutcomeRuntimeJournalView(ledger.journal.db_path if ledger else None)
        # These narrow services own decision ordering.  Atomic execution and
        # recovery primitives remain injected above and keep their existing
        # independently tested contracts.
        self.research_supervisor = OutcomeResearchSupervisor()
        self.holding_supervisor = OutcomeHoldingSupervisor()
        self.entry_supervisor = OutcomeEntrySupervisor()
        self.entry_execution_service = OutcomeEntryExecutionService(
            recovery=self.recovery, gateway=self.machine.gateway, machine=self.machine,
            store=self.entry_lifecycle_store, planner=self.entry_planner,
            exit_store=self.exit_lifecycle_store,
            controller=self.entry_requote_controller, ledger=self.ledger,
            loss_reentry_gate=self.loss_reentry_gate, record_result=self._record,
            fast_risk_decision=self._entry_fast_risk_decision,
            safety_preflight=self.safety_ready_for_new_entry,
            entry_quality_shadow=self.entry_quality_shadow,
        )
        self.exit_recovery_service = OutcomeExitRecoveryService(
            recovery=self.recovery, store=self.exit_lifecycle_store,
            loss_reentry_gate=self.loss_reentry_gate,
        )
        self.holding_execution_service = OutcomeHoldingExecutionService(
            recovery=self.recovery, machine=self.machine,
            entry_store=self.entry_lifecycle_store,
            entry_controller=self.entry_requote_controller, ledger=self.ledger,
            persisted_policy=self._persisted_p3_exit_policy,
            persisted_maker_fee=self._persisted_p3_maker_fee,
            strategy_exit_tier=self._strategy_exit_tier,
            exit_requote_enabled=self.exit_requote_enabled,
            record_result=self._record,
        )
        self.holding_risk_service = OutcomeHoldingRiskService(
            recovery=self.recovery, machine=self.machine,
            store=self.exit_lifecycle_store, ledger=self.ledger,
            fast_policy=self.fast_failure_exit_policy,
            fast_controller=self.fast_failure_exit_controller,
            emergency_policy=self.emergency_exit_policy,
            emergency_controller=self.emergency_exit_controller,
            reversal_windows=self._emergency_reversal_windows,
            fresh_book=self._fresh_book_once,
            official_holding_age=self._official_holding_age_sec,
            live_entry_age=self._live_entry_age_sec,
            gate_audit=self._audit_exit_safety_gate,
            risk_episodes=self.risk_episode_store,
            narrow_policy=(self.narrow_hard_failure_policy if self.narrow_hard_failure_canary_enabled else None),
            narrow_controller=self.narrow_hard_failure_controller,
            narrow_candidate=(self._narrow_hard_failure_candidate if self.narrow_hard_failure_canary_enabled else None),
            confirmed_loss_recorder=self.loss_reentry_gate,
            loss_exit_enabled=self.loss_exit_enabled,
        )
        self.exit_requote_service = OutcomeExitRequoteService(
            recovery=self.recovery, machine=self.machine,
            stream_health=lambda: self.stream_health, store=self.exit_lifecycle_store,
            controller=self.exit_requote_controller, planner=self.exit_planner,
            holding_context=self._holding_context,
            reversal_classifier=self.reversal_classifier,
            opposite_observation_counts=self._opposite_observation_counts,
            canary_eligible_order_ids=self._e5_canary_eligible_order_ids,
            fresh_book=self._fresh_book_once, top_of_book=self._top_of_book,
            persisted_policy=self._persisted_p3_exit_policy,
            persisted_maker_fee=self._persisted_p3_maker_fee,
            strategy_exit_tier=self._strategy_exit_tier,
            enabled=self.exit_requote_enabled,
            canary_enabled=self.exit_requote_canary_enabled,
            loss_exit_enabled=lambda: self.loss_exit_enabled,
            gate_audit=self._audit_exit_requote_gate,
        )
        self._current_tick_snapshot: OutcomeRuntimeTickSnapshot | None = None
        # This is observation-only and intentionally process-local.  A fresh
        # process/new market emits full evidence rather than guessing prior
        # state.  Only the currently active market is retained, preventing an
        # unbounded dictionary over daily rollovers.
        self._decision_telemetry_market_id: int | None = None
        self._decision_telemetry_state: dict[str, tuple[str, float]] = {}

    def safety_components(self, *, settlement_ready: bool | None = None) -> tuple[SafetyComponent, ...]:
        """Report loaded components from runtime truth, never config intent."""
        exit_ready = self.exit_lifecycle_store is not None and self.exit_requote_controller is not None
        return (
            SafetyComponent("account_reconciliation", self.recovery is not None, "safety_critical", "v1", "ready" if self.recovery else "missing", self.recovery is not None, True),
            SafetyComponent("protective_sell", self.holding_execution_service is not None, "safety_critical", "v1", "ready" if self.holding_execution_service else "missing", self.holding_execution_service is not None, True),
            SafetyComponent("entry_ambiguity_fence", self.entry_lifecycle_store is not None, "safety_critical", "v1", "ready" if self.entry_lifecycle_store else "missing", self.entry_lifecycle_store is not None, True),
            SafetyComponent("exit_ambiguity_fence", self.exit_lifecycle_store is not None, "safety_critical", "v1", "ready" if self.exit_lifecycle_store else "missing", self.exit_lifecycle_store is not None, True),
            SafetyComponent("exit_lifecycle", self.exit_lifecycle_store is not None, "safety_critical", "v1", "ready" if self.exit_lifecycle_store else "missing", self.exit_lifecycle_store is not None, True),
            SafetyComponent("exit_requote", self.exit_requote_enabled(), "live_exit_authorization", "e4", "ready" if exit_ready else "missing", exit_ready, True),
            SafetyComponent("loss_band", self.loss_exit_enabled and self.exit_requote_enabled(), "live_exit_authorization", "e4", "ready" if self.loss_exit_enabled and exit_ready else ("operator_disabled" if not self.loss_exit_enabled else "missing"), True if not self.loss_exit_enabled else exit_ready, True),
            SafetyComponent("fast_failure", self.loss_exit_enabled and self.fast_failure_exit_controller is not None, "live_exit_authorization", "v1", "ready" if self.loss_exit_enabled and self.fast_failure_exit_controller else ("operator_disabled" if not self.loss_exit_enabled else "missing"), True if not self.loss_exit_enabled else self.fast_failure_exit_controller is not None, True),
            SafetyComponent("narrow_hard_failure_canary", self.loss_exit_enabled and self.narrow_hard_failure_canary_enabled, "live_exit_authorization", "v1", "ready" if self.loss_exit_enabled and self.narrow_hard_failure_controller else ("operator_disabled" if not self.loss_exit_enabled else "disabled_or_missing_shared_budget"), True if not self.loss_exit_enabled else self.narrow_hard_failure_controller is not None, self.narrow_hard_failure_canary_enabled),
            SafetyComponent("s3_emergency", self.loss_exit_enabled and self.emergency_exit_controller is not None, "live_exit_authorization", "s3", "ready" if self.loss_exit_enabled and self.emergency_exit_controller else ("operator_disabled" if not self.loss_exit_enabled else "missing"), True if not self.loss_exit_enabled else self.emergency_exit_controller is not None, True),
            SafetyComponent("reversal_monitor", self.reversal_classifier is not None, "strategy_input", "v1", "ready" if self.reversal_classifier else "missing", self.reversal_classifier is not None),
            SafetyComponent("crash_shadow", self.crash_circuit_shadow is not None, "read_only", "v1", "ready" if self.crash_circuit_shadow else "missing", self.crash_circuit_shadow is not None),
            SafetyComponent("holding_path", self.holding_path_recorder is not None, "read_only", "v3", "ready" if self.holding_path_recorder else "missing", self.holding_path_recorder is not None),
            SafetyComponent("ws_stream_health", self.stream_health is not None, "safety_critical", "v1", "attached" if self.stream_health else "not_attached", self.stream_health is not None, True),
            SafetyComponent("portfolio_guard", self.portfolio_guard is not None, "safety_critical", "f5", "ready" if self.portfolio_guard else "missing", self.portfolio_guard is not None, True),
            SafetyComponent("risk_episode_budget", self.risk_episode_store is not None, "live_exit_authorization", "v1", "enabled" if self.risk_episode_store else "disabled", True),
            SafetyComponent("settlement_worker", settlement_ready is not None, "read_only", "v1", "ready" if settlement_ready else "external_or_missing", bool(settlement_ready)),
            SafetyComponent("market_risk_monitor", self.market_risk_monitor is not None, "read_only", "v1", "ready" if self.market_risk_monitor else "missing", self.market_risk_monitor is not None),
        )

    def write_startup_manifest(self, *, settlement_ready: bool | None = None) -> bool:
        return self.runtime_safety.write_startup_manifest(self.safety_components(settlement_ready=settlement_ready))

    def safety_ready_for_new_entry(self, *, outcome_id: int | None = None) -> tuple[bool, str]:
        components = self.safety_components()
        missing = [item.name for item in components if item.safety_critical and not item.ready]
        if missing:
            self.runtime_safety.audit_not_ready(components=components, outcome_id=outcome_id)
            return False, "safety_components_not_ready:" + ",".join(missing)
        return True, "ready"

    def audit_safety_for_existing_holding(self, *, outcome_id: int) -> None:
        """Raise durable attention when an owned position loses exit safety."""
        self.runtime_safety.audit_not_ready(
            components=self.safety_components(), outcome_id=outcome_id, active_holding=True,
        )

    def _audit_exit_safety_gate(
        self, *, component: str, eligible: bool, reason: str, market: OutcomeMarketSpec,
        lifecycle: object, item: object, loss_band_state: str | None,
        book_state: str | None, executable_pnl: str | None,
    ) -> None:
        try:
            position_age_sec = float(getattr(item, "holding_age_sec", None))
        except (TypeError, ValueError):
            position_age_sec = None
        self.runtime_safety.audit_gate(
            component=component, eligible=eligible, reason=reason,
            outcome_id=market.outcome_id,
            lifecycle_id=str(getattr(lifecycle, "order_id", "")) or None,
            position_age_sec=position_age_sec,
            current_executable_pnl=executable_pnl,
            reversal_state=(
                "confirmed" if int(getattr(item, "reversal_independent_observations", 0)) >= 3
                else "not_confirmed"
            ),
            independent_confirmation_count=int(getattr(item, "reversal_independent_observations", 0)),
            loss_band_state=loss_band_state, book_state=book_state,
        )

    def _audit_exit_requote_gate(self, **payload: object) -> None:
        market = payload.pop("market")
        assert isinstance(market, OutcomeMarketSpec)
        lifecycle = payload.pop("lifecycle")
        self.runtime_safety.audit_gate(
            component=str(payload.pop("component")), eligible=bool(payload.pop("eligible")),
            reason=str(payload.pop("reason")), outcome_id=market.outcome_id,
            lifecycle_id=str(getattr(lifecycle, "order_id", "")) or None,
            position_age_sec=payload.pop("position_age_sec", None),
            current_executable_pnl=payload.pop("executable_pnl", None),
            reversal_state=payload.pop("reversal_state", None),
            independent_confirmation_count=payload.pop("independent_confirmation_count", None),
            loss_band_state=payload.pop("loss_band_state", None),
            book_state=payload.pop("book_state", None),
        )

    def set_open_orders_stream(self, stream: object | None) -> None:
        """Attach the launcher's cross-validated user-order observation."""
        self._account_reads.set_open_orders_stream(stream)

    def _should_sync_fills(
        self, *, active: list[object], pending_owned_entry: bool, now: float,
        pending_owned_exit: bool = False, stable_protective_exit: bool = False,
        pending_ambiguous_exit: bool = False,
    ) -> bool:
        """Reserve synchronous high-weight fill reads for unsafe states.

        The read-only research worker independently records the ordinary
        30-second ``userFills`` evidence stream.  A stable, account-confirmed
        covering SELL may use that cadence; every other owned-exit state
        remains immediate so a flat/stale lifecycle cannot be terminalised
        without independent fill proof.
        """
        urgent = pending_owned_entry or pending_ambiguous_exit or (
            pending_owned_exit and not stable_protective_exit
        ) or any(
            str(getattr(finding, "state", "")) in {"unprotected_inventory", "conflicting_orders", "orphan_sell"}
            for finding in active
        )
        return urgent or now - self._last_fill_sync_at >= 30.0

    @staticmethod
    def _audit_decimal(audit: dict[str, object], name: str) -> Decimal | None:
        try:
            value = audit.get(name)
            return Decimal(str(value)) if value is not None else None
        except (ArithmeticError, ValueError):
            return None

    def _entry_fast_risk_decision(
        self, *, market: OutcomeMarketSpec, lifecycle: OutcomeEntryLifecycle,
        current_side_index: int, desired_side_index: int | None, decision_reason: str,
        now: float | None = None,
    ) -> tuple[bool, str | None, dict[str, object]]:
        """Observe strong cancel-only risks; ordinary quote movement stays on the 5m lane."""
        key = (market.outcome_id, lifecycle.coin)
        reason: str | None = None
        evidence: dict[str, object] = {}
        observed_now = time.monotonic() if now is None else now
        status = self.stream_health.check(market, now=observed_now) if self.stream_health is not None else None
        if status is None or not status.ready:
            reason = f"market_data_unhealthy:{status.reason if status is not None else 'not_configured'}"
        elif desired_side_index in (0, 1) and desired_side_index != current_side_index:
            reason = "confirmed_side_flip"
        elif desired_side_index not in (0, 1) and decision_reason in {
            "directional_confirmation_not_met", "selected_bid_in_no_trade_band",
        }:
            reason = "confirmed_signal_invalidation"
        else:
            snapshot = self.stream_health.fresh_book_top(market, lifecycle.coin) if self.stream_health else None
            audit = self.entry_lifecycle_store.submit_audit(
                order_id=lifecycle.order_id, coin=lifecycle.coin,
            ) if self.entry_lifecycle_store else None
            if snapshot is not None:
                bid = Decimal(str(snapshot["bid"]))
                depth = Decimal(str(snapshot["top3_bid_depth"]))
                evidence.update({"ws_bid": str(bid), "ws_ask": str(snapshot["ask"]),
                                 "ws_top3_bid_depth": str(depth)})
                drift_bps = max(Decimal("0"), (lifecycle.price - bid) / lifecycle.price * Decimal("10000"))
                drift_limit = self._audit_decimal(audit or {}, "entry_max_submit_drift_bps") or Decimal("25")
                evidence.update({"adverse_bid_drift_bps": str(drift_bps), "drift_limit_bps": str(drift_limit)})
                baseline_depth = self._audit_decimal(audit or {}, "entry_top3_depth_shares")
                submitted = self._audit_decimal(audit or {}, "entry_submitted_shares")
                depth_collapsed = bool(
                    baseline_depth is not None and baseline_depth > 0 and submitted is not None and submitted > 0
                    and depth < baseline_depth
                    and depth <= submitted * Decimal("1.25")
                )
                if drift_bps >= drift_limit:
                    reason = "adverse_bid_drift"
                elif depth_collapsed:
                    reason = "supporting_bid_depth_collapsed"
        observed = self.entry_fast_risk_tracker.observe(key=key, reason=reason, now=observed_now)
        evidence.update({
            "reason": observed.reason, "observation_count": observed.observation_count,
            "duration_sec": round(observed.duration_sec, 3), "confirmed": observed.confirmed,
        })
        if self.ledger is not None and reason is not None:
            self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_ENTRY_FAST_RISK", {
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
                "coin": lifecycle.coin, "order_id": lifecycle.order_id,
                "execution_submitted": False, **evidence,
            })
        return observed.confirmed, observed.reason, evidence

    @staticmethod
    def _context_decimal(context: dict[str, object], name: str) -> Decimal | None:
        try:
            value = context.get(name)
            return Decimal(str(value)) if value is not None else None
        except (ArithmeticError, ValueError):
            return None

    def _log_best_effort_strategy_event(
        self, event_type: str, payload: dict[str, object],
    ) -> int | None:
        """Write shadow/admission telemetry without delaying live safety work.

        Execution intent, lifecycle, fills, and reconciliation deliberately do
        not call this helper.  They retain their existing durable writes and
        fail-closed behaviour.  Lightweight test journals may not implement
        the new best-effort method, so their normal in-memory write is a safe
        compatibility fallback.
        """
        if self.ledger is None:
            return None
        journal = self.ledger.journal
        writer = getattr(journal, "log_best_effort_strategy_event", None)
        if callable(writer):
            return writer(self.ledger.run_id, event_type, payload)
        return journal.log_strategy_event(self.ledger.run_id, event_type, payload)

    def _record_wide_spread_candidate(
        self, *, market: OutcomeMarketSpec, coin: str, bid: Decimal, ask: Decimal | None,
        quality: object, entry_tier: str, requested_shares: int, admission: dict[str, object],
    ) -> None:
        """Write bounded evidence for a rejected entry; never change its result."""
        if self.wide_spread_candidate_tracker is None:
            return
        if str(getattr(quality, "reason", "")) != "entry_spread_exceeds_calibrated_ceiling":
            return
        regime = admission.get("market_regime_shadow")
        self.wide_spread_candidate_tracker.observe_rejection(
            market=market, coin=coin, observed_at_ms=int(time.time() * 1000), bid=bid, ask=ask,
            spread_bps=getattr(quality, "spread_bps", None), entry_tier=entry_tier,
            time_left_sec=market.time_to_expiry_sec(),
            regime=regime if isinstance(regime, dict) else None,
            requested_shares=requested_shares,
            safe_max_shares=getattr(quality, "safe_max_shares", None),
            top3_depth_shares=getattr(quality, "top_depth_shares", None),
            recent_trade_shares_5m=getattr(quality, "recent_trade_shares", None),
        )

    def _observe_entry_readiness_shadow(
        self, *, market: OutcomeMarketSpec, entry_side_index: int | None,
        entry_evidence: dict[str, object],
    ) -> dict[str, object]:
        """Journal a compact, candidate-bound read-only readiness result.

        This is intentionally invoked before admission gates, but its output
        is stored only in ``admission`` and never read by the entry state
        machine.  Absent upstream candidates therefore produce an explicit
        not-evaluated record rather than fabricated positive evidence.
        """
        now = time.time()
        decision_observed_at_ms = entry_evidence.get("decision_observed_at_ms")
        if entry_side_index not in (0, 1):
            payload: dict[str, object] = {
                "schema_version": 1, "venue": "hyperliquid_outcome", "read_only": True,
                "live_authority": False, "execution_submitted": False,
                "outcome_id": market.outcome_id, "period": market.period,
                "side_index": entry_side_index, "timestamp": now,
                "decision_observed_at_ms": decision_observed_at_ms,
                "candidate": False, "persistent_candidate": False,
                "state": "ENTRY_READINESS_NOT_EVALUATED_SHADOW",
                "reason": "no_upstream_entry_candidate",
                "promotion_boundary": {"shadow_only": True, "may_submit_order": False,
                                       "may_cancel_order": False, "may_replace_order": False,
                                       "may_block_entry": False, "may_change_stale_cancel_age": False},
            }
        elif self.entry_readiness_shadow is None:
            payload = {
                "schema_version": 1, "venue": "hyperliquid_outcome", "read_only": True,
                "live_authority": False, "execution_submitted": False,
                "outcome_id": market.outcome_id, "period": market.period,
                "side_index": entry_side_index, "timestamp": now,
                "decision_observed_at_ms": decision_observed_at_ms,
                "candidate": False, "persistent_candidate": False,
                "state": "ENTRY_READINESS_NOT_EVALUATED_SHADOW", "reason": "observer_unavailable",
                "promotion_boundary": {"shadow_only": True, "may_submit_order": False,
                                       "may_cancel_order": False, "may_replace_order": False,
                                       "may_block_entry": False, "may_change_stale_cancel_age": False},
            }
        else:
            try:
                payload = self.entry_readiness_shadow.evaluate(
                    outcome_id=market.outcome_id, period=market.period, side_index=entry_side_index,
                    yes_coin=market.yes_coin, no_coin=market.no_coin, now=now,
                    decision_observed_at_ms=(int(decision_observed_at_ms)
                                             if decision_observed_at_ms is not None else None),
                )
            except Exception:
                payload = {
                    "schema_version": 1, "venue": "hyperliquid_outcome", "read_only": True,
                    "live_authority": False, "execution_submitted": False,
                    "outcome_id": market.outcome_id, "period": market.period,
                    "side_index": entry_side_index, "timestamp": now,
                    "decision_observed_at_ms": decision_observed_at_ms,
                    "candidate": False, "persistent_candidate": False,
                    "state": "ENTRY_READINESS_NOT_READY_SHADOW", "reason": "observer_error",
                    "promotion_boundary": {"shadow_only": True, "may_submit_order": False,
                                           "may_cancel_order": False, "may_replace_order": False,
                                           "may_block_entry": False, "may_change_stale_cancel_age": False},
                }
        if self.ledger is not None:
            side_key = int(entry_side_index) if entry_side_index in (0, 1) else -1
            fingerprint = ":".join((str(payload.get("state")), str(payload.get("candidate")),
                                      str(payload.get("persistent_candidate")), str(payload.get("reason"))))
            key, observed = (market.outcome_id, side_key), time.monotonic()
            previous = self._last_entry_readiness_shadow_record.get(key)
            if previous is None or previous[0] != fingerprint or observed - previous[1] >= 30.0:
                self._log_best_effort_strategy_event("OUTCOME_ENTRY_READINESS_SHADOW", payload)
                self._last_entry_readiness_shadow_record[key] = (fingerprint, observed)
        return payload

    def _observe_market_regime_shadow(
        self, *, market: OutcomeMarketSpec, entry_side_index: int | None,
        entry_evidence: dict[str, object], market_context: dict[str, object] | None,
    ) -> dict[str, object]:
        """Classify the market for audit only; this never changes an order."""
        context = dict(market_context or {})
        continuation = entry_evidence.get("trend_continuation")
        continuation_data = continuation if isinstance(continuation, dict) else {}
        bid = self._context_decimal(context, "yes_best_bid")
        ask = self._context_decimal(context, "yes_best_ask")
        midpoint = (bid + ask) / Decimal("2") if bid is not None and ask is not None and bid < ask else None
        decision = self.market_regime_shadow.observe_market(OutcomeMarketRegimeInput(
            outcome_id=market.outcome_id, now_ts=time.time(), yes_midpoint=midpoint,
            spot_strike_bps=self._context_decimal(context, "spot_strike_bps"),
            mark_5m_bps=self._context_decimal(context, "mark_return_bps"),
            mark_15m_bps=self._context_decimal(continuation_data, "mark_15m_bps"),
            mark_60m_bps=self._context_decimal(continuation_data, "mark_60m_bps"),
            candidate_side_index=entry_side_index,
        ))
        payload = {
            "venue": "hyperliquid_outcome", "read_only": True,
            "outcome_id": market.outcome_id, "period": market.period,
            "state": decision.state, "reason": decision.reason,
            "candidate_side_index": entry_side_index,
            "yes_midpoint": str(midpoint) if midpoint is not None else None,
            "spot_strike_bps": str(self._context_decimal(context, "spot_strike_bps")) if self._context_decimal(context, "spot_strike_bps") is not None else None,
            "mark_5m_bps": str(self._context_decimal(context, "mark_return_bps")) if self._context_decimal(context, "mark_return_bps") is not None else None,
            "mark_15m_bps": str(self._context_decimal(continuation_data, "mark_15m_bps")) if self._context_decimal(continuation_data, "mark_15m_bps") is not None else None,
            "mark_60m_bps": str(self._context_decimal(continuation_data, "mark_60m_bps")) if self._context_decimal(continuation_data, "mark_60m_bps") is not None else None,
            "confirmed_crosses_2h": decision.confirmed_crosses_2h,
            "last_cross_at": decision.last_cross_at,
            "current_zone": decision.current_zone,
            "execution_submitted": False,
        }
        if self.ledger is not None:
            previous = self._last_regime_shadow_record.get(market.outcome_id)
            now = time.monotonic()
            # Persist state changes immediately and otherwise a compact row at
            # 30-second cadence.  The in-memory classifier still sees every
            # strategy tick, so rate limiting storage cannot change a state.
            if previous is None or previous[0] != str(decision.state) or now - previous[1] >= 30.0:
                event_id = self._log_best_effort_strategy_event("OUTCOME_MARKET_REGIME_SHADOW", payload)
                payload["event_id"] = event_id
                self._last_regime_shadow_record[market.outcome_id] = (str(decision.state), now)
        self._latest_regime_shadow[market.outcome_id] = dict(payload)
        return payload

    def _observe_confidence_entry_shadow(
        self, *, market: OutcomeMarketSpec, market_context: dict[str, object] | None,
    ) -> dict[str, object]:
        payload = self.confidence_entry_shadow.evaluate(
            context=dict(market_context or {}), time_left_sec=market.time_to_expiry_sec(),
        )
        payload.update({"venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period})
        if self.ledger is not None:
            fingerprint = f"{payload.get('candidate_side_index')}:{payload.get('candidate_confidence')}:{payload.get('reason')}"
            key = ("confidence", market.outcome_id, -1)
            previous = self._last_efficiency_shadow_record.get(key)
            now = time.monotonic()
            if previous is None or previous[0] != fingerprint or now - previous[1] >= 30.0:
                self._log_best_effort_strategy_event("OUTCOME_CONFIDENCE_ENTRY_SHADOW", payload)
                self._last_efficiency_shadow_record[key] = (fingerprint, now)
        return payload

    def _observe_active_challenger_shadow(
        self, *, market: OutcomeMarketSpec, market_context: dict[str, object] | None,
        production_side_index: int | None, production_reason: str,
        regime: dict[str, object] | None,
    ) -> dict[str, object]:
        """Record a B5 counterfactual entry action; never submit it."""
        payload = self.active_challenger_shadow.evaluate_entry(
            context=dict(market_context or {}),
            time_left_sec=market.time_to_expiry_sec(),
            production_side_index=production_side_index,
            production_reason=production_reason,
            regime=regime,
        )
        payload.update({"venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period})
        if self.ledger is not None:
            challenger = payload.get("challenger") if isinstance(payload.get("challenger"), dict) else {}
            fingerprint = f"{challenger.get('action')}:{challenger.get('side_index')}:{challenger.get('reason')}"
            key = ("active_challenger", market.outcome_id, -1)
            previous = self._last_efficiency_shadow_record.get(key)
            now = time.monotonic()
            if previous is None or previous[0] != fingerprint or now - previous[1] >= 30.0:
                self._log_best_effort_strategy_event("OUTCOME_ACTIVE_CHALLENGER_SHADOW", payload)
                self._last_efficiency_shadow_record[key] = (fingerprint, now)
        return payload

    def _observe_queue_pricing_shadow(
        self, *, market: OutcomeMarketSpec, side_index: int, book: dict[str, object],
        requested_shares: int, current_bid: Decimal, confidence: dict[str, object] | None,
    ) -> dict[str, object]:
        scores = confidence.get("scores") if isinstance(confidence, dict) else None
        fair_score = scores.get(str(side_index), {}) if isinstance(scores, dict) else {}
        payload = self.queue_pricing_shadow.evaluate(
            side_index=side_index, book=book, requested_shares=requested_shares,
            fair_score=fair_score if isinstance(fair_score, dict) else {}, current_bid=current_bid,
        )
        payload.update({"venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period})
        if self.ledger is not None:
            fingerprint = f"{payload.get('action')}:{payload.get('reason')}:{payload.get('shadow_quote')}"
            key = ("queue", market.outcome_id, side_index)
            previous = self._last_efficiency_shadow_record.get(key)
            now = time.monotonic()
            if previous is None or previous[0] != fingerprint or now - previous[1] >= 30.0:
                self._log_best_effort_strategy_event("OUTCOME_QUEUE_PRICING_SHADOW", payload)
                self._last_efficiency_shadow_record[key] = (fingerprint, now)
        return payload

    def _observe_toxic_fill_shadow(self, *, market: OutcomeMarketSpec, finding: object) -> None:
        """Record a short post-fill adverse-selection candidate without exiting."""
        if self.ledger is None or self.stream_health is None:
            return
        coin = str(getattr(finding, "coin", ""))
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        snapshot = self.stream_health.fresh_book_top(market, coin)
        if not coin or inventory <= 0 or vwap is None or snapshot is None:
            return
        provenance = self._resolve_holding_entry_provenance(
            market=market, coin=coin, inventory=inventory, fill_vwap=vwap,
        ) or {}
        try:
            filled_at = datetime.fromisoformat(str(provenance["entry_filled_at"])).timestamp()
            holding_age_sec = max(0.0, time.time() - filled_at)
            age_basis = "official_fill_timestamp"
        except (KeyError, TypeError, ValueError):
            return
        decision = self.market_regime_shadow.observe_toxic_fill(OutcomeToxicFillInput(
            outcome_id=market.outcome_id, coin=coin, now_ts=time.time(), holding_age_sec=holding_age_sec,
            fill_vwap=vwap, best_bid=Decimal(str(snapshot["bid"])),
            top3_bid_depth=Decimal(str(snapshot["top3_bid_depth"])),
        ))
        key, now = (market.outcome_id, coin), time.monotonic()
        previous = self._last_toxic_shadow_record.get(key)
        # During the two-minute window retain a compact ten-second path, plus
        # every state change.  No raw L2 levels are copied to the journal.
        if previous is not None and previous[0] == str(decision.state) and now - previous[1] < 10.0:
            return
        self._log_best_effort_strategy_event("OUTCOME_TOXIC_FILL_SHADOW", {
            "venue": "hyperliquid_outcome", "read_only": True,
            "outcome_id": market.outcome_id, "period": market.period, "coin": coin,
            "state": decision.state, "reason": decision.reason,
            "holding_age_sec": round(holding_age_sec, 3), "age_basis": age_basis,
            "fill_vwap": str(vwap), "best_bid": str(snapshot["bid"]),
            "top3_bid_depth": str(snapshot["top3_bid_depth"]),
            "executable_return_pct": str(decision.executable_return_pct) if decision.executable_return_pct is not None else None,
            "bid_drift_bps": str(decision.bid_drift_bps) if decision.bid_drift_bps is not None else None,
            "depth_ratio": str(decision.depth_ratio) if decision.depth_ratio is not None else None,
            "observation_count": decision.observation_count,
            "duration_sec": round(decision.duration_sec, 3), "execution_submitted": False,
        })
        self._last_toxic_shadow_record[key] = (str(decision.state), now)

    def _observe_postfill_quality_shadow(self, *, market: OutcomeMarketSpec, finding: object) -> None:
        """Record a scratch counterfactual after protection, never an action."""
        if self.ledger is None or self.stream_health is None:
            return
        coin = str(getattr(finding, "coin", ""))
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        snapshot = self.stream_health.fresh_book_top(market, coin)
        if not coin or inventory <= 0 or vwap is None or snapshot is None:
            return
        provenance = self._resolve_holding_entry_provenance(
            market=market, coin=coin, inventory=inventory, fill_vwap=vwap,
        )
        if not provenance:
            return
        try:
            filled_at = datetime.fromisoformat(str(provenance["entry_filled_at"])).timestamp()
            trade_id = str(provenance["entry_trade_id"])
            order_id = str(provenance["entry_order_id"])
        except (KeyError, TypeError, ValueError):
            return
        holding_age_sec = max(0.0, time.time() - filled_at)
        # Phase B only labels immediate post-fill quality.  Long-held risk is
        # covered by the existing bounded holding-path/crash observers; do
        # not mirror it here and grow a multi-GB journal indefinitely.
        if holding_age_sec > 120.0:
            return
        payload = self.entry_quality_shadow.evaluate_post_fill(OutcomePostFillQualityInput(
            outcome_id=market.outcome_id, period=market.period, coin=coin,
            order_id=order_id, fill_trade_id=trade_id, fill_vwap=vwap,
            holding_age_sec=holding_age_sec,
            best_bid=Decimal(str(snapshot["bid"])), best_ask=Decimal(str(snapshot["ask"])),
            top3_bid_depth=Decimal(str(snapshot["top3_bid_depth"])),
        ))
        action = str((payload.get("post_fill_scratch_shadow") or {}).get("action"))
        previous, now = self._last_postfill_quality_record.get(trade_id), time.monotonic()
        if previous is not None and previous[0] == action and now - previous[1] < 10.0:
            return
        self._log_best_effort_strategy_event("OUTCOME_POST_FILL_QUALITY_SHADOW", payload)
        self._last_postfill_quality_record[trade_id] = (action, now)

    def _continuation_entry_already_submitted(self, *, outcome_id: int) -> bool:
        """One continuation canary submit per daily market, including restarts."""
        return self.journal_view.continuation_entry_already_submitted(outcome_id)

    def _capture_trend_continuation_path(
        self, *, market: OutcomeMarketSpec, entry_evidence: dict[str, object], market_context: dict[str, object] | None,
    ) -> None:
        if self.trend_continuation_recorder is None:
            return
        candidate = entry_evidence.get("trend_continuation")
        context = market_context or {}
        try:
            bbo = {
                0: (context.get("yes_best_bid"), context.get("yes_best_ask")),
                1: (context.get("no_best_bid"), context.get("no_best_ask")),
            }
        except AttributeError:
            return
        self.trend_continuation_recorder.observe(
            outcome_id=market.outcome_id, period=market.period,
            candidate=candidate if isinstance(candidate, dict) else None,
            bbo_by_side=bbo,
        )

    def _begin_tick(self) -> None:
        self._account_reads.begin_tick()
        self.journal_view.begin_tick()
        self._tick_books.clear()
        begin_timing_scope = getattr(self.machine.gateway, "begin_timing_scope", None)
        if callable(begin_timing_scope):
            begin_timing_scope()

    def _fresh_book_once(self, *, market: OutcomeMarketSpec, side_index: int) -> dict[str, object]:
        """One REST L2 snapshot per decision tick for observational logic.

        This cache is never passed through a mutation boundary.  Controllers
        independently refetch after cancel confirmation, preserving the
        post-only and price-protection contracts.
        """
        key = (market.outcome_id, side_index)
        book = self._tick_books.get(key)
        if book is None:
            book = self.machine.gateway.fetch_order_book(market=market, side_index=side_index)
            self._tick_books[key] = book
        return book

    @staticmethod
    def _top_of_book(book: dict[str, object]) -> tuple[Decimal, Decimal] | None:
        try:
            bid = Decimal(str(book["bids"][0]["price"]))  # type: ignore[index]
            ask = Decimal(str(book["asks"][0]["price"]))  # type: ignore[index]
            return (bid, ask) if Decimal("0") < bid < ask < Decimal("1") else None
        except (IndexError, KeyError, TypeError, ValueError, ArithmeticError):
            return None

    def _record(self, market: OutcomeMarketSpec, side_index: int, result: MakerTickResult) -> LiveExecutionResult:
        coin = self.machine.gateway.outcome_coin(market, side_index)
        if self.ledger:
            self.ledger.record_transition(market_id=market.outcome_id, coin=coin, result=result)
            fills = getattr(self.recovery.account, "get_user_fills_sync", lambda _user: [])(self.recovery.wallet)
            self.ledger.sync_fills(fills=fills, market_key=f"outcome:{market.outcome_id}", period=market.period)
        if result.state == "sell_placed" and self.exit_lifecycle_store and result.order_id and result.audit:
            try:
                lifecycle = OutcomeExitLifecycle(
                    wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin, order_id=str(result.order_id),
                    inventory=Decimal(str(result.audit["inventory"])), target_price=Decimal(str(result.audit["requested_price"])),
                    replacement_count=0, state="SELL_RESTING",
                )
                intent_id = str(result.audit.get("exit_intent_id") or "")
                if self.exit_lifecycle_store.record(
                    lifecycle, reason="initial_verified_alo_sell",
                    extra={"pricing_basis": result.audit.get("pricing_basis"),
                           "exit_mode": result.audit.get("exit_mode"), "exit_intent_id": intent_id or None},
                    durable=True,
                ) is None:
                    raise RuntimeError("acknowledged protective SELL ownership could not persist durably")
                if intent_id and not self.exit_lifecycle_store.finalize_submit_intent(
                    intent_id=intent_id, order_id=str(result.order_id),
                    reason="sdk_acknowledged_initial_protective_sell",
                ):
                    raise RuntimeError("acknowledged protective SELL intent could not finalize durably")
                self._e5_canary_eligible_order_ids.add(str(result.order_id))
            except (KeyError, ValueError):
                pass
        if result.state == "buy_placed" and self.entry_lifecycle_store and result.order_id and result.audit:
            # Only an S0 schema-versioned decision can grant future entry
            # cancel/requote ownership.  P3 and manual orders remain outside
            # this controller's authority.
            try:
                if result.audit.get("entry_policy_schema_version") == 1 and result.audit.get("entry_policy_kind") in {
                    "s0_oi_spot_mark_confirmation", "s0_spot_mark_tier_b",
                }:
                    persisted = self.entry_lifecycle_store.record(OutcomeEntryLifecycle(
                        wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
                        order_id=str(result.order_id),
                        # The state machine can refresh the book between the
                        # admission decision and venue submit.  Lifecycle
                        # quote management must track the accepted limit,
                        # never the earlier decision snapshot.
                        price=Decimal(str(result.audit.get(
                            "entry_submit_bid", result.audit["entry_bid_at_decision"],
                        ))),
                        replacement_count=0, state="BUY_RESTING",
                    ), reason="initial_audited_s0_alo_buy", durable=True)
                    if persisted is None:
                        # The exchange already accepted this BUY.  Do not
                        # continue as if later cancel/requote ownership were
                        # established: the durable pre-submit intent plus
                        # account-truth recovery is the only safe route.
                        return LiveExecutionResult(
                            "blocked",
                            "acknowledged entry ownership persistence failed; reconciliation required",
                            str(result.order_id),
                        )
                    intent_id = result.audit.get("entry_intent_id")
                    if intent_id and self.ledger is not None:
                        self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_ORDER_INTENT_ACK", {
                            "venue": "hyperliquid_outcome", "intent_id": str(intent_id),
                            "outcome_id": market.outcome_id, "coin": coin,
                            "order_id": str(result.order_id), "execution_submitted": True,
                        })
            except (KeyError, ValueError):
                pass
        return LiveExecutionResult(result.state, result.detail, result.order_id)

    @staticmethod
    def enabled() -> bool:
        return (
            os.environ.get("OUTCOME_AUTOMATED_EXECUTION_ENABLED") == "1"
            and os.environ.get("OUTCOME_SDK_EXECUTION_ENABLED") == "1"
        )

    @staticmethod
    def calibration_enabled() -> bool:
        return (
            OutcomeLiveExecutionRuntime.enabled()
            and os.environ.get("OUTCOME_P3_CALIBRATION_ENABLED") == "1"
        )

    @staticmethod
    def live_strategy_enabled() -> bool:
        """Separate opt-in for S0; P3 calibration never enables it."""
        return (
            OutcomeLiveExecutionRuntime.enabled()
            and os.environ.get("OUTCOME_LIVE_STRATEGY_ENABLED") == "1"
        )

    @staticmethod
    def exit_requote_enabled() -> bool:
        """E4's explicit fourth gate; false is the safe default."""
        return OutcomeLiveExecutionRuntime.enabled() and os.environ.get("OUTCOME_EXIT_REQUOTE_ENABLED") == "1"

    @staticmethod
    def exit_requote_canary_enabled() -> bool:
        """One explicit additional gate for an automated, single E5 replacement."""
        return OutcomeLiveExecutionRuntime.exit_requote_enabled() and os.environ.get("OUTCOME_EXIT_REQUOTE_CANARY_ENABLED") == "1"

    def _daily_calibration_entries(self) -> int:
        return self.journal_view.daily_calibration_entries()

    def _persisted_entry_policy_evidence(self, *, market: OutcomeMarketSpec, coin: str) -> tuple[str, dict[str, object]] | None:
        """Load a verified entry policy, preferring strategy evidence.

        New S0 orders duplicate their policy decision on the matching
        ``ORDER_SUBMIT`` audit row.  This provides a recovery path if the
        process exits after the accepted order is journaled but before its
        follow-up strategy event commits.  When both records exist, their
        target and fee fields must agree; disagreement is fail-closed.
        """
        return self.journal_view.persisted_entry_policy(market, coin)

    def _persisted_p3_exit_policy(self, *, market: OutcomeMarketSpec, coin: str) -> OutcomeP3CalibrationConfig | None:
        """Recover the policy that created a still-managed P3/S0 inventory."""
        evidence = self._persisted_entry_policy_evidence(market=market, coin=coin)
        if evidence is None:
            return None
        _, payload = evidence
        try:
            return OutcomeP3CalibrationConfig(
                max_daily_entries=1,
                target_return_pct=Decimal(str(payload["target_return_pct"])),
                loss_reprice_pct=Decimal(str(payload["loss_reprice_pct"])),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _persisted_p3_maker_fee(self, *, market: OutcomeMarketSpec, coin: str) -> Decimal | None:
        evidence = self._persisted_entry_policy_evidence(market=market, coin=coin)
        if evidence is None:
            return None
        try:
            _, payload = evidence
            raw_fee = payload.get("maker_close_fee_rate")
            fee = Decimal(str(raw_fee)) if raw_fee is not None else None
            return fee if fee is not None and Decimal("0") <= fee < Decimal("1") else None
        except (ArithmeticError, ValueError):
            return None

    def _persisted_taker_fee(self, *, market: OutcomeMarketSpec, coin: str) -> Decimal | None:
        evidence = self._persisted_entry_policy_evidence(market=market, coin=coin)
        if evidence is None:
            return None
        try:
            raw_fee = evidence[1].get("taker_close_fee_rate")
            fee = Decimal(str(raw_fee)) if raw_fee is not None else None
            return fee if fee is not None and Decimal("0") <= fee < Decimal("1") else None
        except (ArithmeticError, ValueError):
            return None

    def _strategy_exit_tier(self, *, market: OutcomeMarketSpec, coin: str) -> tuple[Decimal, Decimal | None] | None:
        """Return the elapsed-time S0 target and any permitted loss-band floor.

        The strategy starts with a strict +5% fee-after target.  It may narrow
        to +3% after the first configured age and +2% after the second.  Only
        at that final tier may a weak book use the fee-inclusive break-even
        floor.  This prevents a one-tick adverse quote from replacing the
        initial take-profit order with a near-cost sell.
        """
        evidence = self._persisted_entry_policy_evidence(market=market, coin=coin)
        if evidence is None:
            return None
        try:
            timestamp, payload = evidence
            # Pre-submit-audit S0 records used the strategy event type and
            # tier fields but did not yet carry ``sampling_policy``.  The
            # presence of the full tier contract identifies that legacy S0
            # shape without mistaking a P3 calibration record for S0.
            is_s0 = (
                str(payload.get("entry_policy_kind", "")) in {"s0_oi_spot_mark_confirmation", "s0_spot_mark_tier_b"}
                or str(payload.get("sampling_policy", "")) in {"oi_spot_mark_confirmation", "spot_mark_tier_b"}
                or all(key in payload for key in ("narrow_after_sec", "narrow_return_pct", "floor_after_sec", "floor_return_pct"))
            )
            if not is_s0:
                return None
            target = Decimal(str(payload["target_return_pct"]))
            narrow_after = float(payload["narrow_after_sec"])
            narrow = Decimal(str(payload["narrow_return_pct"]))
            floor_after = float(payload["floor_after_sec"])
            floor = Decimal(str(payload["floor_return_pct"]))
            # Age is anchored to the original entry evidence, not the latest
            # replacement timestamp; otherwise each rebook would silently
            # postpone the +3%/+2% time tiers.
            age = max(0.0, time.time() - datetime.fromisoformat(timestamp).timestamp())
            if age >= floor_after:
                # The loss band is only eligible after the two-hour floor
                # tier.  It is a fee-inclusive -5% passive quote, never an
                # immediate stop or a taker instruction.
                return min(target, narrow, floor), Decimal("0.05")
            if age >= narrow_after:
                return min(target, narrow), None
            return target, None
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None

    @staticmethod
    def _decision_signal_summary(entry_evidence: dict[str, object]) -> dict[str, object]:
        """Keep scalar S0/gate-ablation evidence on every compact tick."""
        keys = (
            "decision_observed_at_ms", "oi_current_id", "oi_prior_id", "oi_age_ms",
            "spot_strike_bps", "oi_return_bps", "mark_return_bps", "oi_lookback_sec",
            "tier_b_enabled", "entry_tier",
        )
        summary = {key: entry_evidence.get(key) for key in keys if key in entry_evidence}
        variants = entry_evidence.get("gate_variants")
        if isinstance(variants, dict):
            # Gate ablation needs only eligibility/side, not the unrelated
            # nested score/research context carried by a legacy full event.
            summary["gate_variants"] = {
                str(name): {
                    "eligible": bool(value.get("eligible")),
                    "side_index": value.get("side_index"),
                }
                for name, value in variants.items() if isinstance(value, dict)
            }
        return summary

    @staticmethod
    def _decision_shadow_state(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        for key in ("state", "reason", "candidate_confidence", "decision_kind"):
            if value.get(key) is not None:
                return str(value[key])
        return None

    @staticmethod
    def _decision_blocker_code(*, state: str, reason: str) -> str:
        normalized = reason.lower()
        if state == "buy_placed":
            return "buy_placed"
        if "directional_confirmation_not_met" in normalized:
            return "no_signal"
        if "account recovery" in normalized:
            return "account_recovery_block"
        if "retiring" in normalized or "rollover" in normalized:
            return "retiring_market_active"
        if "existing outcome" in normalized or "protective" in normalized:
            return "active_inventory"
        if "owned entry" in normalized:
            return "pending_owned_entry"
        if "visibility" in normalized:
            return "fill_visibility_barrier"
        if "market-data gate" in normalized or "ws_" in normalized:
            return "stream_not_ready"
        if "stale" in normalized:
            return "stale_decision"
        if "risk gate" in normalized:
            return "risk_gate"
        if "portfolio" in normalized:
            return "portfolio_guard"
        if "submit" in normalized or state == "error":
            return "entry_submit_failed"
        if state == "flat":
            return "flat"
        return "other"

    def _decision_payload_mode(self, *, event_type: str, market: OutcomeMarketSpec,
                               state_signature: dict[str, object], important: bool = False) -> str:
        """Return compact/full mode without ever influencing execution flow."""
        now = time.monotonic()
        if self._decision_telemetry_market_id != market.outcome_id:
            self._decision_telemetry_market_id = market.outcome_id
            self._decision_telemetry_state.clear()
        key = event_type
        signature = json.dumps(state_signature, sort_keys=True, default=str, separators=(",", ":"))
        prior = self._decision_telemetry_state.get(key)
        if prior is None or prior[0] != signature or important:
            self._decision_telemetry_state[key] = (signature, now)
            return "full_transition"
        if now - prior[1] >= self._DECISION_TELEMETRY_FULL_HEARTBEAT_SEC:
            self._decision_telemetry_state[key] = (signature, now)
            return "full_heartbeat"
        return "compact"

    def _record_entry_gate_decision(self, *, market: OutcomeMarketSpec, entry_side_index: int | None,
                                    entry_reason: str, entry_evidence: dict[str, object],
                                    active: list[object]) -> None:
        """Persist the small S0 decision record needed for gate ablation.

        This is intentionally one event per live-strategy tick, not a raw
        WebSocket dump.  It records why an entry was or was not actionable,
        including the independent account/order blocker.
        """
        if self.ledger is None:
            return
        active_summary = [
            {"coin": str(getattr(item, "coin", "")), "state": str(getattr(item, "state", "")),
             "inventory": str(getattr(item, "inventory", "0"))}
            for item in active
        ]
        compact = {
            "schema_version": 2, "payload_mode": "compact",
            "venue": "hyperliquid_outcome", "read_only": True,
            "outcome_id": market.outcome_id, "period": market.period,
            "entry_side_index": entry_side_index, "entry_reason": entry_reason,
            "signal_present": entry_side_index is not None,
            "signal_summary": self._decision_signal_summary(entry_evidence),
            "active_exposure_count": len(active), "active_states": active_summary,
            "execution_submitted": False,
        }
        mode = self._decision_payload_mode(
            event_type="OUTCOME_ENTRY_GATE_DECISION", market=market,
            state_signature={
                "side": entry_side_index, "reason": entry_reason,
                "active": active_summary, "variants": compact["signal_summary"].get("gate_variants"),
            },
        )
        if mode == "compact":
            self._log_best_effort_strategy_event("OUTCOME_ENTRY_GATE_DECISION", compact)
            return
        self._log_best_effort_strategy_event("OUTCOME_ENTRY_GATE_DECISION", {
            "schema_version": 2, "payload_mode": mode,
            "venue": "hyperliquid_outcome", "read_only": True,
            "outcome_id": market.outcome_id, "period": market.period,
            "entry_side_index": entry_side_index, "entry_reason": entry_reason,
            "entry_evidence": entry_evidence,
            "active_account_states": [
                {"coin": str(getattr(item, "coin", "")), "state": str(getattr(item, "state", "")),
                 "inventory": str(getattr(item, "inventory", "0")),
                 "buy_order_ids": list(getattr(item, "buy_order_ids", ())),
                 "sell_order_ids": list(getattr(item, "sell_order_ids", ()))}
                for item in active
            ],
            "execution_submitted": False, "compact_summary": compact,
        })

    def _record_entry_admission_decision(self, *, market: OutcomeMarketSpec,
                                         entry_side_index: int | None, entry_reason: str,
                                         entry_evidence: dict[str, object],
                                         admission: dict[str, object],
                                         result: LiveExecutionResult) -> None:
        """Persist the final S0 admission result, after every venue-side gate.

        ``OUTCOME_ENTRY_GATE_DECISION`` describes the upstream signal only.
        This record answers the operationally distinct question: did that
        signal reach a live ALO submit, and if not, precisely which later
        guard stopped it?  It is journal-only and never changes an order.
        """
        if self.ledger is None:
            return
        stream: dict[str, object]
        if self.stream_health is None:
            stream = {"ready": False, "reason": "ws_health_not_configured"}
        else:
            status = self.stream_health.check(market)
            stream = {"ready": status.ready, "reason": status.reason}
        account = admission.get("account_recovery")
        account_safe = account.get("safe_for_new_entry") if isinstance(account, dict) else None
        account_reason = account.get("reason") if isinstance(account, dict) else None
        readiness = admission.get("entry_readiness_shadow")
        regime = admission.get("market_regime_shadow")
        final_reason = str(result.detail)
        blocker = self._decision_blocker_code(state=result.state, reason=final_reason)
        compact = {
            "schema_version": 2, "payload_mode": "compact",
            "venue": "hyperliquid_outcome", "read_only": True,
            "outcome_id": market.outcome_id, "period": market.period,
            "raw_signal_side_index": entry_side_index, "raw_signal_reason": entry_reason,
            "signal_summary": self._decision_signal_summary(entry_evidence),
            "final_state": result.state, "final_reason": final_reason,
            "blocker_code": blocker, "execution_submitted": result.state == "buy_placed",
            "order_id": result.order_id,
            "stream_ready": stream["ready"], "stream_reason": stream["reason"],
            "account_safe_for_new_entry": account_safe, "account_recovery_reason": account_reason,
            "reduce_only": bool(admission.get("reduce_only", False)),
            "active_exposure_count": admission.get("active_current_market_count"),
            "entry_readiness_state": self._decision_shadow_state(readiness),
            "entry_readiness_candidate": readiness.get("candidate") if isinstance(readiness, dict) else None,
            "entry_readiness_persistent_candidate": readiness.get("persistent_candidate") if isinstance(readiness, dict) else None,
            "market_regime_state": self._decision_shadow_state(regime),
        }
        important = result.state == "buy_placed" or blocker in {"entry_submit_failed", "account_recovery_block"}
        mode = self._decision_payload_mode(
            event_type="OUTCOME_ENTRY_ADMISSION_DECISION", market=market,
            state_signature={
                "side": entry_side_index, "signal_reason": entry_reason,
                "state": result.state, "blocker": blocker, "reason": final_reason,
                "stream": stream, "account_safe": account_safe,
                "active": compact["active_exposure_count"],
                "readiness": compact["entry_readiness_state"], "regime": compact["market_regime_state"],
            }, important=important,
        )
        if mode == "compact":
            self._log_best_effort_strategy_event("OUTCOME_ENTRY_ADMISSION_DECISION", compact)
            return
        self._log_best_effort_strategy_event("OUTCOME_ENTRY_ADMISSION_DECISION", {
            "schema_version": 2, "payload_mode": mode,
            "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
            "period": market.period, "read_only": True,
            "raw_signal_side_index": entry_side_index, "raw_signal_reason": entry_reason,
            "raw_signal_evidence": entry_evidence,
            "admission_inputs": admission,
            "stream_at_completion": stream,
            "final_state": result.state, "final_reason": result.detail,
            "execution_submitted": result.state == "buy_placed",
            "order_id": result.order_id,
            "blocker_code": blocker, "compact_summary": compact,
        })

    def tick(self, *, market: OutcomeMarketSpec, side_index: int, entry_permitted: bool) -> LiveExecutionResult:
        self._begin_tick()
        if not self.enabled():
            return LiveExecutionResult("disabled", "automated execution requires OUTCOME_AUTOMATED_EXECUTION_ENABLED=1 and OUTCOME_SDK_EXECUTION_ENABLED=1")
        if entry_permitted:
            return LiveExecutionResult("blocked", "generic live entry has no explicit verified exit policy; use a dedicated policy runtime")
        if entry_permitted:
            health_error = self._stream_ready(market)
            if health_error:
                return health_error
        report = self.recovery.reconcile([market])
        if not report.safe_for_new_entry:
            return LiveExecutionResult("blocked", f"account recovery blocked execution: {report.reason}")
        selected_coin = self.machine.gateway.outcome_coin(market, side_index)
        other_live = [finding for finding in report.findings if finding.coin != selected_coin and finding.state != "flat"]
        if other_live:
            return LiveExecutionResult("blocked", "other side of this Outcome already has live inventory or order")
        result: MakerTickResult = self.machine.tick(market=market, side_index=side_index, entry_permitted=entry_permitted)
        return self._record(market, side_index, result)

    def _stream_ready(self, market: OutcomeMarketSpec) -> LiveExecutionResult | None:
        if self.stream_health is None:
            return LiveExecutionResult("blocked", "market-data gate: ws_health_not_configured")
        stream = self.stream_health.check(market)
        if not stream.ready:
            return LiveExecutionResult("blocked", f"market-data gate: {stream.reason}")
        return None

    def _entry_fill_visibility_barrier(
        self, *, market: OutcomeMarketSpec, report: object, admission: dict[str, object],
    ) -> LiveExecutionResult | None:
        """Fail closed when an official entry fill precedes account visibility.

        Hyperliquid's user-fill feed can report a maker fill seconds before
        balances or open orders reflect its new inventory.  Treating that
        transient empty snapshot as flat used to permit a second BUY.  A
        durable official fill now blocks admission until account truth shows
        the inventory (or its protective sell); it is never used to infer a
        sell size.
        """
        if self.entry_lifecycle_store is None:
            return None
        findings = tuple(getattr(report, "findings", ()))
        for coin in (market.yes_coin, market.no_coin):
            lifecycle = self.entry_lifecycle_store.recover(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            )
            if lifecycle is None:
                continue
            finding = next((item for item in findings if getattr(item, "market_id", None) == market.outcome_id
                            and str(getattr(item, "coin", "")) == coin), None)
            if finding is None:
                continue
            inventory = Decimal(str(getattr(finding, "inventory", "0")))
            buy_ids = {str(value) for value in getattr(finding, "buy_order_ids", ())}
            sell_ids = tuple(getattr(finding, "sell_order_ids", ()))
            # Once the account confirms either the inventory or a covering
            # sell, the fill is reconciled and cannot block a later, flat
            # round trip.
            if inventory > 0 or sell_ids:
                if lifecycle.state in {"BUY_RESTING", "CANCEL_SUBMITTED", "FILL_PENDING_RECONCILIATION", "RECONCILE_REQUIRED"}:
                    self.entry_lifecycle_store.record(
                        lifecycle, reason="entry_account_visibility_reconciled",
                        extra={"state": "FILL_RECONCILED", "account_inventory": str(inventory),
                               "sell_order_ids": list(sell_ids)},
                    )
                continue
            if lifecycle.order_id in buy_ids:
                continue
            fill = self.entry_lifecycle_store.official_buy_fill(
                outcome_id=market.outcome_id, coin=coin, order_id=lifecycle.order_id,
            )
            if fill is not None:
                if lifecycle.state != "FILL_PENDING_RECONCILIATION":
                    self.entry_lifecycle_store.record(
                        lifecycle, reason="official_buy_fill_before_account_visibility",
                        extra={"state": "FILL_PENDING_RECONCILIATION", "official_fill": fill},
                    )
                admission["entry_fill_visibility_gate"] = {
                    "allowed": False, "reason": "official_buy_fill_pending_account_reconciliation",
                    "coin": coin, "order_id": lifecycle.order_id, "official_fill": fill,
                }
                self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_ENTRY_FILL_VISIBILITY_LAG_BLOCK", {
                    "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period,
                    "coin": coin, "order_id": lifecycle.order_id, "official_fill": fill,
                    "action": "new_buy_refused_until_account_inventory_or_protective_sell_visible",
                })
                return LiveExecutionResult("blocked", "official buy fill pending account reconciliation", lifecycle.order_id)
            cancel_reason = self.entry_lifecycle_store.latest_cancel_intent_reason(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id,
                coin=coin, order_id=lifecycle.order_id,
            )
            if (
                lifecycle.state == "RECONCILE_REQUIRED"
                and cancel_reason is not None
                and self._resolve_absent_entry_after_prior_cancel(
                    market=market, lifecycle=lifecycle, cancel_reason=cancel_reason,
                )
            ):
                # The fresh authoritative reads below prove this was a
                # cancelled order rather than a delayed fill.  The lifecycle
                # is terminal now; do not let it poison future admission.
                continue
            # A durably owned entry disappearing without cancellation or fill
            # evidence is likewise unsafe to overwrite with another BUY.
            if lifecycle.state != "RECONCILE_REQUIRED":
                self.entry_lifecycle_store.record(
                    lifecycle, reason="owned_entry_absent_without_terminal_evidence",
                    extra={"state": "RECONCILE_REQUIRED"},
                )
            admission["entry_fill_visibility_gate"] = {
                "allowed": False, "reason": "owned_entry_absent_without_terminal_evidence",
                "coin": coin, "order_id": lifecycle.order_id,
            }
            return LiveExecutionResult("blocked", "owned entry absent without terminal reconciliation evidence", lifecycle.order_id)
        return None

    def _resolve_absent_entry_after_prior_cancel(
        self, *, market: OutcomeMarketSpec, lifecycle: OutcomeEntryLifecycle, cancel_reason: str,
    ) -> bool:
        """Terminally reconcile one cancelled owned BUY from fresh account truth.

        This is intentionally *not* a generic absent-order escape hatch.  It
        runs only after a durable ``CANCEL_SUBMITTED`` for the same order, and
        requires a cache-bypassing open-order read plus fresh balance and fill
        reads to all be absent.  Any exception or contradictory evidence keeps
        the lifecycle blocked.
        """
        if self.entry_lifecycle_store is None or self.ledger is None:
            return False
        key = (market.outcome_id, lifecycle.coin, lifecycle.order_id)
        now = time.monotonic()
        last = self._last_absent_entry_terminal_reconcile_at.get(key, float("-inf"))
        if now - last < 15.0:
            return False
        self._last_absent_entry_terminal_reconcile_at[key] = now
        try:
            # ``reconcile()`` above may have used a same-tick cache or a
            # cross-validated WS view.  Terminalising a durable lifecycle
            # requires a fresh REST read instead.
            self._account_reads.invalidate()
            balance_snapshot = self._account_reads.get_spot_clearinghouse_state_sync(self.recovery.wallet)
            orders = self._account_reads.force_open_orders_reconciliation_sync(self.recovery.wallet)
            fills = self._account_reads.get_user_fills_sync(self.recovery.wallet)
            if not isinstance(balance_snapshot, dict) or not isinstance(orders, list) or not isinstance(fills, list):
                return False
            self.ledger.sync_fills(fills=fills, market_key=str(market.outcome_id), period=market.period)
            normalized_coin = normalize_outcome_coin(lifecycle.coin)
            inventory = sum(
                (
                    Decimal(str(row.get("total", "0")))
                    for row in balance_snapshot.get("balances", [])
                    if isinstance(row, dict) and normalize_outcome_coin(row.get("coin")) == normalized_coin
                ),
                Decimal("0"),
            )
            coin_orders = [
                row for row in orders
                if isinstance(row, dict) and normalize_outcome_coin(row.get("coin")) == normalized_coin
            ]
            matching_fill = any(
                isinstance(row, dict) and str(row.get("oid") or "") == lifecycle.order_id
                for row in fills
            )
            if inventory > 0 or coin_orders or matching_fill:
                return False
        except Exception:
            # This is a read-only safety boundary: a transport, schema, or
            # journal-sync failure is never evidence that a BUY was cancelled.
            return False

        self.entry_lifecycle_store.record(
            lifecycle, reason=cancel_reason,
            extra={
                "state": "CANCELLED",
                "terminal_reconciliation": "fresh_account_truth_after_prior_cancel",
                "fresh_inventory": "0",
                "fresh_open_orders_for_coin": 0,
                "fresh_matching_user_fill": False,
            },
        )
        self.ledger.journal.log_order_event(
            self.ledger.run_id, "ORDER_CANCEL", venue_order_id=lifecycle.order_id,
            side="BUY", status="CANCELLED", instrument_id=lifecycle.coin,
            reason="entry_absent_after_prior_cancel_and_fresh_account_reconciliation",
            payload={
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
                "coin": lifecycle.coin, "prior_cancel_reason": cancel_reason,
                "terminal_reconciliation": "fresh_account_truth_after_prior_cancel",
            },
        )
        return True

    def _resolve_holding_entry_provenance(
        self, *, market: OutcomeMarketSpec, coin: str, inventory: Decimal, fill_vwap: Decimal,
    ) -> dict[str, object] | None:
        """Bind an open holding only to its exact official BUY fill.

        The report must never accidentally combine two same-coin round trips.
        Current live safety permits one inventory, but partial/multi-fill
        history can still be ambiguous after a restart; such a path remains
        unlabelled rather than being assigned to the latest strategy event.
        """
        return self.journal_view.exact_holding_provenance(market, coin, inventory, fill_vwap)

    def _update_holding_reversal(
        self, *, market: OutcomeMarketSpec, coin: str, side_index: int,
        vwap: Decimal, bid: Decimal, ask: Decimal, evidence: dict[str, object], book_source: str,
        holding_audit: dict[str, object] | None = None,
    ) -> str:
        def _decimal(name: str) -> Decimal | None:
            try:
                value = evidence.get(name)
                return Decimal(str(value)) if value is not None else None
            except (ValueError, ArithmeticError):
                return None

        spot_bps = _decimal("spot_strike_bps")
        mark_bps = _decimal("mark_return_bps")
        oi_bps = _decimal("oi_return_bps")
        opposite = False
        if spot_bps is not None and mark_bps is not None and oi_bps is not None:
            direction = Decimal("1") if side_index == 0 else Decimal("-1")
            opposite = direction * spot_bps < 0 and direction * mark_bps < 0 and oi_bps > 0
        key = (market.outcome_id, coin)
        self._opposite_observation_counts[key] = self._opposite_observation_counts.get(key, 0) + 1 if opposite else 0
        try:
            oi_age_ms = int(evidence.get("oi_age_ms"))
        except (TypeError, ValueError):
            oi_age_ms = -1
        context_fresh = 0 <= oi_age_ms <= 90_000
        decision = self.reversal_classifier.classify(OutcomeReversalInput(
            side_index, vwap, bid, ask, spot_bps, mark_bps, oi_bps, context_fresh,
            self._opposite_observation_counts[key],
        ))
        now = time.time()
        if decision.state.value == "REVERSAL_CONFIRMED":
            first_ts, last_independent_ts, count = self._emergency_reversal_windows.get(key, (now, 0.0, 0))
            if last_independent_ts <= 0 or now - last_independent_ts >= 60.0:
                count += 1
                last_independent_ts = now
            self._emergency_reversal_windows[key] = (first_ts, last_independent_ts, count)
        else:
            self._emergency_reversal_windows.pop(key, None)
        if self.ledger is not None:
            self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_REVERSAL_SHADOW_DECISION", {
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period,
                "coin": coin, "state": decision.state, "reason": decision.reason,
                "book_source": book_source, "oi_context_fresh": context_fresh,
                "emergency_independent_observations": self._emergency_reversal_windows.get(key, (0.0, 0.0, 0))[2],
                # These fields are research/audit only.  They deliberately do
                # not influence the existing S2/S3 execution policy.
                "entry_lifecycle_id": (holding_audit or {}).get("entry_lifecycle_id"),
                "entry_trade_id": (holding_audit or {}).get("entry_trade_id"),
                "holding_age_sec": (holding_audit or {}).get("holding_age_sec"),
                "time_left_sec": (holding_audit or {}).get("time_left_sec"),
                "entry_side_index": (holding_audit or {}).get("entry_side_index"),
                "entry_tier": (holding_audit or {}).get("entry_tier"),
                "entry_target_return_pct": (holding_audit or {}).get("entry_target_return_pct"),
                "execution_submitted": False,
            })
        if decision.state.value == "REVERSAL_CONFIRMED" and self.exit_lifecycle_store is not None:
            lifecycle = self.exit_lifecycle_store.recover(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            )
            if lifecycle is not None:
                self.exit_lifecycle_store.record(
                    lifecycle, reason="reversal_classifier_shadow_confirmed",
                    extra={"state": "REVERSAL_CONFIRMED"},
                )
        return str(decision.state.value)

    def _observe_holding_reversal_ws(self, *, market: OutcomeMarketSpec, finding: object) -> bool:
        """Update the risk classifier from healthy WS BBO without waiting for research REST capture."""
        if self.stream_health is None:
            return False
        coin = str(getattr(finding, "coin", ""))
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        book_top = self.stream_health.fresh_book_top(market, coin)
        if inventory <= 0 or vwap is None or book_top is None:
            return False
        side_index = 0 if coin == market.yes_coin else 1
        evidence = dict(self._holding_context.get(market.outcome_id, {}))
        bid, ask = Decimal(str(book_top["bid"])), Decimal(str(book_top["ask"]))
        provenance = self._resolve_holding_entry_provenance(
            market=market, coin=coin, inventory=inventory, fill_vwap=vwap,
        ) or {}
        lifecycle_id = str(provenance.get("entry_lifecycle_id") or "")
        try:
            holding_age_sec = max(0.0, time.time() - datetime.fromisoformat(str(provenance["entry_filled_at"])).timestamp())
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            holding_age_sec = None
        holding_audit = {
            **provenance, "holding_age_sec": holding_age_sec,
            "time_left_sec": market.time_to_expiry_sec(),
        }
        reversal_state = self._update_holding_reversal(
            market=market, coin=coin, side_index=side_index, vwap=vwap,
            bid=bid, ask=ask, evidence=evidence, book_source="fresh_ws_bbo", holding_audit=holding_audit,
        )
        # An enabled episode budget closes only after a durable recovery fact:
        # the executable best bid has returned within 2% of entry and the
        # thesis monitor is no longer confirmed adverse.  This never creates
        # an order; it merely lets a later, genuinely separate deterioration
        # receive its own bounded existing-controller budget.
        if (
            self.risk_episode_store is not None
            and bid / vwap - Decimal("1") >= Decimal("-0.02")
            and reversal_state != "REVERSAL_CONFIRMED"
        ):
            self.risk_episode_store.close_recovered(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
                reason="ws_executable_bid_recovered_with_no_confirmed_reversal",
            )
        if lifecycle_id and self.ledger is not None:
            def _evidence_decimal(name: str) -> Decimal | None:
                try:
                    value = evidence.get(name)
                    return Decimal(str(value)) if value is not None else None
                except (ArithmeticError, TypeError, ValueError):
                    return None
            try:
                oi_age_ms = int(evidence.get("oi_age_ms"))
            except (TypeError, ValueError):
                oi_age_ms = None
            crash = self.crash_circuit_shadow.observe(OutcomeCrashCircuitObservation(
                lifecycle_id=lifecycle_id, outcome_id=market.outcome_id, period=market.period, coin=coin,
                timestamp=time.time(), fill_vwap=vwap, bid=bid, ask=ask,
                top3_bid_depth=Decimal(str(book_top["top3_bid_depth"])),
                spot_strike_bps=_evidence_decimal("spot_strike_bps"),
                mark_return_bps=_evidence_decimal("mark_return_bps"),
                oi_return_bps=_evidence_decimal("oi_return_bps"), oi_age_ms=oi_age_ms,
                regime_state=str((self._latest_regime_shadow.get(market.outcome_id) or {}).get("state") or "UNKNOWN"),
                reversal_state=reversal_state,
                holding_age_sec=holding_age_sec, time_left_sec=market.time_to_expiry_sec(),
                entry_side_index=provenance.get("entry_side_index"), entry_tier=provenance.get("entry_tier"),
                entry_target_return_pct=provenance.get("entry_target_return_pct"),
            ))
            state = str(crash.get("research_state") or "UNKNOWN")
            previous = self._last_crash_shadow_record.get(lifecycle_id)
            now = time.time()
            # Retain every transition immediately, then a compact ten-second
            # cadence.  This is enough for 15/30/60 second velocity features
            # while avoiding another multi-GB raw-book-style journal stream.
            if previous is None or previous[0] != state or now - previous[1] >= self._CRASH_SHADOW_MIN_INTERVAL_SEC:
                self.ledger.journal.log_strategy_event(
                    self.ledger.run_id, "OUTCOME_CRASH_CIRCUIT_SHADOW", crash,
                )
                self._last_crash_shadow_record[lifecycle_id] = (state, now)
            # Parallel market-risk monitor: it only turns WS observations into
            # a prepared/shadow authorization.  It deliberately does not
            # receive the gateway or any mutation primitive.
            monitor = self.market_risk_monitor.observe(OutcomeMarketRiskObservation(
                lifecycle_id=lifecycle_id, outcome_id=market.outcome_id, coin=coin, period=market.period,
                timestamp=now, entry_price=vwap, position_size=inventory, best_bid=bid, best_ask=ask,
                top1_depth=Decimal(str(book_top.get("top1_bid_depth", book_top["top3_bid_depth"]))),
                top3_depth=Decimal(str(book_top["top3_bid_depth"])), side_index=side_index,
                time_left_sec=market.time_to_expiry_sec(), entry_time_left_sec=provenance.get("entry_time_left_sec"),
                spot_strike_bps=_evidence_decimal("spot_strike_bps"), mark_return_bps=_evidence_decimal("mark_return_bps"),
                oi_return_bps=_evidence_decimal("oi_return_bps"), oi_age_ms=oi_age_ms,
                reversal_state=reversal_state,
                independent_confirmation_count=self._emergency_reversal_windows.get((market.outcome_id, coin), (0.0, 0.0, 0))[2],
            ))
            monitor_state = str(monitor["state"])
            prior_monitor = self._last_market_risk_record.get(lifecycle_id)
            if prior_monitor is None or prior_monitor[0] != monitor_state or now - prior_monitor[1] >= self._CRASH_SHADOW_MIN_INTERVAL_SEC:
                self.ledger.journal.log_strategy_event(
                    self.ledger.run_id, "OUTCOME_MARKET_RISK_MONITOR_SHADOW", monitor,
                )
                self._last_market_risk_record[lifecycle_id] = (monitor_state, now)
            # Structural-collapse evidence is a separate, public-data-only
            # forward-validation stream.  Its output is journaled solely as
            # research and is never passed to any execution owner.
            if self.structural_collapse_shadow is not None:
                try:
                    entry_filled_at = datetime.fromisoformat(str(provenance["entry_filled_at"])).timestamp()
                except (KeyError, TypeError, ValueError, OSError, OverflowError):
                    # The observer is lifecycle-bound; a submit-time or
                    # guessed timestamp would reintroduce pre-entry leakage.
                    entry_filled_at = None
                if entry_filled_at is None:
                    return True
                structural = self.structural_collapse_shadow.evaluate(
                    lifecycle_id=lifecycle_id, outcome_id=market.outcome_id, period=market.period,
                    held_coin=coin, yes_coin=market.yes_coin, no_coin=market.no_coin,
                    now=now, entry_filled_at=entry_filled_at,
                    position_size=inventory, entry_price=vwap,
                )
                state = str(structural.get("state")) + ":" + ",".join(
                    str(item) for item in structural.get("candidate_branches", ())
                )
                previous = self._last_structural_collapse_shadow_record.get(lifecycle_id)
                if previous is None or previous[0] != state or now - previous[1] >= 30.0:
                    self._log_best_effort_strategy_event("OUTCOME_STRUCTURAL_COLLAPSE_SHADOW", structural)
                    self._last_structural_collapse_shadow_record[lifecycle_id] = (state, now)
        return True

    def _record_holding_risk_decision_shadow(
        self, *, market: OutcomeMarketSpec, coin: str, lifecycle_id: str,
        holding_age_sec: float, time_left_sec: float, lane: dict[str, object],
    ) -> None:
        """Persist one compact three-lane counterfactual at hard-risk boundaries.

        This consumes only the full-depth snapshot already fetched for the
        existing holding-path recorder.  It has no controller reference and
        cannot change the narrow canary, a protective SELL, or entry policy.
        """
        if self.ledger is None:
            return
        state = str(lane.get("state") or "UNKNOWN")
        if state not in {"HARD_CANDIDATE_WITHIN_CAP", "HARD_CANDIDATE_DEPTH_OR_CAP_BLOCKED"}:
            return
        shape = lane.get("recovery_shape") if isinstance(lane.get("recovery_shape"), dict) else {}
        actions = lane.get("counterfactual_actions") if isinstance(lane.get("counterfactual_actions"), dict) else {}
        hard = actions.get("hard_ioc") if isinstance(actions.get("hard_ioc"), dict) else {}
        warning = actions.get("warning_only") if isinstance(actions.get("warning_only"), dict) else {}
        hold = actions.get("hold_for_recovery") if isinstance(actions.get("hold_for_recovery"), dict) else {}
        fingerprint = "|".join((
            state, str(shape.get("classification") or "unknown"),
            str(hard.get("action") or "unknown"), str(warning.get("action") or "unknown"),
            str(hold.get("action") or "unknown"),
        ))
        now = time.monotonic()
        previous = self._last_holding_risk_decision_record.get(lifecycle_id)
        if previous is not None and previous[0] == fingerprint and now - previous[1] < self._HOLDING_PATH_MIN_INTERVAL_SEC:
            return
        self._log_best_effort_strategy_event("OUTCOME_HOLDING_RISK_DECISION_SHADOW", {
            "schema_version": 1, "read_only": True, "live_authority": False,
            "execution_submitted": False, "outcome_id": market.outcome_id,
            "period": market.period, "coin": coin, "entry_lifecycle_id": lifecycle_id,
            "holding_age_sec": holding_age_sec, "time_left_sec": time_left_sec,
            "hard_candidate_state": state,
            "full_depth_net_return_pct": lane.get("full_depth_net_return_pct"),
            "within_minus_15pct_cap": lane.get("within_minus_15pct_cap"),
            "recovery_shape": shape,
            "counterfactual_actions": actions,
            "promotion_boundary": {
                "hold_veto_live_authorized": False,
                "warning_lane_live_authorized": False,
                "existing_narrow_canary_unchanged": True,
            },
            "limits": [
                "no live decision changes", "no cancel/reprice/IOC authority",
                "hold option cannot veto existing safety lanes",
            ],
        })
        self._last_holding_risk_decision_record[lifecycle_id] = (fingerprint, now)

    def _capture_holding_path(self, *, market: OutcomeMarketSpec, finding: object,
                              update_reversal: bool = True) -> None:
        """Persist as-of open-inventory facts; never changes an order decision."""
        if self.holding_path_recorder is None or self.ledger is None:
            return
        coin = str(getattr(finding, "coin", ""))
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        fee = self._persisted_p3_maker_fee(market=market, coin=coin)
        if inventory <= 0 or vwap is None or fee is None:
            return
        side_index = 0 if coin == market.yes_coin else 1
        try:
            fresh_book = self._fresh_book_once(market=market, side_index=side_index)
            top = self._top_of_book(fresh_book)
            if top is None:
                return
            bid, ask = top
            if not Decimal("0") < bid < ask < Decimal("1"):
                return
            age = 0.0
            age_basis = "unavailable"
            evidence: dict[str, object] = dict(self._holding_context.get(market.outcome_id, {}))
            entry = self.journal_view.latest_strategy_entry(market, coin)
            if entry:
                _, payload = entry
                if not evidence:
                    evidence = payload.get("entry_evidence") if isinstance(payload.get("entry_evidence"), dict) else {}
            provenance = self._resolve_holding_entry_provenance(
                market=market, coin=coin, inventory=inventory, fill_vwap=vwap,
            ) or {}
            try:
                age = max(0.0, time.time() - datetime.fromisoformat(str(provenance["entry_filled_at"])).timestamp())
                age_basis = str(provenance.get("entry_filled_at_source") or "legacy_journal_fill_timestamp")
            except (KeyError, TypeError, ValueError):
                # Keep an unbound fallback observation for operational
                # visibility, but label it so the lifecycle report excludes
                # it rather than treating submit time as a fill time.
                if entry:
                    age = max(0.0, time.time() - datetime.fromisoformat(str(entry[0])).timestamp())
                    age_basis = "entry_submit_fallback_unbound"
            remaining = inventory
            marketable_notional = Decimal("0")
            marketable_depth = Decimal("0")
            for level in fresh_book.get("bids", ()):
                try:
                    level_price = Decimal(str(level.get("price", level.get("px"))))
                    level_size = Decimal(str(level.get("size", level.get("sz"))))
                except (AttributeError, TypeError, ValueError, ArithmeticError):
                    continue
                if level_price <= 0 or level_size <= 0:
                    continue
                taken = min(remaining, level_size)
                marketable_notional += taken * level_price
                marketable_depth += taken
                remaining -= taken
                if remaining <= 0:
                    break
            marketable_vwap = marketable_notional / inventory if remaining <= 0 and inventory > 0 else None
            taker_fee = self._persisted_taker_fee(market=market, coin=coin)
            self.holding_path_recorder.record(OutcomeHoldingPathObservation(
                market.outcome_id, market.period, coin, inventory, vwap, bid, ask, fee, age,
                market.time_to_expiry_sec(), "fresh_rest_book", evidence,
                holding_age_basis=age_basis,
                marketable_exit_vwap=marketable_vwap,
                marketable_exit_depth_shares=marketable_depth,
                taker_close_fee_rate=taker_fee,
                **provenance,
            ))
            lifecycle_id = str(provenance.get("entry_lifecycle_id") or "")
            if lifecycle_id:
                if self.structural_collapse_shadow is not None:
                    self.structural_collapse_shadow.observe_exitability(
                        lifecycle_id=lifecycle_id, timestamp=time.time(), inventory=inventory,
                        executable_vwap=marketable_vwap, taker_fee_rate=taker_fee,
                    )
                lane = self.market_risk_monitor.assess_full_depth(
                    lifecycle_id=lifecycle_id, timestamp=time.time(), entry_price=vwap,
                    full_inventory_vwap=marketable_vwap, full_inventory=marketable_vwap is not None,
                    taker_close_fee_rate=taker_fee,
                )
                lane_state = str(lane.get("state") or "UNKNOWN")
                previous_lane = self._last_fast_failure_lane_record.get(lifecycle_id)
                now = time.time()
                if previous_lane is None or previous_lane[0] != lane_state or now - previous_lane[1] >= self._HOLDING_PATH_MIN_INTERVAL_SEC:
                    self.ledger.journal.log_strategy_event(
                        self.ledger.run_id, "OUTCOME_FAST_FAILURE_LANE_SHADOW", {
                            **lane, "schema_version": 1, "period": market.period,
                            "outcome_id": market.outcome_id, "coin": coin,
                            "entry_lifecycle_id": lifecycle_id, "holding_age_sec": age,
                            "time_left_sec": market.time_to_expiry_sec(),
                        },
                    )
                    self._last_fast_failure_lane_record[lifecycle_id] = (lane_state, now)
                self._record_holding_risk_decision_shadow(
                    market=market, coin=coin, lifecycle_id=lifecycle_id,
                    holding_age_sec=age, time_left_sec=market.time_to_expiry_sec(), lane=lane,
                )
            # B5 observes the same already-fetched full-depth book.  It never
            # creates an order and therefore adds no REST or SDK call.
            live_context = dict(evidence)
            if side_index == 0:
                live_context.update({
                    "yes_best_bid": str(bid), "yes_best_ask": str(ask),
                    "no_best_bid": str(Decimal("1") - ask), "no_best_ask": str(Decimal("1") - bid),
                })
            else:
                live_context.update({
                    "no_best_bid": str(bid), "no_best_ask": str(ask),
                    "yes_best_bid": str(Decimal("1") - ask), "yes_best_ask": str(Decimal("1") - bid),
                })
            marketable_net = marketable_vwap * (Decimal("1") - taker_fee) if marketable_vwap is not None and taker_fee is not None else None
            toxic = self._last_toxic_shadow_record.get((market.outcome_id, coin))
            challenger = self.active_challenger_shadow.evaluate_holding(
                context=live_context,
                time_left_sec=market.time_to_expiry_sec(),
                side_index=side_index,
                fill_vwap=vwap,
                marketable_net_exit_price=marketable_net,
                regime=self._latest_regime_shadow.get(market.outcome_id),
                toxic_state=toxic[0] if toxic is not None else None,
            )
            challenger.update({
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
                "period": market.period, "coin": coin,
                "entry_lifecycle_id": provenance.get("entry_lifecycle_id"),
                "holding_age_sec": age,
                # Exact full-depth evidence is duplicated beside the B5
                # decision so a profit-lock report can audit its action
                # without assuming the top BBO represents the whole holding.
                "full_depth_execution": {
                    "inventory": str(inventory), "fill_vwap": str(vwap),
                    "marketable_exit_vwap": str(marketable_vwap) if marketable_vwap is not None else None,
                    "marketable_exit_depth_shares": str(marketable_depth),
                    "marketable_exit_full_inventory": marketable_vwap is not None,
                    "taker_close_fee_rate": str(taker_fee) if taker_fee is not None else None,
                    "marketable_net_exit_price": str(marketable_net) if marketable_net is not None else None,
                    "marketable_net_exit_vs_entry_pct": (
                        str(marketable_net / vwap - Decimal("1")) if marketable_net is not None else None
                    ),
                },
            })
            self.ledger.journal.log_strategy_event(
                self.ledger.run_id, "OUTCOME_ACTIVE_HOLDING_CHALLENGER_SHADOW", challenger,
            )
            if update_reversal:
                self._update_holding_reversal(
                    market=market, coin=coin, side_index=side_index, vwap=vwap,
                    bid=bid, ask=ask, evidence=evidence, book_source="fresh_rest_book",
                    holding_audit={
                        **provenance, "holding_age_sec": age,
                        "time_left_sec": market.time_to_expiry_sec(),
                    },
                )
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
            return

    def _capture_due_exit_continuations(self, *, market: OutcomeMarketSpec) -> None:
        """Capture fixed post-IOC checkpoints without changing a decision."""
        observer = self.exit_continuation_observer
        if observer is None:
            return
        try:
            for continuation, target in observer.due(outcome_id=market.outcome_id):
                book = self._fresh_book_once(market=market, side_index=continuation.side_index)
                top = self._top_of_book(book)
                if top is None:
                    continue
                bid, ask = top
                remaining, notional, depth = continuation.inventory, Decimal("0"), Decimal("0")
                for level in book.get("bids", ()):
                    px, size = Decimal(str(level.get("price", level.get("px")))), Decimal(str(level.get("size", level.get("sz"))))
                    if px > 0 and size > 0:
                        taken = min(remaining, size)
                        notional += px * taken
                        depth += taken
                        remaining -= taken
                        if remaining <= 0:
                            break
                vwap = notional / continuation.inventory if remaining <= 0 else None
                observer.record(continuation=continuation, target_sec=target, best_bid=bid, best_ask=ask,
                                marketable_vwap=vwap, depth_shares=depth)
        except Exception:
            # This is strictly post-exit research.  A transient journal or
            # book failure must never alter entry/exit authority or delay a
            # protective decision on the next tick.
            return

    def _live_entry_age_sec(self, *, market: OutcomeMarketSpec, coin: str) -> float | None:
        return self.journal_view.live_entry_age_sec(market, coin)

    def _official_holding_age_sec(
        self, *, market: OutcomeMarketSpec, coin: str, inventory: Decimal, fill_vwap: Decimal | None,
    ) -> float | None:
        """Use exact official fill time for an early marketable-exit authority.

        Unlike legacy S3, fast-failure must not be advanced by time spent as a
        resting BUY.  Missing or ambiguous official provenance is fail-closed.
        """
        if fill_vwap is None:
            return None
        provenance = self._resolve_holding_entry_provenance(
            market=market, coin=coin, inventory=inventory, fill_vwap=fill_vwap,
        )
        try:
            if provenance is None or provenance.get("entry_filled_at_source") != "official_fill_timestamp_ms":
                return None
            return max(0.0, time.time() - datetime.fromisoformat(str(provenance["entry_filled_at"])).timestamp())
        except (TypeError, ValueError, KeyError):
            return None

    def _narrow_hard_failure_candidate(
        self, *, market: OutcomeMarketSpec, coin: str, inventory: Decimal, fill_vwap: Decimal | None,
    ) -> dict[str, object] | None:
        """Bind a live canary only to its exact WS-observed entry lifecycle.

        The monitor remains a decision producer: it has no execution object.
        This adapter refuses unbound holdings and only returns a very recent
        two-signal, ten-second-persistent shadow qualification.  The holding
        service then re-reads full L2 and independently validates the cap.
        """
        if fill_vwap is None:
            return None
        provenance = self._resolve_holding_entry_provenance(
            market=market, coin=coin, inventory=inventory, fill_vwap=fill_vwap,
        )
        lifecycle_id = str((provenance or {}).get("entry_lifecycle_id") or "")
        if not lifecycle_id:
            return None
        return self.market_risk_monitor.latest_persistent_lane(
            lifecycle_id=lifecycle_id, now=time.time(), max_age_sec=15.0,
        )

    def tick_market(self, *, market: OutcomeMarketSpec, entry_side_index: int | None) -> LiveExecutionResult:
        """Advance existing exposure first; only a flat market accepts a signal."""
        self._begin_tick()
        if not self.enabled():
            return LiveExecutionResult("disabled", "automated execution requires OUTCOME_AUTOMATED_EXECUTION_ENABLED=1 and OUTCOME_SDK_EXECUTION_ENABLED=1")
        report = self.recovery.reconcile([market])
        self.exit_recovery_service.reconcile(market=market, report=report)
        self._capture_due_exit_continuations(market=market)
        exit_ambiguity = self.exit_recovery_service.ambiguity_barrier(market=market)
        if exit_ambiguity is not None:
            return exit_ambiguity
        active = [finding for finding in report.findings if finding.state != "flat"]
        if len(active) == 1:
            self._capture_holding_path(market=market, finding=active[0])
            requote = self.exit_requote_service.maybe_requote(market=market, finding=active[0])
            if requote is not None:
                return requote
            persisted_exit = self.holding_execution_service.advance_persisted_exit(market=market, finding=active[0])
            if persisted_exit is not None:
                return persisted_exit
        if not report.safe_for_new_entry:
            return LiveExecutionResult("blocked", f"account recovery blocked execution: {report.reason}")
        if active:
            if len(active) != 1:
                return LiveExecutionResult("blocked", "multiple live Outcome sides require explicit reconciliation")
            side_index = 0 if active[0].coin == market.yes_coin else 1
            result = self.machine.tick(market=market, side_index=side_index, entry_permitted=False)
        elif entry_side_index is None:
            return LiveExecutionResult("flat", "no live exposure and no entry signal")
        else:
            # This generic method has no exit-policy parameters.  It may keep
            # observing/reconciling existing orders, but cannot create a buy
            # that would later be forced into a best-ask fallback sell.
            return LiveExecutionResult("blocked", "generic live entry has no explicit verified exit policy; use a dedicated policy runtime")
        side_index = 0 if result.state == "flat" and entry_side_index is None else (side_index if active else entry_side_index)
        assert side_index is not None
        return self._record(market, side_index, result)

    def tick_p3_calibration(self, *, market: OutcomeMarketSpec) -> LiveExecutionResult:
        """Advance one explicit P3 sampling lifecycle without a directional strategy.

        It can only be enabled by a third operator gate.  Existing inventory is
        always handled first; a new entry is one first-level ALO bid on the
        feasible side with the higher market midpoint consensus, subject to the daily cap and all normal
        account/stream/risk checks.  This intentionally bypasses *research*
        readiness because it is collecting the missing P3 evidence, not using
        it for strategy trading.
        """
        self._begin_tick()
        if not self.calibration_enabled():
            return LiveExecutionResult("disabled", "P3 calibration requires automated, SDK, and OUTCOME_P3_CALIBRATION_ENABLED=1 gates")
        if self.ledger is None:
            return LiveExecutionResult("blocked", "P3 calibration requires an execution ledger")
        health_error = self._stream_ready(market)
        if health_error:
            return health_error
        report = self.recovery.reconcile([market])
        self.exit_recovery_service.reconcile(market=market, report=report)
        self._capture_due_exit_continuations(market=market)
        exit_ambiguity = self.exit_recovery_service.ambiguity_barrier(market=market)
        if exit_ambiguity is not None:
            return exit_ambiguity
        config = OutcomeP3CalibrationConfig.from_env()
        active = [finding for finding in report.findings if finding.state != "flat"]
        if len(active) == 1:
            self._capture_holding_path(market=market, finding=active[0])
            requote = self.exit_requote_service.maybe_requote(market=market, finding=active[0])
            if requote is not None:
                return requote
            persisted_exit = self.holding_execution_service.advance_persisted_exit(market=market, finding=active[0])
            if persisted_exit is not None:
                return persisted_exit
        if not report.safe_for_new_entry:
            return LiveExecutionResult("blocked", f"account recovery blocked calibration: {report.reason}")
        if active:
            if len(active) != 1:
                return LiveExecutionResult("blocked", "multiple live Outcome sides require explicit reconciliation")
            side_index = 0 if active[0].coin == market.yes_coin else 1
            fees = self.recovery.account.get_user_fees_sync(self.recovery.wallet)
            maker_close_fee = Decimal(str(fees["userSpotAddRate"]))
            result = self.machine.tick(
                market=market, side_index=side_index, entry_permitted=False,
                minimum_return_pct=config.target_return_pct, maker_close_fee_rate=maker_close_fee,
                loss_reprice_pct=config.loss_reprice_pct,
            )
            return self._record(market, side_index, result)
        if self._daily_calibration_entries() >= config.max_daily_entries:
            return LiveExecutionResult("flat", f"P3 calibration daily entry cap reached ({config.max_daily_entries})")
        fees = self.recovery.account.get_user_fees_sync(self.recovery.wallet)
        maker_close_fee = Decimal(str(fees["userSpotAddRate"]))
        books = {
            0: self.machine.gateway.fetch_order_book(market=market, side_index=0),
            1: self.machine.gateway.fetch_order_book(market=market, side_index=1),
        }
        bids = {
            side: Decimal(str(book["bids"][0]["price"]))
            for side, book in books.items() if book.get("bids")
        }
        mids = {
            side: (Decimal(str(book["bids"][0]["price"])) + Decimal(str(book["asks"][0]["price"]))) / Decimal("2")
            for side, book in books.items() if book.get("bids") and book.get("asks")
        }
        side_index = choose_consensus_calibration_side(
            mids=mids, entry_bids=bids,
            target_return_pct=config.target_return_pct, maker_close_fee_rate=maker_close_fee,
            tie_breaker=market.outcome_id,
        )
        if side_index is None:
            return LiveExecutionResult("flat", "no side can support the configured net take-profit below Outcome price ceiling")
        price = bids[side_index]
        shares = whole_share_size(price)
        account = self.recovery.account
        risk = self.risk_gate.evaluate(
            balances=account.get_spot_clearinghouse_state_sync(self.recovery.wallet).get("balances", []),
            open_orders=account.get_open_orders_sync(self.recovery.wallet), price=price, shares=shares,
        )
        if not risk.allowed:
            return LiveExecutionResult("blocked", f"risk gate: {risk.reason}")
        result = self.machine.tick(market=market, side_index=side_index, entry_permitted=True)
        if result.state == "buy_placed":
            self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_P3_CALIBRATION_ENTRY_PLACED", {
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period,
                "side_index": side_index, "coin": self.machine.gateway.outcome_coin(market, side_index),
                "price": str(price), "shares": shares, "target_return_pct": str(config.target_return_pct),
                "loss_reprice_pct": str(config.loss_reprice_pct), "maker_close_fee_rate": str(maker_close_fee),
                "order_id": result.order_id,
                "sampling_policy": "market_mid_consensus", "directional_signal_used": False,
            })
        return self._record(market, side_index, result)

    def tick_live_strategy(self, *, market: OutcomeMarketSpec, entry_side_index: int | None,
                           entry_reason: str, entry_evidence: dict[str, object],
                           retiring_markets: tuple[OutcomeMarketSpec, ...] = (),
                           market_context: dict[str, object] | None = None,
                           reduce_only: bool = False) -> LiveExecutionResult:
        """Run S0 and durably record its final admission or rejection reason."""
        self._begin_tick()
        # Preserve the upstream S0 evaluation time across the potentially
        # slower recovery/holding stages.  A decision that predates an owned
        # TP fill must not be allowed to reopen the same exposure afterwards.
        entry_evidence = dict(entry_evidence)
        entry_evidence.setdefault("decision_observed_at_ms", int(time.time() * 1000))
        started_at = time.monotonic()
        admission: dict[str, object] = {}
        result = self._tick_live_strategy(
            market=market, entry_side_index=entry_side_index, entry_reason=entry_reason,
            entry_evidence=entry_evidence, retiring_markets=retiring_markets,
            market_context=market_context, admission=admission, reduce_only=reduce_only,
        )
        admission["timing_runtime_before_journal_ms"] = round((time.monotonic() - started_at) * 1000, 3)
        admission["reduce_only"] = reduce_only
        journal_started_at = time.monotonic()
        self._record_entry_admission_decision(
            market=market, entry_side_index=entry_side_index, entry_reason=entry_reason,
            entry_evidence=entry_evidence, admission=admission, result=result,
        )
        journal_write_ms = round((time.monotonic() - journal_started_at) * 1000, 3)
        total_ms = round((time.monotonic() - started_at) * 1000, 3)
        # The outer loop historically showed long tails whose stages did not
        # add up.  Keep an explicit residual rather than silently assigning
        # it to SQLite or the network.  Only non-overlapping stage timers are
        # included; the aggregate runtime timer itself is excluded.
        timing_stage_names = (
            "timing_account_recovery_ms", "timing_research_shadow_ms", "timing_lifecycle_lookup_ms",
            "timing_fill_sync_ms", "timing_exit_recovery_ms", "timing_exit_continuations_ms",
            "timing_entry_gate_journal_ms", "timing_holding_before_stream_ms", "timing_stream_gate_ms",
            "timing_holding_after_stream_ms", "timing_entry_preflight_ms", "timing_fee_read_ms",
            "timing_entry_book_request_ms", "timing_target_policy_ms", "timing_risk_gate_ms",
            "timing_portfolio_guard_ms", "timing_entry_submit_ms",
        )
        measured_runtime_ms = round(sum(
            float(admission[name]) for name in timing_stage_names
            if isinstance(admission.get(name), (int, float))
        ), 3)
        unattributed_runtime_ms = round(max(0.0, total_ms - journal_write_ms - measured_runtime_ms), 3)
        if self.ledger is not None and (total_ms >= 1000 or result.state in {"buy_placed", "sell_placed", "sell_resting"}):
            self._log_best_effort_strategy_event("OUTCOME_RUNTIME_TIMING", {
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
                "runtime_state": result.state, "total_ms": total_ms,
                "oi_signal_read_ms": entry_evidence.get("timing_oi_read_ms"),
                "oi_signal_read_timeout_sec": entry_evidence.get("oi_read_timeout_sec"),
                "account_recovery_ms": admission.get("timing_account_recovery_ms"),
                "exit_recovery_ms": admission.get("timing_exit_recovery_ms"),
                "exit_continuations_ms": admission.get("timing_exit_continuations_ms"),
                "fill_sync_ms": admission.get("timing_fill_sync_ms"),
                "holding_before_stream_ms": admission.get("timing_holding_before_stream_ms"),
                "stream_gate_ms": admission.get("timing_stream_gate_ms"),
                "holding_after_stream_ms": admission.get("timing_holding_after_stream_ms"),
                "entry_preflight_ms": admission.get("timing_entry_preflight_ms"),
                "lifecycle_lookup_ms": admission.get("timing_lifecycle_lookup_ms"),
                "entry_gate_journal_ms": admission.get("timing_entry_gate_journal_ms"),
                "fee_read_ms": admission.get("timing_fee_read_ms"),
                "research_shadow_ms": admission.get("timing_research_shadow_ms"),
                "research_shadow_stages_ms": admission.get("timing_research_shadow_stages_ms"),
                "book_request_ms": admission.get("timing_entry_book_request_ms"),
                "target_policy_ms": admission.get("timing_target_policy_ms"),
                "risk_gate_ms": admission.get("timing_risk_gate_ms"),
                "portfolio_guard_ms": admission.get("timing_portfolio_guard_ms"),
                "entry_submit_ms": admission.get("timing_entry_submit_ms"),
                "measured_runtime_ms": measured_runtime_ms,
                "unattributed_runtime_ms": unattributed_runtime_ms,
                "journal_write_ms": journal_write_ms,
                "journal_writer_last_ms": dict(getattr(self.ledger.journal, "last_write_timing_ms", {})),
                # These are decision-local ledgers.  Do not replace them with
                # a sidecar-global "last request": that stale value was the
                # source of the prior false latency attribution.
                "account_read_timing": self._account_reads.timing_summary(),
                "sdk_requests": list(getattr(self.machine.gateway, "timing_events", lambda: ())()),
            })
        return result

    def _tick_live_strategy(self, *, market: OutcomeMarketSpec, entry_side_index: int | None,
                            entry_reason: str, entry_evidence: dict[str, object],
                            retiring_markets: tuple[OutcomeMarketSpec, ...],
                            market_context: dict[str, object] | None,
                            admission: dict[str, object], reduce_only: bool = False) -> LiveExecutionResult:
        """Run one explicitly gated S0 live strategy lifecycle.

        The caller supplies a pure, already fail-closed OI/spot decision.  The
        runtime still owns stream health, account recovery, feasibility, risk,
        order submission and durable evidence.
        """
        if not self.live_strategy_enabled():
            admission["execution_gate"] = "live_strategy_disabled"
            return LiveExecutionResult("disabled", "live strategy requires automated, SDK, and OUTCOME_LIVE_STRATEGY_ENABLED gates")
        if self.ledger is None:
            return LiveExecutionResult("blocked", "live strategy requires an execution ledger")
        self._holding_context[market.outcome_id] = dict(market_context or entry_evidence)
        config = OutcomeLiveStrategyConfig.from_env()
        tracked_markets = (market, *retiring_markets)
        recovery_started_at = time.monotonic()
        report = self.recovery.reconcile(tracked_markets)
        admission["timing_account_recovery_ms"] = round((time.monotonic() - recovery_started_at) * 1000, 3)
        admission["account_recovery"] = {
            "safe_for_new_entry": bool(getattr(report, "safe_for_new_entry", False)),
            "reason": str(getattr(report, "reason", "unknown")),
        }
        # This observer is read-only.  Account recovery supplies holding and
        # exit safety, so it must run first when the loop is under pressure.
        research_started_at = time.monotonic()
        research_timings = self.research_supervisor.observe_entry(
            self, market=market, entry_side_index=entry_side_index,
            entry_reason=entry_reason, entry_evidence=entry_evidence,
            market_context=market_context, admission=admission,
        )
        admission["timing_research_shadow_ms"] = round((time.monotonic() - research_started_at) * 1000, 3)
        admission["timing_research_shadow_stages_ms"] = research_timings
        retired_active = [
            finding for finding in report.findings
            if finding.market_id != market.outcome_id and finding.state != "flat"
        ]
        if retired_active:
            admission["rollover_gate"] = "retiring_market_active"
            return LiveExecutionResult(
                "blocked",
                "market rollover pending: retiring Outcome has live inventory or order; new entry refused",
            )
        active = [finding for finding in report.findings if finding.market_id == market.outcome_id and finding.state != "flat"]
        lifecycle_lookup_started_at = time.monotonic()
        pending_owned_entry = not active and self.entry_lifecycle_store is not None and any(
            self.entry_lifecycle_store.recover(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            ) is not None
            for coin in (market.yes_coin, market.no_coin)
        )
        exit_lifecycles = {
            coin: self.exit_lifecycle_store.recover(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            )
            for coin in (market.yes_coin, market.no_coin)
        } if self.exit_lifecycle_store is not None else {}
        pending_owned_exit = any(lifecycle is not None for lifecycle in exit_lifecycles.values())
        pending_ambiguous_exit = self.exit_lifecycle_store is not None and any(
            self.exit_lifecycle_store.pending_ambiguous_submit(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            ) is not None
            for coin in (market.yes_coin, market.no_coin)
        )
        active_by_coin = {str(getattr(finding, "coin", "")): finding for finding in active}
        stable_protective_exit = pending_owned_exit and all(
            lifecycle is not None
            and str(getattr(lifecycle, "state", "")) in {"SELL_RESTING", "LOSS_BAND_RESTING"}
            and (finding := active_by_coin.get(coin)) is not None
            and str(getattr(finding, "state", "")) == "protected_inventory"
            and not tuple(getattr(finding, "buy_order_ids", ()))
            and tuple(getattr(finding, "sell_order_ids", ())) == (str(getattr(lifecycle, "order_id", "")),)
            for coin, lifecycle in exit_lifecycles.items()
            if lifecycle is not None
        ) and not pending_ambiguous_exit
        admission["timing_lifecycle_lookup_ms"] = round((time.monotonic() - lifecycle_lookup_started_at) * 1000, 3)
        # ``userFills`` has a high /info weight.  Account recovery already
        # reads balances and open orders every decision, so a healthy resting
        # BUY or covering SELL does not justify downloading the entire fill
        # window every 1.5 seconds.  A newly unprotected inventory or a
        # disappeared durable entry remains urgent and forces an immediate
        # fill read so protective-sell and ambiguous-submit safety are intact.
        # Stable states use a 30-second audit cadence; research owns its own
        # independent 30-second markout ingestion.
        now_monotonic = time.monotonic()
        if self._should_sync_fills(
            active=active, pending_owned_entry=pending_owned_entry,
            pending_owned_exit=pending_owned_exit, stable_protective_exit=stable_protective_exit,
            pending_ambiguous_exit=pending_ambiguous_exit, now=now_monotonic,
        ):
            fill_sync_started_at = time.monotonic()
            try:
                self.ledger.sync_fills(
                    fills=self.recovery.account.get_user_fills_sync(self.recovery.wallet),
                    market_key=f"outcome:{market.outcome_id}", period=market.period,
                )
                self._last_fill_sync_at = now_monotonic
            except Exception:
                # Account recovery remains the hard safety source; missing
                # fill history means no inferred loss/re-entry transition.
                pass
            finally:
                admission["timing_fill_sync_ms"] = round((time.monotonic() - fill_sync_started_at) * 1000, 3)
        else:
            admission["timing_fill_sync_ms"] = 0.0
        # Do not terminalise a locally owned SELL from a possibly stale
        # account snapshot before its official fill has been synchronized.
        exit_recovery_started_at = time.monotonic()
        self.exit_recovery_service.reconcile(market=market, report=report)
        admission["timing_exit_recovery_ms"] = round((time.monotonic() - exit_recovery_started_at) * 1000, 3)
        continuation_started_at = time.monotonic()
        self._capture_due_exit_continuations(market=market)
        admission["timing_exit_continuations_ms"] = round((time.monotonic() - continuation_started_at) * 1000, 3)
        exit_ambiguity = self.exit_recovery_service.ambiguity_barrier(market=market)
        if exit_ambiguity is not None:
            admission["ambiguous_exit_fence"] = "pending"
            return exit_ambiguity
        admission["active_current_market_count"] = len(active)
        snapshot = OutcomeRuntimeTickSnapshot(
            market=market, report=report, active=tuple(active),
            pending_owned_entry=pending_owned_entry,
            entry_side_index=entry_side_index, entry_reason=entry_reason,
            reduce_only=reduce_only, observed_monotonic=time.monotonic(),
            market_context=dict(market_context or {}),
            entry_decision_at_ms=(
                int(entry_evidence["decision_observed_at_ms"])
                if entry_evidence.get("decision_observed_at_ms") is not None else None
            ),
        )
        self._current_tick_snapshot = snapshot
        gate_journal_started_at = time.monotonic()
        self._record_entry_gate_decision(
            market=market, entry_side_index=entry_side_index, entry_reason=entry_reason,
            entry_evidence=entry_evidence, active=active,
        )
        admission["timing_entry_gate_journal_ms"] = round((time.monotonic() - gate_journal_started_at) * 1000, 3)
        fill_visibility_barrier = self._entry_fill_visibility_barrier(
            market=market, report=report, admission=admission,
        )
        if fill_visibility_barrier is not None:
            return fill_visibility_barrier
        holding_before_started_at = time.monotonic()
        holding_result = self.holding_supervisor.manage_before_stream_gate(
            self, snapshot=snapshot, config=config,
        )
        admission["timing_holding_before_stream_ms"] = round((time.monotonic() - holding_before_started_at) * 1000, 3)
        if holding_result is not None:
            return holding_result
        stream_gate_started_at = time.monotonic()
        health_error = self._stream_ready(market)
        admission["timing_stream_gate_ms"] = round((time.monotonic() - stream_gate_started_at) * 1000, 3)
        if health_error and not (reduce_only and active):
            admission["market_data_gate"] = health_error.detail
            return health_error
        admission["market_data_gate"] = "ws_fresh" if health_error is None else "ws_stale_existing_exit_rest_fallback"
        holding_after_started_at = time.monotonic()
        holding_result = self.holding_supervisor.manage_after_stream_gate(self, snapshot=snapshot)
        admission["timing_holding_after_stream_ms"] = round((time.monotonic() - holding_after_started_at) * 1000, 3)
        if holding_result is not None:
            return holding_result
        entry_preflight_started_at = time.monotonic()
        entry_preflight = self.entry_supervisor.preflight(
            self, snapshot=snapshot, admission=admission, config=config,
        )
        admission["timing_entry_preflight_ms"] = round((time.monotonic() - entry_preflight_started_at) * 1000, 3)
        if entry_preflight is not None:
            return entry_preflight
        fee_read_started_at = time.monotonic()
        fees = self.recovery.account.get_user_fees_sync(self.recovery.wallet)
        maker_close_fee = Decimal(str(fees["userSpotAddRate"]))
        taker_close_fee = Decimal(str(fees["userSpotCrossRate"]))
        admission["timing_fee_read_ms"] = round((time.monotonic() - fee_read_started_at) * 1000, 3)
        book_started_at = time.monotonic()
        book = self.machine.gateway.fetch_order_book(market=market, side_index=entry_side_index)
        admission["timing_entry_book_request_ms"] = round((time.monotonic() - book_started_at) * 1000, 3)
        try:
            price = Decimal(str(book["bids"][0]["price"]))
            entry_ask = Decimal(str(book["asks"][0]["price"]))
        except (IndexError, KeyError, TypeError, ValueError):
            admission["book_gate"] = "selected_entry_book_unavailable"
            return LiveExecutionResult("blocked", "live strategy entry book unavailable")
        admission["selected_best_bid"] = str(price)
        # The opening 50/50 region is an uncertainty regime, not a bargain
        # by itself.  S0 is a momentum/confirmation experiment and therefore
        # never tries to call a reversal from the centre of the binary range.
        if price < config.min_entry_price:
            admission["entry_price_gate"] = "selected_bid_in_no_trade_band"
            return LiveExecutionResult(
                "flat",
                f"live strategy no-trade band: selected bid {price} < {config.min_entry_price}",
            )
        if price >= config.max_entry_price:
            admission["entry_price_gate"] = "selected_bid_at_or_above_high_premium_pause"
            return LiveExecutionResult(
                "flat",
                f"live strategy high-premium pause: selected bid {price} >= {config.max_entry_price}",
            )
        shadow_requested_shares = max(
            whole_share_size(price),
            int((self.risk_gate.limits.max_entry_notional_usdc / price).to_integral_value(rounding=ROUND_FLOOR)),
        )
        admission["queue_pricing_shadow"] = self._observe_queue_pricing_shadow(
            market=market, side_index=entry_side_index, book=book,
            requested_shares=shadow_requested_shares, current_bid=price,
            confidence=admission.get("confidence_entry_shadow") if isinstance(admission.get("confidence_entry_shadow"), dict) else None,
        )
        target_policy_started_at = time.monotonic()
        target_decision = OutcomeExitTargetPolicy(self.ledger.journal.db_path).decide(
            outcome_id=market.outcome_id, side_index=entry_side_index,
        )
        admission["timing_target_policy_ms"] = round((time.monotonic() - target_policy_started_at) * 1000, 3)
        target_price_preview = take_profit_price(
            entry_price=price, target_return_pct=target_decision.target_return_pct,
            maker_close_fee_rate=maker_close_fee,
        )
        if target_price_preview is None:
            admission["target_gate"] = "fee_after_target_exceeds_price_ceiling"
            return LiveExecutionResult("flat", "live strategy dynamic fee-after target exceeds Outcome price ceiling")
        reentry = None
        if self.loss_reentry_gate is not None:
            reentry = self.loss_reentry_gate.evaluate(
                outcome_id=market.outcome_id,
                coin=admission["selected_coin"],
                candidate_bid=float(price),
            )
            admission["loss_reentry_gate"] = {
                "allowed": reentry.allowed,
                "reason": reentry.reason,
                "loss_reentry_active": reentry.is_loss_reentry,
                "prior_exit_price": reentry.prior_exit_price,
                "cooldown_remaining_sec": reentry.cooldown_remaining_sec,
            }
            if not reentry.allowed:
                return LiveExecutionResult("flat", f"live strategy no entry: {reentry.reason}")
        admission["target_return_pct"] = str(target_decision.target_return_pct)
        admission["target_policy_source"] = target_decision.source
        admission["target_price_preview"] = str(target_price_preview)
        # Persist the target decision on the ORDER_SUBMIT record itself.  The
        # follow-up strategy event remains useful for research queries, but
        # it is deliberately not the sole source of truth for an accepted
        # live entry order.
        entry_tier = str(entry_evidence.get("entry_tier") or "tier_a_spot_mark_oi")
        tier_b = entry_tier == "tier_b_spot_mark"
        continuation = entry_tier == "tier_c_trend_continuation"
        entry_policy_kind = (
            "s0_trend_continuation" if continuation
            else "s0_spot_mark_tier_b" if tier_b else "s0_oi_spot_mark_confirmation"
        )
        sampling_policy = (
            "trend_continuation" if continuation
            else "spot_mark_tier_b" if tier_b else "oi_spot_mark_confirmation"
        )
        if continuation:
            # C4 deliberately spends the one canary opportunity when the
            # official SDK accepts a resting BUY, not when a signal merely
            # appears.  The DB check makes restart behavior equally bounded.
            if self._continuation_entry_already_submitted(outcome_id=market.outcome_id):
                admission["continuation_gate"] = "one_submit_per_market_exhausted"
                return LiveExecutionResult("flat", "live strategy no entry: continuation_one_submit_per_market_exhausted")
            admission["continuation_gate"] = "one_submit_per_market_available"
        capacity_canary = (
            self.risk_gate.limits.max_entry_notional_usdc >= Decimal("20")
            and self.risk_gate.limits.max_total_outcome_exposure_usdc >= Decimal("20")
        )
        shares = whole_share_size(price)
        entry_max_submit_price: Decimal | None = None
        execution_audit: dict[str, object] = {"entry_capacity_canary_enabled": capacity_canary}
        if capacity_canary:
            min_opening_shares = whole_share_size(price)
            # Size to the configured dollar ceiling without rounding *above*
            # it.  The venue minimum is still separately enforced below.
            desired_shares = max(
                min_opening_shares,
                int((self.risk_gate.limits.max_entry_notional_usdc / price).to_integral_value(rounding=ROUND_FLOOR)),
            )
            gate = self.tier_b_execution_gate
            quality = gate.evaluate(
                bid=price, ask=entry_ask, bid_levels=book.get("bids", ()), requested_shares=Decimal(desired_shares),
                coin=str(admission["selected_coin"]),
            ) if gate is not None else None
            if quality is None:
                admission["entry_execution_gate"] = {"allowed": False, "reason": "entry_execution_gate_unavailable"}
                return LiveExecutionResult("blocked", "entry execution-quality gate unavailable")
            safe_shares = min(Decimal(desired_shares), quality.safe_max_shares or Decimal("0"))
            stress = self.stress_exitability_sizer.evaluate(
                bid_levels=book.get("bids", ()), desired_shares=safe_shares,
                venue_minimum_shares=Decimal(min_opening_shares), entry_price=price,
            )
            # The stress policy is intentionally a ceiling: it never turns a
            # rejected/too-small capacity into a larger order.
            safe_shares = min(safe_shares, stress.stress_safe_shares)
            # Do not let whole_share_size round inadequate capacity up to the
            # venue minimum.  Capacity is a ceiling, never a hint.
            shares = int(safe_shares) if safe_shares >= min_opening_shares else 0
            execution_audit.update({
                "entry_execution_tier": entry_tier,
                "entry_spread_bps": str(quality.spread_bps) if quality.spread_bps is not None else None,
                "entry_top3_depth_shares": str(quality.top_depth_shares) if quality.top_depth_shares is not None else None,
                "entry_safe_max_shares": str(quality.safe_max_shares) if quality.safe_max_shares is not None else None,
                "entry_recent_trade_shares_5m": str(quality.recent_trade_shares) if quality.recent_trade_shares is not None else None,
                "entry_requested_shares": desired_shares,
                "entry_submitted_shares": shares,
                "entry_max_submit_drift_bps": str(quality.policy.max_submit_drift_bps),
                "entry_policy_sample_count": quality.policy.sample_count,
                "entry_policy_source": quality.policy.source,
                "entry_decision_bid": str(quality.decision_bid) if quality.decision_bid is not None else None,
                "entry_stress_exitability": stress.audit,
            })
            admission["entry_execution_gate"] = {"allowed": quality.allowed, "reason": quality.reason, **execution_audit}
            if not quality.allowed or quality.max_submit_bid is None:
                self._record_wide_spread_candidate(
                    market=market, coin=str(admission["selected_coin"]), bid=price, ask=entry_ask,
                    quality=quality, entry_tier=entry_tier, requested_shares=desired_shares, admission=admission,
                )
                return LiveExecutionResult("flat", f"live strategy no entry: {quality.reason}")
            if not stress.allowed:
                return LiveExecutionResult("flat", f"live strategy no entry: {stress.reason}")
            if shares < min_opening_shares:
                return LiveExecutionResult("flat", "live strategy no entry: entry_safe_capacity_below_venue_minimum")
            entry_max_submit_price = quality.max_submit_bid
        elif tier_b:
            # Preserve the existing $11 Tier-B quality behavior until the
            # operator explicitly starts the $20 canary.
            gate = self.tier_b_execution_gate
            quality = gate.evaluate(
                bid=price, ask=entry_ask, bid_levels=book.get("bids", ()), requested_shares=Decimal(shares),
                coin=str(admission["selected_coin"]),
            ) if gate is not None else None
            if quality is None or not quality.allowed or quality.max_submit_bid is None:
                reason = quality.reason if quality is not None else "tier_b_execution_gate_unavailable"
                admission["tier_b_execution_gate"] = {"allowed": False, "reason": reason}
                if quality is not None:
                    self._record_wide_spread_candidate(
                        market=market, coin=str(admission["selected_coin"]), bid=price, ask=entry_ask,
                        quality=quality, entry_tier=entry_tier, requested_shares=shares, admission=admission,
                    )
                return LiveExecutionResult("flat", f"live strategy no entry: {reason}")
            entry_max_submit_price = quality.max_submit_bid
            execution_audit.update({
                "tier_b_spread_bps": str(quality.spread_bps) if quality.spread_bps is not None else None,
                "tier_b_top_depth_shares": str(quality.top_depth_shares) if quality.top_depth_shares is not None else None,
                "tier_b_max_submit_drift_bps": str(quality.policy.max_submit_drift_bps),
                "tier_b_policy_sample_count": quality.policy.sample_count,
                "tier_b_policy_source": quality.policy.source,
                "tier_b_decision_bid": str(quality.decision_bid) if quality.decision_bid is not None else None,
            })
        risk_gate_started_at = time.monotonic()
        risk = self.risk_gate.evaluate(
            balances=self.recovery.account.get_spot_clearinghouse_state_sync(self.recovery.wallet).get("balances", []),
            open_orders=self.recovery.account.get_open_orders_sync(self.recovery.wallet), price=price, shares=shares,
        )
        admission["timing_risk_gate_ms"] = round((time.monotonic() - risk_gate_started_at) * 1000, 3)
        if not risk.allowed:
            admission["risk_gate"] = {
                "allowed": False, "reason": risk.reason,
                "entry_notional": str(risk.entry_notional),
                "available_collateral": str(risk.available_collateral),
                "current_exposure": str(risk.current_exposure),
            }
            return LiveExecutionResult("blocked", f"risk gate: {risk.reason}")
        admission["risk_gate"] = {
            "allowed": True, "reason": risk.reason,
            "entry_notional": str(risk.entry_notional),
            "available_collateral": str(risk.available_collateral),
            "current_exposure": str(risk.current_exposure),
        }
        portfolio_started_at = time.monotonic()
        portfolio = self.portfolio_guard.evaluate(
            outcome_id=market.outcome_id, prospective_notional=risk.entry_notional,
            phase_entry_cap=self.risk_gate.limits.max_entry_notional_usdc,
            phase_exposure_cap=self.risk_gate.limits.max_total_outcome_exposure_usdc,
        ) if self.portfolio_guard is not None else None
        admission["timing_portfolio_guard_ms"] = round((time.monotonic() - portfolio_started_at) * 1000, 3)
        if portfolio is None or not portfolio.allowed:
            reason = portfolio.reason if portfolio is not None else "portfolio_guard_unavailable"
            admission["portfolio_guard"] = {"allowed": False, "reason": reason}
            return LiveExecutionResult("flat", f"live strategy no entry: {reason}")
        admission["portfolio_guard"] = {
            "allowed": True, "reason": portfolio.reason, "enabled": portfolio.enabled,
            "market_session_gross_entry_usdc": str(portfolio.market_session_gross_entry_usdc),
            "market_session_realized_net_usdc": str(portfolio.market_session_realized_net_usdc),
            "market_session_gross_entry_limit_usdc": str(portfolio.market_session_gross_entry_limit_usdc),
            "market_session_realized_loss_limit_usdc": str(portfolio.market_session_realized_loss_limit_usdc),
            "rolling_24h_realized_net_usdc": str(portfolio.rolling_24h_realized_net_usdc),
            "rolling_24h_realized_loss_limit_usdc": str(portfolio.rolling_24h_realized_loss_limit_usdc),
            "consecutive_loss_exits": portfolio.consecutive_loss_exits,
            "market_loss_exits": portfolio.market_loss_exits,
        }
        entry_audit = {
            "entry_policy_schema_version": 1,
            "entry_policy_kind": entry_policy_kind,
            "entry_tier": entry_tier,
            "target_return_pct": str(target_decision.target_return_pct),
            "target_policy_source": target_decision.source,
            "target_estimated_move_pct": (
                str(target_decision.estimated_move_pct)
                if target_decision.estimated_move_pct is not None else None
            ),
            "target_volatility_sample_count": target_decision.sample_count,
            "maker_close_fee_rate": str(maker_close_fee),
            "taker_close_fee_rate": str(taker_close_fee),
            "loss_reprice_pct": "0.05",
            "narrow_after_sec": config.narrow_after_sec,
            "narrow_return_pct": str(config.narrow_return_pct),
            "floor_after_sec": config.floor_after_sec,
            "floor_return_pct": str(config.floor_return_pct),
            "entry_bid_at_decision": str(price),
            # A fill may occur away from this bid.  The protective sell is
            # recalculated from verified fill VWAP, so label this explicitly
            # as a decision-time preview rather than an asserted exit price.
            "target_price_preview_from_decision_bid": str(target_price_preview),
            "target_decision_at_ms": int(time.time() * 1000),
            "entry_time_left_sec": market.time_to_expiry_sec(),
            "entry_regime_state": (
                admission.get("market_regime_shadow", {}).get("state")
                if isinstance(admission.get("market_regime_shadow"), dict) else None
            ),
            "entry_regime_reason": (
                admission.get("market_regime_shadow", {}).get("reason")
                if isinstance(admission.get("market_regime_shadow"), dict) else None
            ),
            "entry_regime_current_zone": (
                admission.get("market_regime_shadow", {}).get("current_zone")
                if isinstance(admission.get("market_regime_shadow"), dict) else None
            ),
            "entry_regime_event_id": (
                admission.get("market_regime_shadow", {}).get("event_id")
                if isinstance(admission.get("market_regime_shadow"), dict) else None
            ),
            "loss_reentry_policy": reentry.reason if reentry is not None else "unavailable",
            "loss_reentry_active": bool(reentry.is_loss_reentry) if reentry is not None else False,
            "loss_reentry_prior_exit_price": (
                str(reentry.prior_exit_price) if reentry is not None and reentry.prior_exit_price is not None else None
            ),
            **execution_audit,
        }
        submit_started_at = time.monotonic()
        result = self.entry_execution_service.submit_new_entry(
            market=market, side_index=entry_side_index,
            coin=str(admission["selected_coin"]), price=price, shares=shares,
            entry_audit=entry_audit, entry_max_submit_price=entry_max_submit_price,
            config=config, target_price_preview=target_price_preview,
            target_decision=target_decision, reentry=reentry,
            entry_reason=entry_reason, entry_evidence=entry_evidence,
            entry_tier=entry_tier, sampling_policy=sampling_policy,
            maker_close_fee=maker_close_fee, taker_close_fee=taker_close_fee,
            max_entry_notional=self.risk_gate.limits.max_entry_notional_usdc,
        )
        admission["timing_entry_submit_ms"] = round((time.monotonic() - submit_started_at) * 1000, 3)
        return result

    def cancel_resting_buys(
        self,
        *,
        market: OutcomeMarketSpec,
        tracked_markets: tuple[OutcomeMarketSpec, ...] = (),
    ) -> LiveExecutionResult:
        """Cancel entry BUYs with venue confirmation, even if inventory exists.

        This is a risk-reducing action.  It deliberately does not reuse the
        ``safe_for_new_entry`` gate: a partial fill is precisely when the
        remaining BUY must be cancelled most urgently.
        """
        if not self.enabled():
            return LiveExecutionResult("disabled", "automated execution is disabled")
        report = self.recovery.reconcile((market, *tracked_markets))
        cancelled: list[str] = []
        for side_index, coin in enumerate((market.yes_coin, market.no_coin)):
            finding = next(item for item in report.findings if item.market_id == market.outcome_id and item.coin == coin)
            for order_id in finding.buy_order_ids:
                from bot.outcome_order_mutation import cancel_and_confirm
                mutation = cancel_and_confirm(
                    account=self._account_reads, gateway=self.machine.gateway, wallet=self.recovery.wallet,
                    market=market, side_index=side_index, order_id=order_id,
                )
                if not mutation.confirmed:
                    return LiveExecutionResult("reconcile_required", f"reduce-only {mutation.reason}", order_id)
                cancelled.append(order_id)
        return LiveExecutionResult("cancelled" if cancelled else "flat", "cancelled owned entry buys" if cancelled else "no owned entry buy", cancelled[0] if cancelled else None)

    def tick_reduce_only(
        self, *, market: OutcomeMarketSpec, entry_reason: str, entry_evidence: dict[str, object],
        retiring_markets: tuple[OutcomeMarketSpec, ...] = (), market_context: dict[str, object] | None = None,
    ) -> LiveExecutionResult:
        """No-new-risk tail: cancel BUYs, then continue the full SELL lifecycle."""
        cancelled = self.cancel_resting_buys(market=market, tracked_markets=retiring_markets)
        if cancelled.state == "reconcile_required":
            return cancelled
        return self.tick_live_strategy(
            market=market, entry_side_index=None, entry_reason=entry_reason,
            entry_evidence=entry_evidence, retiring_markets=retiring_markets,
            market_context=market_context, reduce_only=True,
        )
