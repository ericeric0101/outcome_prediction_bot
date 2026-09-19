"""Fail-closed entry ownership barriers independent of strategy scoring."""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Callable, Protocol

from bot.outcome_entry_lifecycle import OutcomeEntryLifecycleStore
from bot.outcome_exit_lifecycle import OutcomeExitLifecycleStore
from bot.outcome_exit_target_policy import OutcomeExitTargetDecision
from bot.outcome_loss_reentry import OutcomeLossReentryDecision, OutcomeLossReentryGate
from bot.outcome_sdk_sidecar import OutcomeSdkAmbiguousExecutionError
from bot.outcome_entry_requote import (
    EntryQuoteAction,
    EntryQuoteInput,
    EntryQuotePlan,
    OutcomeEntryQuotePlanner,
    OutcomeEntryRequoteController,
)
from bot.outcome_execution_ledger import OutcomeExecutionLedger
from bot.outcome_entry_quality_shadow import OutcomeEntryQualityShadow, OutcomeRestingBuyQualityInput
from bot.outcome_live_strategy import OutcomeLiveStrategyConfig
from bot.outcome_runtime_types import LiveExecutionResult, OutcomeRuntimeTickSnapshot


class EntryRecoveryPort(Protocol):
    wallet: str
    account: Any


class EntryGatewayPort(Protocol):
    def outcome_coin(self, market: object, side_index: int) -> str: ...
    def fetch_order_book(self, **kwargs: Any) -> dict[str, Any]: ...


