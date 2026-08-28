"""Cancel-confirm-rebook-replace controller for a single owned Outcome sell.

This controller is intentionally not wired into ``OutcomeLiveExecutionRuntime``
until E4.  Its observable contract is nonetheless production-shaped: no new
order can be created before account truth confirms cancellation of the old
order, and every uncertain state remains reconciliation-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any, Protocol

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_exit_lifecycle import OutcomeExitLifecycle, OutcomeExitLifecycleStore
from bot.outcome_exit_quote_planner import ExitQuoteAction, ExitQuotePlan


class ExitAccountReader(Protocol):
    def get_spot_clearinghouse_state_sync(self, user: str) -> dict[str, Any]: ...
    def get_open_orders_sync(self, user: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ExitRequoteResult:
    state: str
    detail: str
    old_order_id: str | None = None
    new_order_id: str | None = None


def _inventory(snapshot: dict[str, Any], coin: str) -> Decimal:
    alternate = "+" + coin[1:] if coin.startswith("#") else coin
    for row in snapshot.get("balances", []):
        if row.get("coin") in {coin, alternate}:
            return Decimal(str(row.get("total", "0")))
    return Decimal("0")


class OutcomeExitRequoteController:
    def __init__(self, *, account: ExitAccountReader, gateway: Any, store: OutcomeExitLifecycleStore,
                 wallet: str, tick_size: Decimal = Decimal("0.00001")) -> None:
        self.account, self.gateway, self.store, self.wallet = account, gateway, store, wallet
        self.tick_size = tick_size
        self._in_flight: set[tuple[int, str]] = set()

    def execute(self, *, market: OutcomeMarketSpec, side_index: int, lifecycle: OutcomeExitLifecycle,
                plan: ExitQuotePlan) -> ExitRequoteResult:
        key = (market.outcome_id, lifecycle.coin)
        if plan.action is not ExitQuoteAction.CANCEL_REPLACE or plan.target_price is None:
            return ExitRequoteResult("blocked", "plan_does_not_authorize_replacement", lifecycle.order_id)
        if key in self._in_flight:
            return ExitRequoteResult("blocked", "replacement_already_in_flight", lifecycle.order_id)
        self._in_flight.add(key)
        try:
            before_orders = self.account.get_open_orders_sync(self.wallet)
            before_inventory = _inventory(self.account.get_spot_clearinghouse_state_sync(self.wallet), lifecycle.coin)
            owned = self.store.reconcile_owned_sell(wallet=self.wallet, outcome_id=market.outcome_id, coin=lifecycle.coin,
                                                    inventory=before_inventory, open_orders=before_orders)
            if owned is None or owned.order_id != lifecycle.order_id:
                return ExitRequoteResult("reconcile_required", "owned_sell_not_verified_from_account_truth", lifecycle.order_id)
            self.store.record(lifecycle, reason=plan.reason, extra={"state": "CANCEL_SUBMITTED", "planned_price": str(plan.target_price)})
            try:
                self.gateway.cancel_owned_order(market=market, side_index=side_index, order_id=lifecycle.order_id)
            except Exception as exc:
                self.store.record(lifecycle, reason=f"cancel_exception:{type(exc).__name__}", extra={"state": "RECONCILE_REQUIRED"})
                return ExitRequoteResult("reconcile_required", "cancel_request_failed", lifecycle.order_id)

            orders_after_cancel = self.account.get_open_orders_sync(self.wallet)
            if any(str(row.get("oid")) == lifecycle.order_id for row in orders_after_cancel):
                self.store.record(lifecycle, reason="cancel_not_confirmed", extra={"state": "RECONCILE_REQUIRED"})
                return ExitRequoteResult("reconcile_required", "old_order_still_open_after_cancel", lifecycle.order_id)
            inventory_after_cancel = _inventory(self.account.get_spot_clearinghouse_state_sync(self.wallet), lifecycle.coin)
            if inventory_after_cancel <= 0:
                self.store.record(lifecycle, reason="inventory_flat_after_cancel", extra={"state": "RECONCILE_REQUIRED"})
                return ExitRequoteResult("reconciled", "inventory_filled_or_flat_during_cancel", lifecycle.order_id)
            if inventory_after_cancel != lifecycle.inventory:
                self.store.record(lifecycle, reason="partial_fill_or_inventory_change_during_cancel", extra={"state": "RECONCILE_REQUIRED"})
                return ExitRequoteResult("reconcile_required", "inventory_changed_during_cancel", lifecycle.order_id)

            book = self.gateway.fetch_order_book(market=market, side_index=side_index)
            try:
                bid = Decimal(str(book["bids"][0]["price"]))
                ask = Decimal(str(book["asks"][0]["price"]))
            except (IndexError, KeyError, TypeError, ValueError):
                self.store.record(lifecycle, reason="invalid_book_after_cancel", extra={"state": "RECONCILE_REQUIRED"})
                return ExitRequoteResult("reconcile_required", "book_unusable_after_cancel", lifecycle.order_id)
            price = max(plan.target_price, ask, bid + self.tick_size)
            price = (price / self.tick_size).to_integral_value(rounding=ROUND_CEILING) * self.tick_size
            if not Decimal("0") < price < Decimal("1") or price <= bid:
                self.store.record(lifecycle, reason="replacement_would_cross_or_bound", extra={"state": "RECONCILE_REQUIRED"})
                return ExitRequoteResult("reconcile_required", "replacement_not_passive_after_rebook", lifecycle.order_id)
            try:
                result = self.gateway.place_alo(market=market, side_index=side_index, is_buy=False, price=price,
                                                requested_shares=inventory_after_cancel, reduce_only=True)
            except Exception as exc:
                self.store.record(lifecycle, reason=f"replacement_exception:{type(exc).__name__}", extra={"state": "RECONCILE_REQUIRED"})
                return ExitRequoteResult("reconcile_required", "replacement_submission_failed", lifecycle.order_id)
            new_id = str(result.get("orderId") or "")
            if not new_id:
                self.store.record(lifecycle, reason="replacement_missing_order_id", extra={"state": "RECONCILE_REQUIRED"})
                return ExitRequoteResult("reconcile_required", "replacement_unconfirmed", lifecycle.order_id)
            new_lifecycle = OutcomeExitLifecycle(self.wallet, market.outcome_id, lifecycle.coin, new_id,
                                                  inventory_after_cancel, price, lifecycle.replacement_count + 1, "SELL_RESTING")
            self.store.record(new_lifecycle, reason=plan.reason, extra={"old_order_id": lifecycle.order_id,
                              "best_bid": str(bid), "best_ask": str(ask), "exit_mode": plan.exit_mode or "unknown"})
            return ExitRequoteResult("sell_resting", "cancel_confirmed_rebooked_alo_replacement", lifecycle.order_id, new_id)
        finally:
            self._in_flight.discard(key)
