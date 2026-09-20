"""Passive protective-SELL repricing for live Outcome holdings."""
from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any, Callable

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_exit_quote_planner import ExitQuoteAction, ExitQuoteInput, ExitQuotePlan, OutcomeExitQuotePlanner
from bot.outcome_exit_requote_controller import OutcomeExitRequoteController
from bot.outcome_p3_calibration import OutcomeP3CalibrationConfig
from bot.outcome_reversal import OutcomeReversalClassifier, OutcomeReversalInput, OutcomeReversalState
from bot.outcome_runtime_types import LiveExecutionResult


class OutcomeExitRequoteService:
    """Own E4/E5 passive exit policy evaluation and replacement mutation."""

    def __init__(
        self, *, recovery: Any, machine: Any, stream_health: Callable[[], Any],
        store: Any, controller: OutcomeExitRequoteController | None,
        planner: OutcomeExitQuotePlanner, holding_context: dict[int, dict[str, object]],
        reversal_classifier: OutcomeReversalClassifier,
        opposite_observation_counts: dict[tuple[int, str], int],
        canary_eligible_order_ids: set[str], fresh_book: Callable[..., dict[str, object]],
        top_of_book: Callable[[dict[str, object]], tuple[Decimal, Decimal] | None],
        persisted_policy: Callable[..., OutcomeP3CalibrationConfig | None],
        persisted_maker_fee: Callable[..., Decimal | None],
        strategy_exit_tier: Callable[..., tuple[Decimal, Decimal | None] | None],
        enabled: Callable[[], bool], canary_enabled: Callable[[], bool],
        loss_exit_enabled: Callable[[], bool] = lambda: True,
        gate_audit: Callable[..., None] | None = None,
    ) -> None:
        self.recovery = recovery
        self.machine = machine
        self.stream_health = stream_health
        self.store = store
        self.controller = controller
        self.planner = planner
        self.holding_context = holding_context
        self.reversal_classifier = reversal_classifier
        self.opposite_observation_counts = opposite_observation_counts
        self.canary_eligible_order_ids = canary_eligible_order_ids
        self.fresh_book = fresh_book
        self.top_of_book = top_of_book
        self.persisted_policy = persisted_policy
        self.persisted_maker_fee = persisted_maker_fee
        self.strategy_exit_tier = strategy_exit_tier
        self.enabled = enabled
        self.canary_enabled = canary_enabled
        self.loss_exit_enabled = loss_exit_enabled
        self.gate_audit = gate_audit
        self._last_modify_shadow: dict[str, tuple[str, float]] = {}

    def _record_modify_order_shadow(
        self, *, market: OutcomeMarketSpec, lifecycle: Any, plan: ExitQuotePlan,
        inventory: Decimal, bid: Decimal, ask: Decimal,
    ) -> None:
        """Record a would-modify candidate without changing execution behavior.

        Price edits are expected to lose queue priority according to the SDK
        contract.  The event is deliberately emitted only for a real existing
        cancel/replace plan and is rate-limited, so it can later be joined to
        fill quality without becoming a second execution pathway.
        """
        if plan.action is not ExitQuoteAction.CANCEL_REPLACE or plan.target_price is None:
            return
        target = str(plan.target_price)
        fingerprint = f"{lifecycle.order_id}:{target}:{inventory}"
        now = time.monotonic()
        previous = self._last_modify_shadow.get(str(lifecycle.order_id))
        if previous is not None and previous[0] == fingerprint and now - previous[1] < 10.0:
            return
        self.store.journal.log_strategy_event(self.store.run_id, "OUTCOME_MODIFY_ORDER_SHADOW", {
            "venue": "hyperliquid_outcome", "read_only": True,
            "outcome_id": market.outcome_id, "coin": lifecycle.coin,
            "order_id": lifecycle.order_id, "current_price": str(lifecycle.target_price),
            "proposed_price": target, "current_shares": str(lifecycle.inventory),
            "proposed_shares": str(inventory), "best_bid": str(bid), "best_ask": str(ask),
            "plan_reason": plan.reason,
            "would_use_modify_order": True,
            "size_only_edit": lifecycle.target_price == plan.target_price and lifecycle.inventory != inventory,
            "price_change_requeues_expected": lifecycle.target_price != plan.target_price,
            "live_authority": False,
        })
        self._last_modify_shadow[str(lifecycle.order_id)] = (fingerprint, now)

    def maybe_requote(self, *, market: OutcomeMarketSpec, finding: object) -> LiveExecutionResult | None:
        if not self.enabled() or self.store is None or self.controller is None:
            return None
        if not tuple(getattr(finding, "sell_order_ids", ())):
            return None
        coin = str(getattr(finding, "coin", ""))
        policy = self.persisted_policy(market=market, coin=coin)
        fee = self.persisted_maker_fee(market=market, coin=coin)
        if policy is None or fee is None:
            return LiveExecutionResult("blocked", "exit reprice requires persisted verified P3 policy")
        lifecycle = self.store.recover(wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin)
        if lifecycle is None:
            return LiveExecutionResult("blocked", "exit reprice refuses unrecorded sell ownership")
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        if vwap is None:
            return LiveExecutionResult("blocked", "exit reprice cannot verify fill VWAP", lifecycle.order_id)
        side_index = 0 if coin == market.yes_coin else 1
        try:
            stream_health = self.stream_health()
            ws_bbo = stream_health.fresh_bbo(market, coin) if stream_health else None
            if ws_bbo is not None:
                bid, ask = ws_bbo
            else:
                top = self.top_of_book(self.fresh_book(market=market, side_index=side_index))
                if top is None:
                    raise ValueError("invalid_book")
                bid, ask = top
        except (IndexError, KeyError, TypeError, ValueError):
            return LiveExecutionResult("blocked", "exit reprice book unavailable", lifecycle.order_id)

        raw_context = self.holding_context.get(market.outcome_id, {})

        def as_decimal(name: str) -> Decimal | None:
            try:
                value = raw_context.get(name)
                return Decimal(str(value)) if value is not None else None
            except (ValueError, ArithmeticError):
                return None

        try:
            oi_age_ms = int(raw_context.get("oi_age_ms"))
        except (TypeError, ValueError):
            oi_age_ms = -1
        reversal = self.reversal_classifier.classify(OutcomeReversalInput(
            side_index, vwap, bid, ask,
            as_decimal("spot_strike_bps"), as_decimal("mark_return_bps"), as_decimal("oi_return_bps"),
            0 <= oi_age_ms <= 90_000,
            self.opposite_observation_counts.get((market.outcome_id, coin), 0),
        ))
        # This is deliberately the same operator authority as the three IOC
        # loss lanes.  A "loss exits off" observation must not silently turn
        # a TP into a passive loss-band SELL.
        loss_exits_enabled = self.loss_exit_enabled()
        loss_band_authorized = (
            loss_exits_enabled
            and reversal.state is OutcomeReversalState.REVERSAL_CONFIRMED
        )
        current_signal = {
            key: raw_context.get(key)
            for key in (
                "signal", "score", "spot_strike_bps", "mark_return_bps",
                "oi_return_bps", "oi_age_ms", "entry_tier", "reason",
            )
            if key in raw_context
        }
        replacement_context = {
            "trigger_bbo": {
                "best_bid": str(bid), "best_ask": str(ask), "timestamp_ms": int(time.time() * 1000),
            },
            "current_signal": {
                **current_signal, "reversal_state": str(reversal.state), "reversal_reason": reversal.reason,
                "opposite_observation_count": self.opposite_observation_counts.get((market.outcome_id, coin), 0),
                "loss_band_authorized": loss_band_authorized,
                "loss_exit_enabled": loss_exits_enabled,
            },
        }
        if (
            self.canary_enabled()
            and not (not loss_exits_enabled and lifecycle.state in {"LOSS_BAND_RESTING", "LOSS_BAND_UNFILLED"})
            and lifecycle.order_id in self.canary_eligible_order_ids
            and lifecycle.replacement_count == 0
        ):
            min_age = max(1.0, float(os.environ.get("OUTCOME_EXIT_REQUOTE_CANARY_MIN_AGE_SEC", "15")))
            if lifecycle.updated_at_ts is not None and time.time() - lifecycle.updated_at_ts >= min_age:
                canary_plan = ExitQuotePlan(
                    ExitQuoteAction.CANCEL_REPLACE, "e5_one_tick_upward_canary",
                    lifecycle.target_price + self.planner.config.tick_size,
                    lifecycle.target_price, inventory, "e5_canary",
                )
                result = self.controller.execute(
                    market=market, side_index=side_index, lifecycle=lifecycle, plan=canary_plan,
                    replacement_context=replacement_context,
                )
                self.canary_eligible_order_ids.discard(lifecycle.order_id)
                return LiveExecutionResult(result.state, result.detail, result.new_order_id or result.old_order_id)
        strategy_tier = self.strategy_exit_tier(market=market, coin=coin)
        minimum_return_pct, loss_reprice_pct = strategy_tier or (policy.target_return_pct, policy.loss_reprice_pct)
        plan = self.planner.plan(ExitQuoteInput(
            inventory=inventory, fill_vwap=vwap, maker_close_fee_rate=fee,
            minimum_return_pct=minimum_return_pct, loss_reprice_pct=loss_reprice_pct,
            existing_order_id=lifecycle.order_id, existing_price=lifecycle.target_price,
            best_bid=bid, best_ask=ask, book_age_sec=0.0, now_ts=time.time(),
            # A previously resting loss-band must be migrated promptly after
            # the operator disables loss exits.  Do not let the normal
            # requote interval preserve that old loss order; all mutation
            # safeguards still live in the controller below.
            last_requote_ts=(
                None
                if not loss_exits_enabled and lifecycle.state in {"LOSS_BAND_RESTING", "LOSS_BAND_UNFILLED"}
                else lifecycle.updated_at_ts
            ),
            replacement_count=lifecycle.replacement_count,
            loss_band_authorized=loss_band_authorized,
        ))
        if self.gate_audit is not None:
            self.gate_audit(
                component="loss_band", eligible=loss_exits_enabled and plan.exit_mode == "loss_band",
                reason=plan.reason, market=market, lifecycle=lifecycle,
                position_age_sec=(None if lifecycle.updated_at_ts is None else max(0.0, time.time() - lifecycle.updated_at_ts)),
                executable_pnl=str(bid / vwap - Decimal("1")),
                reversal_state=str(reversal.state),
                independent_confirmation_count=self.opposite_observation_counts.get((market.outcome_id, coin), 0),
                book_state="fresh", loss_band_state=lifecycle.state,
            )
        if plan.action is ExitQuoteAction.KEEP:
            if plan.exit_mode == "loss_band" and lifecycle.state == "LOSS_BAND_RESTING":
                self.store.record(
                    lifecycle, reason="loss_band_unfilled_passive_quote",
                    extra={"state": "LOSS_BAND_UNFILLED"},
                )
            return LiveExecutionResult("sell_resting", f"exit reprice keep: {plan.reason}", lifecycle.order_id)
        if plan.action is ExitQuoteAction.BLOCK:
            if not loss_exits_enabled and lifecycle.state in {"LOSS_BAND_RESTING", "LOSS_BAND_UNFILLED"}:
                self.store.journal.log_strategy_event(self.store.run_id, "OUTCOME_LOSS_BAND_DISABLE_MIGRATION_BLOCKED", {
                    "outcome_id": market.outcome_id, "coin": coin, "order_id": lifecycle.order_id,
                    "state": lifecycle.state, "reason": plan.reason,
                    "loss_exit_enabled": False, "live_authority": False,
                })
            return LiveExecutionResult("blocked", f"exit reprice blocked: {plan.reason}", lifecycle.order_id)
        self._record_modify_order_shadow(
            market=market, lifecycle=lifecycle, plan=plan, inventory=inventory, bid=bid, ask=ask,
        )
        result = self.controller.execute(
            market=market, side_index=side_index, lifecycle=lifecycle, plan=plan,
            replacement_context=replacement_context,
        )
        if not loss_exits_enabled and lifecycle.state in {"LOSS_BAND_RESTING", "LOSS_BAND_UNFILLED"}:
            self.store.journal.log_strategy_event(self.store.run_id, "OUTCOME_LOSS_BAND_DISABLE_MIGRATION", {
                "outcome_id": market.outcome_id, "coin": coin, "old_order_id": lifecycle.order_id,
                "new_order_id": result.new_order_id, "prior_state": lifecycle.state,
                "result_state": result.state, "detail": result.detail,
                "replacement_exit_mode": plan.exit_mode,
                "loss_exit_enabled": False,
            })
        return LiveExecutionResult(result.state, result.detail, result.new_order_id or result.old_order_id)