class OutcomeEntryExecutionService:
    """Own admission barriers that must precede order construction."""

    def __init__(
        self, *, recovery: EntryRecoveryPort, gateway: EntryGatewayPort, machine: Any,
        store: OutcomeEntryLifecycleStore | None, planner: OutcomeEntryQuotePlanner,
        exit_store: OutcomeExitLifecycleStore | None = None,
        controller: OutcomeEntryRequoteController | None = None,
        ledger: OutcomeExecutionLedger | None = None,
        loss_reentry_gate: OutcomeLossReentryGate | None = None,
        record_result: Callable[..., LiveExecutionResult] | None = None,
        fast_risk_decision: Callable[..., tuple[bool, str, dict[str, object]]] | None = None,
        safety_preflight: Callable[..., tuple[bool, str]] | None = None,
        entry_quality_shadow: OutcomeEntryQualityShadow | None = None,
    ) -> None:
        self.recovery = recovery
        self.gateway = gateway
        self.machine = machine
        self.store = store
        self.exit_store = exit_store
        self.planner = planner
        self.controller = controller
        self.ledger = ledger
        self.loss_reentry_gate = loss_reentry_gate
        self.record_result = record_result
        self.fast_risk_decision = fast_risk_decision
        self.safety_preflight = safety_preflight
        self.entry_quality_shadow = entry_quality_shadow
        self._entry_quality_audits: dict[str, dict[str, object]] = {}
        self._last_entry_quality_observation: dict[str, tuple[str, float]] = {}

    def preflight(
        self, *, snapshot: OutcomeRuntimeTickSnapshot,
        admission: dict[str, object], config: OutcomeLiveStrategyConfig,
    ) -> LiveExecutionResult | None:
        if not bool(getattr(snapshot.report, "safe_for_new_entry", False)):
            return LiveExecutionResult(
                "blocked", f"account recovery blocked live strategy: {getattr(snapshot.report, 'reason', 'unknown')}",
            )
        if snapshot.active:
            admission["account_gate"] = "existing_outcome_inventory_or_order"
            return LiveExecutionResult("blocked", "live strategy has existing Outcome inventory or order")

        # The account snapshot can briefly look flat while a locally owned
        # protective SELL is still resting or is in the process of filling.
        # Its durable lifecycle is an additional safety fact: never submit a
        # second BUY until that exact owned exit has reached terminal account
        # truth.  This closes the TP-fill / new-BUY race without adopting any
        # external or manual SELL.
        if self.exit_store is not None:
            for coin in (snapshot.market.yes_coin, snapshot.market.no_coin):
                exit_lifecycle = self.exit_store.recover(
                    wallet=self.recovery.wallet, outcome_id=snapshot.market.outcome_id, coin=coin,
                )
                if exit_lifecycle is not None:
                    admission["owned_exit_lifecycle_fence"] = {
                        "allowed": False, "coin": coin, "order_id": exit_lifecycle.order_id,
                        "state": exit_lifecycle.state,
                    }
                    return LiveExecutionResult(
                        "blocked", "owned protective exit pending terminal account reconciliation",
                        exit_lifecycle.order_id,
                    )

        if self.safety_preflight is not None:
            ready, reason = self.safety_preflight(outcome_id=snapshot.market.outcome_id)
            admission["safety_readiness"] = {"ready": ready, "reason": reason}
            if not ready:
                return LiveExecutionResult("blocked", f"live strategy safety readiness: {reason}")

        if self.store is not None:
            pending = self.store.pending_ambiguous_submit(
                wallet=self.recovery.wallet, outcome_id=snapshot.market.outcome_id,
            )
            if pending is not None:
                try:
                    open_orders = self.recovery.account.get_open_orders_sync(self.recovery.wallet)
                    adopted = None
                    for coin in (snapshot.market.yes_coin, snapshot.market.no_coin):
                        candidate = self.store.recover_or_adopt_audited_submit(
                            wallet=self.recovery.wallet, outcome_id=snapshot.market.outcome_id,
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
                    return LiveExecutionResult(
                        "blocked", "ambiguous prior entry adopted; refreshing account truth before any new action",
                        adopted.order_id,
                    )
                return LiveExecutionResult(
                    "blocked", "ambiguous prior entry submission; reconciliation required before any new entry",
                )
            rearm_allowed, rearm_reason, rearmed_at_ms = self.store.stale_decision_rearm_barrier(
                wallet=self.recovery.wallet, outcome_id=snapshot.market.outcome_id,
                decision_at_ms=snapshot.entry_decision_at_ms,
                signal_ineligible=snapshot.entry_side_index not in (0, 1),
            )
            admission["stale_entry_rearm"] = {
                "allowed": rearm_allowed, "reason": rearm_reason,
                "rearmed_at_ms": rearmed_at_ms,
            }
            if not rearm_allowed:
                return LiveExecutionResult("flat", f"live strategy no entry: {rearm_reason}")
        if snapshot.reduce_only:
            admission["reduce_only_gate"] = "new_entries_prohibited"
            return LiveExecutionResult("flat", "reduce-only: no live exposure after entry cancellation")
        if snapshot.entry_side_index not in (0, 1):
            admission["signal_gate"] = "no_directional_signal"
            return LiveExecutionResult("flat", f"live strategy no entry: {snapshot.entry_reason}")

        admission["selected_side_index"] = snapshot.entry_side_index
        admission["selected_coin"] = self.gateway.outcome_coin(snapshot.market, snapshot.entry_side_index)
        if self.exit_store is not None and snapshot.entry_decision_at_ms is not None:
            last_exit = self.exit_store.latest_owned_sell_fill_at_ms(
                wallet=self.recovery.wallet, outcome_id=snapshot.market.outcome_id,
                coin=str(admission["selected_coin"]),
            )
            if last_exit is not None and snapshot.entry_decision_at_ms <= last_exit[0]:
                admission["post_exit_decision_fence"] = {
                    "allowed": False, "decision_observed_at_ms": snapshot.entry_decision_at_ms,
                    "owned_exit_filled_at_ms": last_exit[0], "owned_exit_order_id": last_exit[1],
                }
                return LiveExecutionResult(
                    "flat", "live strategy no entry: decision predates completed owned exit",
                    last_exit[1],
                )
        if self.store is not None:
            cooldown = self.store.fast_rebook_cooldown_remaining(
                wallet=self.recovery.wallet, outcome_id=snapshot.market.outcome_id,
                coin=str(admission["selected_coin"]),
                cooldown_sec=self.planner.config.fast_rebook_cooldown_sec,
            )
            admission["entry_fast_rebook_cooldown_remaining_sec"] = round(cooldown, 3)
            if cooldown > 0:
                return LiveExecutionResult(
                    "flat", f"live strategy no entry: fast_risk_rebook_cooldown ({cooldown:.1f}s remaining)",
                )
        return None

    def manage_resting_buy(
        self, *, snapshot: OutcomeRuntimeTickSnapshot,
        config: OutcomeLiveStrategyConfig,
    ) -> LiveExecutionResult | None:
        """Cancel one stale audited BUY; a later tick may create a replacement."""
        if not self.store or not self.controller:
            return None
        finding = snapshot.active[0] if len(snapshot.active) == 1 else None
        if finding is None:
            return None
        if Decimal(str(getattr(finding, "inventory", "0"))) != 0 or str(getattr(finding, "state", "")) != "buy_resting":
            return None
        buy_order_ids = tuple(getattr(finding, "buy_order_ids", ()))
        coin = str(getattr(finding, "coin", ""))
        if len(buy_order_ids) != 1 or coin not in {snapshot.market.yes_coin, snapshot.market.no_coin}:
            return LiveExecutionResult("blocked", "entry requote requires exactly one current-market buy order")
        side_index = 0 if coin == snapshot.market.yes_coin else 1
        lifecycle = self.store.recover_or_adopt_audited_submit(
            wallet=self.recovery.wallet, outcome_id=snapshot.market.outcome_id, coin=coin,
            open_orders=self.recovery.account.get_open_orders_sync(self.recovery.wallet),
        )
        if lifecycle is None or lifecycle.order_id != str(buy_order_ids[0]):
            return LiveExecutionResult("blocked", "entry requote refuses unrecorded buy ownership", str(buy_order_ids[0]))
        # Use the immutable accepted-submit audit rather than the most recent
        # lifecycle/recovery event.  Restart adoption must not give an old
        # resting BUY a fresh 60-second stale-order lease.
        resting_since = lifecycle.submitted_at_ts if lifecycle.submitted_at_ts is not None else lifecycle.updated_at_ts
        age = None if resting_since is None else max(0.0, time.time() - resting_since)
        self._observe_resting_buy_quality(
            snapshot=snapshot, lifecycle=lifecycle, side_index=side_index, order_age_sec=age,
        )
        # The reviewed stale-passive policy is deliberately narrower than the
        # ordinary requote lane: only an owned BUY with no durable official
        # fill and fresh zero account inventory may be cancelled at 60s.
        # It never makes a replacement decision in this tick.
        if config.stale_entry_cancel_enabled and age is not None and age >= config.stale_entry_cancel_sec:
            official_fill = self.store.official_buy_fill(
                outcome_id=snapshot.market.outcome_id, coin=coin, order_id=lifecycle.order_id,
            )
            if official_fill is not None:
                self.store.record(
                    lifecycle, reason="stale_zero_fill_cancel_refused_official_fill_present",
                    extra={"state": "RECONCILE_REQUIRED", "trade_id": official_fill.get("trade_id")},
                )
                return LiveExecutionResult(
                    "reconcile_required", "stale zero-fill cancel refused: official buy fill present", lifecycle.order_id,
                )
            audit = self.store.submit_audit(order_id=lifecycle.order_id, coin=coin) or {}
            decision_at_ms = audit.get("target_decision_at_ms")
            try:
                decision_at_ms = int(decision_at_ms) if decision_at_ms is not None else None
            except (TypeError, ValueError):
                decision_at_ms = None
            stale_cancel_decision_event_id = self.store.record_stale_cancel_decision(
                lifecycle=lifecycle, decision_at_ms=decision_at_ms, order_age_sec=age,
            )
            if stale_cancel_decision_event_id is None:
                return LiveExecutionResult("blocked", "stale zero-fill cancel durable decision unavailable", lifecycle.order_id)
            stale_plan = EntryQuotePlan(EntryQuoteAction.CANCEL, "stale_zero_fill_60s")
            try:
                result = self.controller.execute_cancel(
                    market=snapshot.market, side_index=side_index, lifecycle=lifecycle, plan=stale_plan,
                )
            except Exception:
                self.store.record(lifecycle, reason="stale_zero_fill_cancel_exception", extra={"state": "RECONCILE_REQUIRED"})
                return LiveExecutionResult("reconcile_required", "stale zero-fill cancel exception; reconciliation required", lifecycle.order_id)
            self._record_cancel(snapshot=snapshot, coin=coin, lifecycle=lifecycle,
                                result=result, reason=stale_plan.reason, fast_risk=None)
            if result.state != "cancelled":
                return LiveExecutionResult(result.state, result.detail, result.old_order_id)
            expired = self.store.record_stale_cancel_confirmed(
                lifecycle=lifecycle, decision_at_ms=decision_at_ms, order_age_sec=age,
                stale_cancel_decision_event_id=stale_cancel_decision_event_id,
            )
            if expired is None:
                return LiveExecutionResult("blocked", "stale cancel confirmed but decision expiry persistence unavailable", lifecycle.order_id)
            return LiveExecutionResult("cancelled", "stale zero-fill BUY cancelled; old decision expired pending re-arm", lifecycle.order_id)
        fast_confirmed, fast_reason, fast_evidence = (False, "fast_risk_unavailable", {})
        if self.fast_risk_decision is not None:
            fast_confirmed, fast_reason, fast_evidence = self.fast_risk_decision(
                market=snapshot.market, lifecycle=lifecycle, current_side_index=side_index,
                desired_side_index=snapshot.entry_side_index, decision_reason=snapshot.entry_reason,
            )
        interval_plan = self.planner.plan(EntryQuoteInput(
            current_side_index=side_index, existing_price=lifecycle.price,
            desired_side_index=None, desired_bid=None, decision_reason=snapshot.entry_reason,
            order_age_sec=age, fast_risk_confirmed=fast_confirmed,
            fast_risk_reason=fast_reason,
        ))
        if interval_plan.action is EntryQuoteAction.CANCEL:
            result = self.controller.execute_cancel(
                market=snapshot.market, side_index=side_index, lifecycle=lifecycle, plan=interval_plan,
            )
            self._record_cancel(snapshot=snapshot, coin=coin, lifecycle=lifecycle,
                                result=result, reason=interval_plan.reason, fast_risk=fast_evidence)
            return LiveExecutionResult(result.state, result.detail, result.old_order_id)
        if interval_plan.action is EntryQuoteAction.KEEP and interval_plan.reason == "entry_requote_interval_not_elapsed":
            return LiveExecutionResult("buy_resting", f"entry requote keep: {interval_plan.reason}", lifecycle.order_id)

        desired_side, desired_bid, decision_reason = snapshot.entry_side_index, None, snapshot.entry_reason
        if desired_side in (0, 1):
            try:
                book = self.gateway.fetch_order_book(market=snapshot.market, side_index=desired_side)
                desired_bid = Decimal(str(book["bids"][0]["price"]))
                if desired_bid < config.min_entry_price:
                    desired_side, desired_bid, decision_reason = None, None, "selected_bid_in_no_trade_band"
            except (IndexError, KeyError, TypeError, ValueError):
                desired_bid = None
        plan = self.planner.plan(EntryQuoteInput(
            current_side_index=side_index, existing_price=lifecycle.price,
            desired_side_index=desired_side, desired_bid=desired_bid,
            decision_reason=decision_reason, order_age_sec=age,
        ))
        if plan.action is EntryQuoteAction.KEEP:
            return LiveExecutionResult("buy_resting", f"entry requote keep: {plan.reason}", lifecycle.order_id)
        if plan.action is EntryQuoteAction.BLOCK:
            return LiveExecutionResult("blocked", f"entry requote blocked: {plan.reason}", lifecycle.order_id)
        result = self.controller.execute_cancel(
            market=snapshot.market, side_index=side_index, lifecycle=lifecycle, plan=plan,
        )
        self._record_cancel(snapshot=snapshot, coin=coin, lifecycle=lifecycle,
                            result=result, reason=plan.reason, fast_risk=None)
        return LiveExecutionResult(result.state, result.detail, result.old_order_id)

    def _observe_resting_buy_quality(
        self, *, snapshot: OutcomeRuntimeTickSnapshot, lifecycle: OutcomeEntryLifecycle,
        side_index: int, order_age_sec: float | None,
    ) -> None:
        """Persist a bounded Phase-A/B counterfactual, never an action."""
        if self.entry_quality_shadow is None or self.ledger is None:
            return
        audit = self._entry_quality_audits.get(lifecycle.order_id)
        if audit is None:
            audit = self.store.submit_audit(order_id=lifecycle.order_id, coin=lifecycle.coin) if self.store else None
            audit = dict(audit or {})
            self._entry_quality_audits[lifecycle.order_id] = audit
        payload = self.entry_quality_shadow.evaluate_resting_buy(OutcomeRestingBuyQualityInput(
            outcome_id=snapshot.market.outcome_id, period=snapshot.market.period,
            coin=lifecycle.coin, order_id=lifecycle.order_id, side_index=side_index,
            order_price=lifecycle.price, order_age_sec=order_age_sec,
            current_signal_side_index=snapshot.entry_side_index,
            current_signal_reason=snapshot.entry_reason, entry_audit=audit,
            market_context=dict(snapshot.market_context or {}),
        ))
        fingerprint = ":".join([
            str(payload.get("signal_state")),
            str((payload.get("stale_cancel_shadow") or {}).get("action")),
            str(payload.get("quote_off_touch")),
        ])
        previous = self._last_entry_quality_observation.get(lifecycle.order_id)
        now = time.monotonic()
        if previous is not None and previous[0] == fingerprint and now - previous[1] < 10.0:
            return
        self.ledger.journal.log_strategy_event(
            self.ledger.run_id, "OUTCOME_ENTRY_QUALITY_SHADOW", payload,
        )
        self._last_entry_quality_observation[lifecycle.order_id] = (fingerprint, now)

    def _record_cancel(
        self, *, snapshot: OutcomeRuntimeTickSnapshot, coin: str, lifecycle: Any,
        result: Any, reason: str, fast_risk: dict[str, object] | None,
    ) -> None:
        if self.ledger is None:
            return
        payload: dict[str, object] = {
            "venue": "hyperliquid_outcome", "outcome_id": snapshot.market.outcome_id,
            "coin": coin, "entry_requote_reason": reason,
            "execution_submitted": result.state == "cancelled",
        }
        if fast_risk is not None:
            payload["fast_risk"] = fast_risk
        self.ledger.journal.log_order_event(
            self.ledger.run_id, "ORDER_CANCEL", venue_order_id=lifecycle.order_id, side="BUY",
            status="CANCELLED" if result.state == "cancelled" else "RECONCILE_REQUIRED",
            instrument_id=coin, reason=result.detail, payload=payload,
        )

    def submit_new_entry(
        self, *, market: Any, side_index: int, coin: str, price: Decimal,
        shares: int, entry_audit: dict[str, object],
        entry_max_submit_price: Decimal | None, config: OutcomeLiveStrategyConfig,
        target_price_preview: Decimal, target_decision: OutcomeExitTargetDecision,
        reentry: OutcomeLossReentryDecision | None, entry_reason: str,
        entry_evidence: dict[str, object], entry_tier: str, sampling_policy: str,
        maker_close_fee: Decimal, taker_close_fee: Decimal,
        max_entry_notional: Decimal,
    ) -> LiveExecutionResult:
        """Persist intent, perform the sole new-BUY mutation, and finalize audit."""
        if self.ledger is None or self.record_result is None:
            return LiveExecutionResult("blocked", "live strategy requires an execution ledger")
        intent_id = f"{market.outcome_id}:{entry_audit['entry_policy_kind']}:{int(time.time() * 1000)}"
        entry_audit["entry_intent_id"] = intent_id
        intent_event_id = self.ledger.journal.log_durable_order_intent(self.ledger.run_id, {
            "venue": "hyperliquid_outcome", "wallet": self.recovery.wallet,
            "intent_id": intent_id, "state": "INTENT_DURABLE",
            "outcome_id": market.outcome_id, "coin": coin,
            "side": "BUY", "price": str(price), "shares": shares,
            "audit": entry_audit,
        })
        if intent_event_id is None:
            return LiveExecutionResult("blocked", "durable pre-submit entry intent unavailable")
        entry_audit["entry_intent_event_id"] = intent_event_id
        try:
            result = self.machine.tick(
                market=market, side_index=side_index, entry_permitted=True,
                entry_audit=entry_audit, entry_max_submit_price=entry_max_submit_price,
                entry_min_submit_price=config.min_entry_price,
                entry_requested_shares=Decimal(shares), entry_max_notional=max_entry_notional,
            )
        except OutcomeSdkAmbiguousExecutionError as exc:
            if self.store is not None:
                ambiguity_event_id = self.store.record_ambiguous_submit(
                    wallet=self.recovery.wallet, outcome_id=market.outcome_id,
                    coin=coin, intent_id=intent_id, intent_event_id=int(intent_event_id),
                    sidecar_request_id=exc.request_id, command=exc.command, detail=str(exc),
                )
                if ambiguity_event_id is None:
                    raise RuntimeError("ambiguous SDK submit could not persist reconciliation fence") from exc
            return LiveExecutionResult(
                "blocked", "ambiguous SDK entry submission; reconciliation required before retry",
            )
        if result.state == "buy_placed":
            if reentry is not None and reentry.is_loss_reentry and self.loss_reentry_gate is not None:
                self.loss_reentry_gate.record_reentry_submitted(
                    outcome_id=market.outcome_id, period=market.period, coin=coin,
                    order_id=str(result.order_id), bid=float(price),
                    target_price=float(target_price_preview), entry_reason=entry_reason,
                )
            self.ledger.journal.log_strategy_event(
                self.ledger.run_id, "OUTCOME_LIVE_STRATEGY_ENTRY_PLACED", {
                    "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
                    "period": market.period, "side_index": side_index, "coin": coin,
                    "price": str(price), "shares": shares,
                    "target_return_pct": str(target_decision.target_return_pct),
                    "target_policy_source": target_decision.source,
                    "target_estimated_move_pct": (
                        str(target_decision.estimated_move_pct)
                        if target_decision.estimated_move_pct is not None else None
                    ),
                    "target_volatility_sample_count": target_decision.sample_count,
                    "entry_policy_schema_version": 1, "order_submit_audit_persisted": True,
                    "loss_reprice_pct": "0.05", "maker_close_fee_rate": str(maker_close_fee),
                    "taker_close_fee_rate": str(taker_close_fee),
                    "narrow_after_sec": config.narrow_after_sec,
                    "narrow_return_pct": str(config.narrow_return_pct),
                    "floor_after_sec": config.floor_after_sec,
                    "floor_return_pct": str(config.floor_return_pct),
                    "order_id": result.order_id, "entry_reason": entry_reason,
                    "entry_evidence": entry_evidence, "entry_tier": entry_tier,
                    "sampling_policy": sampling_policy, "directional_signal_used": True,
                    "loss_reentry_policy": reentry.reason if reentry is not None else "unavailable",
                    "loss_reentry_active": bool(reentry.is_loss_reentry) if reentry is not None else False,
                    "loss_reentry_prior_exit_price": (
                        str(reentry.prior_exit_price)
                        if reentry is not None and reentry.prior_exit_price is not None else None
                    ),
                },
            )
        return self.record_result(market, side_index, result)
