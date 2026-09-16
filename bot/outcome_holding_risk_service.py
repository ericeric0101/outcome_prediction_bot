"""Bounded taker-exit lanes for an already protected Outcome holding."""
from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_emergency_exit import (
    EmergencyExitAction,
    OutcomeEmergencyExitController,
    OutcomeEmergencyExitInput,
    OutcomeEmergencyExitPolicy,
    book_age_sec as emergency_book_age_sec,
    parse_bid_levels,
)
from bot.outcome_execution_ledger import OutcomeExecutionLedger
from bot.outcome_exit_lifecycle import OutcomeExitLifecycleStore
from bot.outcome_maker_state_machine import OutcomeMakerStateMachine
from bot.outcome_runtime_types import LiveExecutionResult
from bot.outcome_risk_episode import OutcomeRiskEpisodeStore, RiskEpisode


class OutcomeHoldingRiskService:
    """Own fast-failure and S3 policy-to-controller dispatch."""

    def __init__(
        self, *, recovery: Any, machine: OutcomeMakerStateMachine,
        store: OutcomeExitLifecycleStore | None, ledger: OutcomeExecutionLedger | None,
        fast_policy: OutcomeEmergencyExitPolicy,
        fast_controller: OutcomeEmergencyExitController | None,
        emergency_policy: OutcomeEmergencyExitPolicy,
        emergency_controller: OutcomeEmergencyExitController | None,
        reversal_windows: dict[tuple[int, str], tuple[float, float, int]],
        fresh_book: Callable[..., dict[str, object]],
        official_holding_age: Callable[..., float | None],
        live_entry_age: Callable[..., float | None],
        gate_audit: Callable[..., None] | None = None,
        risk_episodes: OutcomeRiskEpisodeStore | None = None,
        narrow_policy: OutcomeEmergencyExitPolicy | None = None,
        narrow_controller: OutcomeEmergencyExitController | None = None,
        narrow_candidate: Callable[..., dict[str, object] | None] | None = None,
        confirmed_loss_recorder: Any | None = None,
    ) -> None:
        self.recovery = recovery
        self.machine = machine
        self.store = store
        self.ledger = ledger
        self.fast_policy = fast_policy
        self.fast_controller = fast_controller
        self.emergency_policy = emergency_policy
        self.emergency_controller = emergency_controller
        self.narrow_policy = narrow_policy
        self.narrow_controller = narrow_controller
        self.narrow_candidate = narrow_candidate
        # The existing loss re-entry gate owns the durable cooldown/reclaim
        # policy.  Emergency IOC exits must feed it only after account truth
        # is flat and the official fill reconciliation has completed.
        self.confirmed_loss_recorder = confirmed_loss_recorder
        self.reversal_windows = reversal_windows
        self.fresh_book = fresh_book
        self.official_holding_age = official_holding_age
        self.live_entry_age = live_entry_age
        self.gate_audit = gate_audit
        self.risk_episodes = risk_episodes

    def maybe_narrow_hard_failure(self, *, market: OutcomeMarketSpec, finding: object) -> LiveExecutionResult | None:
        """Execute only a fresh, shadow-qualified $11-cap canary candidate.

        The candidate has no mutation authority.  This method independently
        re-reads fees/L2 and shares the same durable risk-episode budget as
        existing fast-failure and S3 controllers.
        """
        if (self.ledger is None or self.store is None or self.narrow_controller is None
                or self.narrow_policy is None or self.narrow_candidate is None or self.risk_episodes is None):
            return None
        coin = str(getattr(finding, "coin", ""))
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        if not tuple(getattr(finding, "sell_order_ids", ())):
            self._audit_early(component="OUTCOME_NARROW_HARD_FAILURE_CANARY", market=market, coin=coin, reason="no_owned_protective_sell", inventory=inventory)
            return None
        lifecycle = self.store.recover(wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin)
        if lifecycle is None or lifecycle.state == "EMERGENCY_EXIT_SUBMITTED":
            self._audit_early(component="OUTCOME_NARROW_HARD_FAILURE_CANARY", market=market, coin=coin, lifecycle=lifecycle, reason="exit_lifecycle_unavailable_or_emergency_submitted", inventory=inventory)
            return None
        fill_vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        candidate = self.narrow_candidate(
            market=market, coin=coin, inventory=inventory, fill_vwap=fill_vwap,
        )
        if candidate is None:
            self._audit_early(component="OUTCOME_NARROW_HARD_FAILURE_CANARY", market=market, coin=coin, lifecycle=lifecycle, reason="no_fresh_shadow_hard_candidate", inventory=inventory)
            return None
        if self._attempt_budget_exhausted(market=market, coin=coin):
            self._audit_early(component="OUTCOME_NARROW_HARD_FAILURE_CANARY", market=market, coin=coin, lifecycle=lifecycle, reason="shared_risk_episode_attempt_budget_exhausted", inventory=inventory)
            return None
        entry_age = self.official_holding_age(market=market, coin=coin, inventory=inventory, fill_vwap=fill_vwap)
        if entry_age is None:
            self._audit_early(component="OUTCOME_NARROW_HARD_FAILURE_CANARY", market=market, coin=coin, lifecycle=lifecycle, reason="official_fill_age_unavailable", inventory=inventory)
            return None
        side_index = 0 if coin == market.yes_coin else 1
        try:
            fees = self.recovery.account.get_user_fees_sync(self.recovery.wallet)
            taker_fee = Decimal(str(fees["userSpotCrossRate"]))
            book = self.fresh_book(market=market, side_index=side_index)
            bids = parse_bid_levels(book)
            book_age = emergency_book_age_sec(book, now_ms=int(time.time() * 1000))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            bids, book_age, taker_fee = None, None, None
        item = OutcomeEmergencyExitInput(
            inventory=inventory, fill_vwap=fill_vwap, taker_close_fee_rate=taker_fee,
            bids=bids or (), book_age_sec=book_age, holding_age_sec=entry_age,
            loss_band_unfilled_sec=None, reversal_independent_observations=0,
            reversal_duration_sec=0.0, already_attempted=False, risk_detected_ts=time.time(),
        )
        return self._plan_record_execute(
            market=market, coin=coin, side_index=side_index, lifecycle=lifecycle, item=item,
            policy=self.narrow_policy, controller=self.narrow_controller,
            decision_event="OUTCOME_NARROW_HARD_FAILURE_CANARY",
            execution_type="narrow_hard_failure_price_protected_fak_ioc",
            holding_age_basis="official_fill_timestamp_ms",
        )

    def maybe_fast_failure(self, *, market: OutcomeMarketSpec, finding: object) -> LiveExecutionResult | None:
        if self.ledger is None or self.store is None or self.fast_controller is None:
            return None
        coin = str(getattr(finding, "coin", ""))
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        if not tuple(getattr(finding, "sell_order_ids", ())):
            self._audit_early(component="OUTCOME_FAST_FAILURE_EXIT_DECISION", market=market, coin=coin, reason="no_owned_protective_sell", inventory=inventory)
            return None
        lifecycle = self.store.recover(wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin)
        if lifecycle is None:
            self._audit_early(component="OUTCOME_FAST_FAILURE_EXIT_DECISION", market=market, coin=coin, reason="exit_lifecycle_unavailable", inventory=inventory)
            return None
        if lifecycle.state == "EMERGENCY_EXIT_SUBMITTED":
            self._audit_early(component="OUTCOME_FAST_FAILURE_EXIT_DECISION", market=market, coin=coin, lifecycle=lifecycle, reason="emergency_exit_already_submitted", inventory=inventory)
            return None
        if self._attempt_budget_exhausted(market=market, coin=coin):
            self._audit_early(component="OUTCOME_FAST_FAILURE_EXIT_DECISION", market=market, coin=coin, lifecycle=lifecycle, reason="attempt_budget_exhausted", inventory=inventory)
            return None
        fill_vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        entry_age = self.official_holding_age(
            market=market, coin=coin, inventory=inventory, fill_vwap=fill_vwap,
        )
        window = self.reversal_windows.get((market.outcome_id, coin), (0.0, 0.0, 0))
        cfg = self.fast_policy.config
        if entry_age is None or entry_age < cfg.min_holding_sec:
            self._audit_early(component="OUTCOME_FAST_FAILURE_EXIT_DECISION", market=market, coin=coin, lifecycle=lifecycle, reason="minimum_holding_time_not_reached",
                              inventory=inventory, holding_age_sec=entry_age, reversal_count=window[2])
            return None
        if window[2] < cfg.min_independent_reversal_observations or time.time() - window[0] < cfg.min_reversal_duration_sec:
            self._audit_early(component="OUTCOME_FAST_FAILURE_EXIT_DECISION", market=market, coin=coin, lifecycle=lifecycle, reason="independent_reversal_observations_not_met",
                              inventory=inventory, holding_age_sec=entry_age, reversal_count=window[2], reversal_duration_sec=time.time() - window[0] if window[0] else 0.0)
            return None
        side_index = 0 if coin == market.yes_coin else 1
        try:
            fees = self.recovery.account.get_user_fees_sync(self.recovery.wallet)
            taker_fee = Decimal(str(fees["userSpotCrossRate"]))
            book = self.fresh_book(market=market, side_index=side_index)
            bids = parse_bid_levels(book)
            book_age = emergency_book_age_sec(book, now_ms=int(time.time() * 1000))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            bids, book_age, taker_fee = None, None, None
        item = OutcomeEmergencyExitInput(
            inventory=inventory, fill_vwap=fill_vwap, taker_close_fee_rate=taker_fee,
            bids=bids or (), book_age_sec=book_age, holding_age_sec=entry_age,
            loss_band_unfilled_sec=None, reversal_independent_observations=window[2],
            reversal_duration_sec=(time.time() - window[0]) if window[0] > 0 else 0.0,
            already_attempted=False,
            risk_detected_ts=time.time(),
        )
        return self._plan_record_execute(
            market=market, coin=coin, side_index=side_index, lifecycle=lifecycle, item=item,
            policy=self.fast_policy, controller=self.fast_controller,
            decision_event="OUTCOME_FAST_FAILURE_EXIT_DECISION",
            execution_type="fast_failure_price_protected_fak_ioc",
            holding_age_basis="official_fill_timestamp_ms",
        )

    def maybe_emergency(self, *, market: OutcomeMarketSpec, finding: object) -> LiveExecutionResult | None:
        if self.ledger is None or self.store is None or self.emergency_controller is None:
            return None
        coin = str(getattr(finding, "coin", ""))
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        if not tuple(getattr(finding, "sell_order_ids", ())):
            self._audit_early(component="OUTCOME_EMERGENCY_EXIT_DECISION", market=market, coin=coin, reason="no_owned_protective_sell", inventory=inventory)
            return None
        lifecycle = self.store.recover(wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin)
        if lifecycle is None:
            self._audit_early(component="OUTCOME_EMERGENCY_EXIT_DECISION", market=market, coin=coin, reason="exit_lifecycle_unavailable", inventory=inventory)
            return None
        if lifecycle.state == "EMERGENCY_EXIT_SUBMITTED":
            self._audit_early(component="OUTCOME_EMERGENCY_EXIT_DECISION", market=market, coin=coin, lifecycle=lifecycle, reason="emergency_exit_already_submitted", inventory=inventory)
            return None
        entry_age = self.live_entry_age(market=market, coin=coin)
        loss_since = self.store.loss_band_first_seen_ts(
            wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
        )
        window = self.reversal_windows.get((market.outcome_id, coin), (0.0, 0.0, 0))
        cfg = self.emergency_policy.config
        if entry_age is None or entry_age < cfg.min_holding_sec:
            self._audit_early(component="OUTCOME_EMERGENCY_EXIT_DECISION", market=market, coin=coin, lifecycle=lifecycle, reason="minimum_holding_time_not_reached",
                              inventory=inventory, holding_age_sec=entry_age, reversal_count=window[2])
            return None
        if loss_since is None or time.time() - loss_since < cfg.min_loss_band_unfilled_sec:
            self._audit_early(component="OUTCOME_EMERGENCY_EXIT_DECISION", market=market, coin=coin, lifecycle=lifecycle, reason="passive_loss_band_wait_not_elapsed",
                              inventory=inventory, holding_age_sec=entry_age, reversal_count=window[2],
                              loss_band_state="not_armed" if loss_since is None else "loss_band_waiting")
            return None
        if window[2] < cfg.min_independent_reversal_observations or time.time() - window[0] < cfg.min_reversal_duration_sec:
            self._audit_early(component="OUTCOME_EMERGENCY_EXIT_DECISION", market=market, coin=coin, lifecycle=lifecycle, reason="independent_reversal_observations_not_met",
                              inventory=inventory, holding_age_sec=entry_age, reversal_count=window[2], reversal_duration_sec=time.time() - window[0] if window[0] else 0.0)
            return None
        if self._attempt_budget_exhausted(market=market, coin=coin):
            self._audit_early(component="OUTCOME_EMERGENCY_EXIT_DECISION", market=market, coin=coin, lifecycle=lifecycle, reason="attempt_budget_exhausted",
                              inventory=inventory, holding_age_sec=entry_age, reversal_count=window[2])
            return None
        fill_vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        side_index = 0 if coin == market.yes_coin else 1
        try:
            fees = self.recovery.account.get_user_fees_sync(self.recovery.wallet)
            taker_fee = Decimal(str(fees["userSpotCrossRate"]))
            book = self.fresh_book(market=market, side_index=side_index)
            bids = parse_bid_levels(book)
            book_age = emergency_book_age_sec(book, now_ms=int(time.time() * 1000))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            bids, book_age, taker_fee = None, None, None
        item = OutcomeEmergencyExitInput(
            inventory=inventory, fill_vwap=fill_vwap, taker_close_fee_rate=taker_fee,
            bids=bids or (), book_age_sec=book_age,
            holding_age_sec=entry_age if entry_age is not None else -1.0,
            loss_band_unfilled_sec=(time.time() - loss_since) if loss_since is not None else None,
            reversal_independent_observations=window[2],
            reversal_duration_sec=(time.time() - window[0]) if window[0] > 0 else 0.0,
            already_attempted=False,
            risk_detected_ts=time.time(),
        )
        return self._plan_record_execute(
            market=market, coin=coin, side_index=side_index, lifecycle=lifecycle, item=item,
            policy=self.emergency_policy, controller=self.emergency_controller,
            decision_event="OUTCOME_EMERGENCY_EXIT_DECISION",
            execution_type="s3_price_protected_fak_ioc", holding_age_basis=None,
        )

    def _plan_record_execute(
        self, *, market: OutcomeMarketSpec, coin: str, side_index: int, lifecycle: Any,
        item: OutcomeEmergencyExitInput, policy: OutcomeEmergencyExitPolicy,
        controller: OutcomeEmergencyExitController, decision_event: str,
        execution_type: str, holding_age_basis: str | None,
    ) -> LiveExecutionResult | None:
        assert self.ledger is not None
        plan = policy.plan(item)
        if self.gate_audit is not None:
            self.gate_audit(
                component=decision_event, eligible=plan.action is EmergencyExitAction.EXECUTE,
                reason=plan.reason, market=market, lifecycle=lifecycle, item=item,
                loss_band_state=getattr(lifecycle, "state", None),
                book_state=("fresh" if item.book_age_sec is not None and item.book_age_sec <= policy.config.max_book_age_sec else "stale_or_missing"),
                executable_pnl=str(plan.net_return_pct) if plan.net_return_pct is not None else None,
            )
        payload: dict[str, object] = {
            "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period,
            "coin": coin, "lifecycle_order_id": lifecycle.order_id, "action": plan.action,
            "reason": plan.reason, "inventory": str(item.inventory),
            "holding_age_sec": item.holding_age_sec,
            "loss_band_unfilled_sec": item.loss_band_unfilled_sec,
            "independent_reversal_observations": item.reversal_independent_observations,
            "reversal_duration_sec": item.reversal_duration_sec, "book_age_sec": item.book_age_sec,
            "limit_price": str(plan.limit_price) if plan.limit_price is not None else None,
            "executable_vwap": str(plan.executable_vwap) if plan.executable_vwap is not None else None,
            "net_return_pct": str(plan.net_return_pct) if plan.net_return_pct is not None else None,
            "execution_submitted": False,
        }
        if holding_age_basis is not None:
            payload["holding_age_basis"] = holding_age_basis
        self.ledger.journal.log_strategy_event(self.ledger.run_id, decision_event, payload)
        if plan.action is not EmergencyExitAction.EXECUTE:
            return None
        episode: RiskEpisode | None = None
        if self.risk_episodes is not None:
            episode = self.risk_episodes.open_or_resume(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
                trigger_family=execution_type,
                severity=str(plan.net_return_pct) if plan.net_return_pct is not None else None,
            )
            if episode is None:
                return LiveExecutionResult("blocked", "risk_episode_durable_open_failed")
            if self.risk_episodes.exhausted(episode):
                return LiveExecutionResult("blocked", "risk_episode_attempt_budget_exhausted")
        result = controller.execute(
            market=market, side_index=side_index, lifecycle=lifecycle, item=item, plan=plan,
        )
        if episode is not None and result.state in {
            "emergency_exit_submitted", "emergency_exit_flat", "emergency_exit_residual", "reconcile_required",
        }:
            # A reconcile-required response may be an ambiguous ACK and must
            # conservatively consume exactly one attempt in this episode.
            if self.risk_episodes.record_attempt(episode, execution_state=result.state) is None:
                return LiveExecutionResult("reconcile_required", "risk_episode_attempt_persistence_failed", result.emergency_order_id or result.old_order_id)
        if result.state in {"emergency_exit_submitted", "emergency_exit_flat", "emergency_exit_residual"}:
            self.ledger.journal.log_order_event(
                self.ledger.run_id, "ORDER_SUBMIT", venue_order_id=result.emergency_order_id,
                side="SELL", status="IOC_SUBMITTED", instrument_id=coin, reason=result.detail,
                payload={
                    "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "coin": coin,
                    "execution_type": execution_type, "old_order_id": result.old_order_id,
                    "limit_price": str(plan.limit_price), "planned_net_return_pct": str(plan.net_return_pct),
                },
            )
            fills = self.recovery.account.get_user_fills_sync(self.recovery.wallet)
            self.ledger.sync_fills(fills=fills, market_key=f"outcome:{market.outcome_id}", period=market.period)
            if result.state == "emergency_exit_flat" and self.confirmed_loss_recorder is not None:
                # This method independently verifies the complete official
                # BUY/SELL lot and fee-inclusive loss.  A failed or partial
                # reconciliation therefore cannot spend a re-entry token.
                self.confirmed_loss_recorder.record_confirmed_loss_exit(
                    outcome_id=market.outcome_id,
                    period=market.period,
                    coin=coin,
                    order_id=str(result.emergency_order_id or ""),
                )
        return LiveExecutionResult(result.state, result.detail, result.emergency_order_id or result.old_order_id)

    def _attempt_budget_exhausted(self, *, market: OutcomeMarketSpec, coin: str) -> bool:
        if self.risk_episodes is not None:
            episode = self.risk_episodes.active(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            )
            return episode is not None and self.risk_episodes.exhausted(episode)
        return self.store.emergency_attempted(wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin)

    def _audit_early(
        self, *, component: str, market: OutcomeMarketSpec, coin: str, reason: str, inventory: Decimal,
        lifecycle: object | None = None, holding_age_sec: float | None = None,
        reversal_count: int = 0, reversal_duration_sec: float = 0.0,
        loss_band_state: str | None = None,
    ) -> None:
        if self.gate_audit is None:
            return
        item = SimpleNamespace(
            inventory=inventory, holding_age_sec=holding_age_sec,
            reversal_independent_observations=reversal_count,
            reversal_duration_sec=reversal_duration_sec,
        )
        self.gate_audit(
            component=component, eligible=False, reason=reason,
            market=market, lifecycle=lifecycle, item=item,
            loss_band_state=loss_band_state or getattr(lifecycle, "state", None),
            book_state="not_reached", executable_pnl=None,
        )
