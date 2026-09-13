"""Exit ownership recovery and ambiguity barriers for the live runtime.

This service owns no strategy policy and performs no order mutation.  It is
the single bridge from fresh account truth to durable exit ownership state.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_exit_lifecycle import OutcomeExitLifecycleStore
from bot.outcome_runtime_types import LiveExecutionResult


class ExitRecoveryAccount(Protocol):
    def get_open_orders_sync(self, user: str) -> list[dict[str, Any]]: ...
    def force_open_orders_reconciliation_sync(self, user: str) -> list[dict[str, Any]]: ...


class ExitRecoveryPort(Protocol):
    wallet: str
    account: ExitRecoveryAccount


class ConfirmedLossRecorder(Protocol):
    def record_confirmed_loss_exit(self, *, outcome_id: int, period: str, coin: str, order_id: str) -> None: ...


class OutcomeExitRecoveryService:
    """Reconcile lifecycle ownership before any holding mutation is allowed."""

    def __init__(
        self, *, recovery: ExitRecoveryPort,
        store: OutcomeExitLifecycleStore | None,
        loss_reentry_gate: ConfirmedLossRecorder | None,
    ) -> None:
        self.recovery = recovery
        self.store = store
        self.loss_reentry_gate = loss_reentry_gate

    def reconcile(self, *, market: OutcomeMarketSpec, report: object) -> None:
        if self.store is None:
            return
        try:
            has_ambiguity = any(
                self.store.pending_ambiguous_submit(
                    wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
                ) is not None
                for coin in (market.yes_coin, market.no_coin)
            )
            force_orders = getattr(self.recovery.account, "force_open_orders_reconciliation_sync", None)
            open_orders = (
                force_orders(self.recovery.wallet)
                if has_ambiguity and callable(force_orders)
                else self.recovery.account.get_open_orders_sync(self.recovery.wallet)
            )
            for finding in getattr(report, "findings", ()):
                if int(getattr(finding, "market_id", -1)) != market.outcome_id:
                    continue
                coin = str(getattr(finding, "coin", ""))
                previous = self.store.recover(
                    wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
                )
                inventory = Decimal(str(getattr(finding, "inventory", "0")))
                self.store.reconcile_owned_sell(
                    wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
                    inventory=inventory, open_orders=open_orders,
                )
                if (inventory <= 0 and previous is not None
                        and not any(str(row.get("oid")) == previous.order_id for row in open_orders)
                        and previous.state in {
                            "LOSS_BAND_RESTING", "LOSS_BAND_UNFILLED", "REVERSAL_CONFIRMED",
                            "EMERGENCY_EXIT_SUBMITTED",
                        }
                        and self.loss_reentry_gate is not None):
                    self.loss_reentry_gate.record_confirmed_loss_exit(
                        outcome_id=market.outcome_id, period=market.period,
                        coin=previous.coin, order_id=previous.order_id,
                    )
        except Exception:
            # The authoritative account-recovery report remains the hard
            # execution gate.  Never infer a close from a failed auxiliary
            # journal/account read.
            return

    def ambiguity_barrier(self, *, market: OutcomeMarketSpec) -> LiveExecutionResult | None:
        if self.store is None:
            return None
        for coin in (market.yes_coin, market.no_coin):
            pending = self.store.pending_ambiguous_submit(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            )
            if pending is not None:
                return LiveExecutionResult(
                    "blocked", "ambiguous prior exit submission; account-truth reconciliation required",
                    str(pending.get("old_order_id") or "") or None,
                )
        return None
