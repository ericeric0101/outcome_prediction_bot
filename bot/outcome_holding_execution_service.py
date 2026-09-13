"""Protective holding transitions separated from strategy orchestration."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_entry_lifecycle import OutcomeEntryLifecycleStore
from bot.outcome_entry_requote import OutcomeEntryRequoteController
from bot.outcome_execution_ledger import OutcomeExecutionLedger
from bot.outcome_maker_state_machine import MakerTickResult, OutcomeMakerStateMachine
from bot.outcome_p3_calibration import OutcomeP3CalibrationConfig
from bot.outcome_runtime_types import LiveExecutionResult


class OutcomeHoldingExecutionService:
    """Own fill cleanup and the first protective SELL transition."""

    def __init__(
        self, *, recovery: Any, machine: OutcomeMakerStateMachine,
        entry_store: OutcomeEntryLifecycleStore | None,
        entry_controller: OutcomeEntryRequoteController | None,
        ledger: OutcomeExecutionLedger | None,
        persisted_policy: Callable[..., OutcomeP3CalibrationConfig | None],
        persisted_maker_fee: Callable[..., Decimal | None],
        strategy_exit_tier: Callable[..., tuple[Decimal, Decimal | None] | None],
        exit_requote_enabled: Callable[[], bool],
        record_result: Callable[[OutcomeMarketSpec, int, MakerTickResult], LiveExecutionResult],
    ) -> None:
        self.recovery = recovery
        self.machine = machine
        self.entry_store = entry_store
        self.entry_controller = entry_controller
        self.ledger = ledger
        self.persisted_policy = persisted_policy
        self.persisted_maker_fee = persisted_maker_fee
        self.strategy_exit_tier = strategy_exit_tier
        self.exit_requote_enabled = exit_requote_enabled
        self.record_result = record_result

    def protect_after_fill(
        self, *, market: OutcomeMarketSpec, finding: object,
    ) -> LiveExecutionResult | None:
        if not self.entry_store or not self.entry_controller:
            return None
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        buy_order_ids = tuple(getattr(finding, "buy_order_ids", ()))
        sell_order_ids = tuple(getattr(finding, "sell_order_ids", ()))
        coin = str(getattr(finding, "coin", ""))
        if inventory <= 0 or len(buy_order_ids) != 1 or sell_order_ids or coin not in {market.yes_coin, market.no_coin}:
            return None
        side_index = 0 if coin == market.yes_coin else 1
        lifecycle = self.entry_store.recover_or_adopt_audited_submit(
            wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            open_orders=self.recovery.account.get_open_orders_sync(self.recovery.wallet),
        )
        if lifecycle is None or lifecycle.order_id != str(buy_order_ids[0]):
            return LiveExecutionResult("blocked", "filled entry refuses unrecorded buy ownership", str(buy_order_ids[0]))
        cancel = self.entry_controller.execute_cancel_after_fill(
            market=market, side_index=side_index, lifecycle=lifecycle,
        )
        if self.ledger:
            self.ledger.journal.log_order_event(
                self.ledger.run_id, "ORDER_CANCEL", venue_order_id=lifecycle.order_id, side="BUY",
                status="CANCELLED" if cancel.state in {"cancelled_after_fill", "flat"} else "RECONCILE_REQUIRED",
                instrument_id=coin, reason=cancel.detail,
                payload={
                    "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "coin": coin,
                    "filled_entry_cleanup": True,
                    "execution_submitted": cancel.state in {"cancelled_after_fill", "flat"},
                },
            )
        if cancel.state != "cancelled_after_fill":
            return LiveExecutionResult(cancel.state, cancel.detail, cancel.old_order_id)
        refreshed = self.recovery.reconcile([market])
        current = next((item for item in refreshed.findings if item.coin == coin), None)
        if current is None or Decimal(str(current.inventory)) <= 0:
            return LiveExecutionResult("flat", "filled entry cancel confirmed and inventory is now flat", cancel.old_order_id)
        if tuple(current.buy_order_ids) or tuple(current.sell_order_ids):
            return LiveExecutionResult("blocked", "filled entry cancel reconciliation found remaining conflicting order", cancel.old_order_id)
        protective = self.advance_persisted_exit(market=market, finding=current)
        if protective is None:
            return LiveExecutionResult("blocked", "filled entry has no persisted verified exit policy", cancel.old_order_id)
        return protective

    def advance_persisted_exit(
        self, *, market: OutcomeMarketSpec, finding: object,
    ) -> LiveExecutionResult | None:
        coin = str(getattr(finding, "coin", ""))
        policy = self.persisted_policy(market=market, coin=coin)
        fee = self.persisted_maker_fee(market=market, coin=coin)
        if policy is None or fee is None:
            return None
        side_index = 0 if coin == market.yes_coin else 1
        strategy_tier = self.strategy_exit_tier(market=market, coin=coin)
        minimum_return_pct, loss_reprice_pct = strategy_tier or (
            policy.target_return_pct,
            policy.loss_reprice_pct if self.exit_requote_enabled() else None,
        )
        result = self.machine.tick(
            market=market, side_index=side_index, entry_permitted=False,
            minimum_return_pct=minimum_return_pct, maker_close_fee_rate=fee,
            loss_reprice_pct=loss_reprice_pct,
        )
        return self.record_result(market, side_index, result)
