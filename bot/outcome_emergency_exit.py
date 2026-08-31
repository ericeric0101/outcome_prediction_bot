"""Price-protected, one-shot emergency exit policy for Outcome inventory.

This is intentionally *not* a generic market-order helper.  It grants an
IOC/FAK sell only after a passive loss quote has failed, a persistent reversal
has been independently observed, and the entire reconciled inventory can be
sold within a fee-inclusive loss cap using the current L2 bids.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING
from enum import StrEnum
from typing import Any, Protocol

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_exit_lifecycle import OutcomeExitLifecycle, OutcomeExitLifecycleStore


class EmergencyExitAction(StrEnum):
    KEEP = "KEEP"
    BLOCK = "BLOCK"
    EXECUTE = "EXECUTE"


@dataclass(frozen=True)
class OutcomeEmergencyExitConfig:
    """Approved S3 values; intentionally code-owned, not shell-tunable."""

    trigger_loss_pct: Decimal = Decimal("0.08")
    max_net_loss_pct: Decimal = Decimal("0.12")
    min_holding_sec: float = 2 * 60 * 60
    min_loss_band_unfilled_sec: float = 20 * 60
    min_independent_reversal_observations: int = 3
    min_reversal_duration_sec: float = 2 * 60
    max_book_age_sec: float = 15.0
    tick_size: Decimal = Decimal("0.00001")


@dataclass(frozen=True)
class OutcomeEmergencyExitInput:
    inventory: Decimal
    fill_vwap: Decimal | None
    taker_close_fee_rate: Decimal | None
    bids: tuple[tuple[Decimal, Decimal], ...]
    book_age_sec: float | None
    holding_age_sec: float
    loss_band_unfilled_sec: float | None
    reversal_independent_observations: int
    reversal_duration_sec: float
    already_attempted: bool = False


@dataclass(frozen=True)
class OutcomeEmergencyExitPlan:
    action: EmergencyExitAction
    reason: str
    limit_price: Decimal | None = None
    executable_vwap: Decimal | None = None
    net_return_pct: Decimal | None = None
    requested_shares: Decimal | None = None


def parse_bid_levels(book: dict[str, Any]) -> tuple[tuple[Decimal, Decimal], ...] | None:
    """Strictly parse descending, positive raw L2 bid levels from the SDK."""
    parsed: list[tuple[Decimal, Decimal]] = []
    try:
        for raw in book.get("bids", []):
            price, size = Decimal(str(raw["price"])), Decimal(str(raw["size"]))
            if not Decimal("0") < price < Decimal("1") or size <= 0:
                return None
            if parsed and price > parsed[-1][0]:
                return None
            parsed.append((price, size))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    return tuple(parsed) or None


def book_age_sec(book: dict[str, Any], *, now_ms: int) -> float | None:
    try:
        timestamp = int(book["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    age = (now_ms - timestamp) / 1000.0
    return age if age >= 0 else None


def _ceil_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


class OutcomeEmergencyExitPolicy:
    def __init__(self, config: OutcomeEmergencyExitConfig | None = None) -> None:
        self.config = config or OutcomeEmergencyExitConfig()

    def plan(self, item: OutcomeEmergencyExitInput) -> OutcomeEmergencyExitPlan:
        cfg = self.config
        if item.already_attempted:
            return OutcomeEmergencyExitPlan(EmergencyExitAction.KEEP, "emergency_exit_already_attempted")
        if item.inventory <= 0 or item.inventory != item.inventory.to_integral_value():
            return OutcomeEmergencyExitPlan(EmergencyExitAction.BLOCK, "unverified_non_integral_inventory")
        if item.fill_vwap is None or not Decimal("0") < item.fill_vwap < Decimal("1"):
            return OutcomeEmergencyExitPlan(EmergencyExitAction.BLOCK, "missing_verified_fill_vwap")
        if item.taker_close_fee_rate is None or not Decimal("0") <= item.taker_close_fee_rate < Decimal("1"):
            return OutcomeEmergencyExitPlan(EmergencyExitAction.BLOCK, "invalid_taker_close_fee")
        if item.holding_age_sec < cfg.min_holding_sec:
            return OutcomeEmergencyExitPlan(EmergencyExitAction.KEEP, "minimum_holding_time_not_reached")
        if item.loss_band_unfilled_sec is None or item.loss_band_unfilled_sec < cfg.min_loss_band_unfilled_sec:
            return OutcomeEmergencyExitPlan(EmergencyExitAction.KEEP, "passive_loss_band_wait_not_elapsed")
        if item.reversal_independent_observations < cfg.min_independent_reversal_observations:
            return OutcomeEmergencyExitPlan(EmergencyExitAction.KEEP, "independent_reversal_observations_not_met")
        if item.reversal_duration_sec < cfg.min_reversal_duration_sec:
            return OutcomeEmergencyExitPlan(EmergencyExitAction.KEEP, "reversal_duration_not_met")
        if item.book_age_sec is None or item.book_age_sec > cfg.max_book_age_sec:
            return OutcomeEmergencyExitPlan(EmergencyExitAction.BLOCK, "stale_or_missing_rest_l2")
        if not item.bids:
            return OutcomeEmergencyExitPlan(EmergencyExitAction.BLOCK, "no_valid_l2_bids")

        fee_denominator = Decimal("1") - item.taker_close_fee_rate
        limit = _ceil_to_tick(item.fill_vwap * (Decimal("1") - cfg.max_net_loss_pct) / fee_denominator, cfg.tick_size)
        if not Decimal("0") < limit < Decimal("1"):
            return OutcomeEmergencyExitPlan(EmergencyExitAction.BLOCK, "loss_cap_outside_outcome_bounds")

        remaining, proceeds = item.inventory, Decimal("0")
        for price, size in item.bids:
            if not Decimal("0") < price < Decimal("1") or size <= 0:
                return OutcomeEmergencyExitPlan(EmergencyExitAction.BLOCK, "invalid_l2_bid_level")
            if price < limit:
                break
            used = min(remaining, size)
            proceeds += used * price
            remaining -= used
            if remaining <= 0:
                break
        if remaining > 0:
            return OutcomeEmergencyExitPlan(EmergencyExitAction.BLOCK, "insufficient_full_inventory_depth_at_loss_cap", limit_price=limit)

        vwap = proceeds / item.inventory
        net_return = (vwap * fee_denominator / item.fill_vwap) - Decimal("1")
        if net_return > -cfg.trigger_loss_pct:
            return OutcomeEmergencyExitPlan(EmergencyExitAction.KEEP, "emergency_loss_trigger_not_reached", limit, vwap, net_return, item.inventory)
        if net_return < -cfg.max_net_loss_pct:
            # Should be unreachable because every included level is >= limit,
            # but retain this independent defence against arithmetic mistakes.
            return OutcomeEmergencyExitPlan(EmergencyExitAction.BLOCK, "depth_walk_exceeds_net_loss_cap", limit, vwap, net_return, item.inventory)
        return OutcomeEmergencyExitPlan(EmergencyExitAction.EXECUTE, "s3_price_protected_emergency_ioc_authorized", limit, vwap, net_return, item.inventory)


class EmergencyExitAccountReader(Protocol):
    def get_spot_clearinghouse_state_sync(self, user: str) -> dict[str, Any]: ...
    def get_open_orders_sync(self, user: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class EmergencyExitExecutionResult:
    state: str
    detail: str
    old_order_id: str | None = None
    emergency_order_id: str | None = None


def _inventory(snapshot: dict[str, Any], coin: str) -> Decimal:
    alternate = "+" + coin[1:] if coin.startswith("#") else coin
    for row in snapshot.get("balances", []):
        if row.get("coin") in {coin, alternate}:
            return Decimal(str(row.get("total", "0")))
    return Decimal("0")


class OutcomeEmergencyExitController:
    """Own the cancel-confirm-read-L2-IOC transaction boundary for S3."""

    def __init__(self, *, account: EmergencyExitAccountReader, gateway: Any,
                 store: OutcomeExitLifecycleStore, wallet: str,
                 policy: OutcomeEmergencyExitPolicy) -> None:
        self.account, self.gateway, self.store, self.wallet, self.policy = account, gateway, store, wallet, policy
        self._in_flight: set[tuple[int, str]] = set()

    def execute(self, *, market: OutcomeMarketSpec, side_index: int,
                lifecycle: OutcomeExitLifecycle, item: OutcomeEmergencyExitInput,
                plan: OutcomeEmergencyExitPlan) -> EmergencyExitExecutionResult:
        key = (market.outcome_id, lifecycle.coin)
        if plan.action is not EmergencyExitAction.EXECUTE or plan.limit_price is None:
            return EmergencyExitExecutionResult("blocked", "emergency_plan_does_not_authorize_ioc", lifecycle.order_id)
        if key in self._in_flight:
            return EmergencyExitExecutionResult("blocked", "emergency_exit_already_in_flight", lifecycle.order_id)
        self._in_flight.add(key)
        try:
            before_orders = self.account.get_open_orders_sync(self.wallet)
            before_inventory = _inventory(self.account.get_spot_clearinghouse_state_sync(self.wallet), lifecycle.coin)
            owned = self.store.reconcile_owned_sell(
                wallet=self.wallet, outcome_id=market.outcome_id, coin=lifecycle.coin,
                inventory=before_inventory, open_orders=before_orders,
            )
            if owned is None or owned.order_id != lifecycle.order_id or before_inventory != item.inventory:
                return EmergencyExitExecutionResult("reconcile_required", "emergency_owned_sell_or_inventory_not_verified", lifecycle.order_id)
            self.store.record(lifecycle, reason=plan.reason, extra={
                "state": "EMERGENCY_CANCEL_SUBMITTED", "limit_price": str(plan.limit_price),
                "planned_net_return_pct": str(plan.net_return_pct),
                "planned_executable_vwap": str(plan.executable_vwap),
            })
            try:
                self.gateway.cancel_owned_order(market=market, side_index=side_index, order_id=lifecycle.order_id)
            except Exception as exc:
                self.store.record(lifecycle, reason=f"emergency_cancel_exception:{type(exc).__name__}", extra={"state": "RECONCILE_REQUIRED"})
                return EmergencyExitExecutionResult("reconcile_required", "emergency_cancel_request_failed", lifecycle.order_id)
            after_orders = self.account.get_open_orders_sync(self.wallet)
            if any(str(row.get("oid")) == lifecycle.order_id for row in after_orders):
                self.store.record(lifecycle, reason="emergency_cancel_not_confirmed", extra={"state": "RECONCILE_REQUIRED"})
                return EmergencyExitExecutionResult("reconcile_required", "emergency_old_sell_still_open", lifecycle.order_id)
            after_inventory = _inventory(self.account.get_spot_clearinghouse_state_sync(self.wallet), lifecycle.coin)
            if after_inventory <= 0:
                self.store.record(lifecycle, reason="emergency_inventory_flat_during_cancel", extra={"state": "RECONCILE_REQUIRED"})
                return EmergencyExitExecutionResult("reconciled", "inventory_filled_or_flat_during_emergency_cancel", lifecycle.order_id)
            if after_inventory != item.inventory:
                self.store.record(lifecycle, reason="emergency_inventory_changed_during_cancel", extra={"state": "RECONCILE_REQUIRED"})
                return EmergencyExitExecutionResult("reconcile_required", "inventory_changed_during_emergency_cancel", lifecycle.order_id)
            try:
                book = self.gateway.fetch_order_book(market=market, side_index=side_index)
                bids = parse_bid_levels(book)
                fresh_item = replace(item, bids=bids or (), book_age_sec=book_age_sec(book, now_ms=int(time.time() * 1000)))
                fresh_plan = self.policy.plan(fresh_item)
            except Exception as exc:
                self.store.record(lifecycle, reason=f"emergency_l2_exception:{type(exc).__name__}", extra={"state": "RECONCILE_REQUIRED"})
                return EmergencyExitExecutionResult("reconcile_required", "emergency_fresh_l2_unavailable", lifecycle.order_id)
            if fresh_plan.action is not EmergencyExitAction.EXECUTE or fresh_plan.limit_price is None:
                self.store.record(lifecycle, reason=f"emergency_revalidation_{fresh_plan.reason}", extra={"state": "RECONCILE_REQUIRED"})
                return EmergencyExitExecutionResult("reconcile_required", f"emergency_revalidation_blocked:{fresh_plan.reason}", lifecycle.order_id)
            try:
                result = self.gateway.place_price_protected_ioc_exit(
                    market=market, side_index=side_index, limit_price=fresh_plan.limit_price,
                    requested_shares=after_inventory,
                )
            except Exception as exc:
                self.store.record(lifecycle, reason=f"emergency_ioc_exception:{type(exc).__name__}", extra={"state": "RECONCILE_REQUIRED"})
                return EmergencyExitExecutionResult("reconcile_required", "emergency_ioc_submission_failed", lifecycle.order_id)
            emergency_order_id = str(result.get("orderId") or "")
            if not emergency_order_id:
                self.store.record(lifecycle, reason="emergency_ioc_missing_order_id", extra={"state": "RECONCILE_REQUIRED"})
                return EmergencyExitExecutionResult("reconcile_required", "emergency_ioc_unconfirmed", lifecycle.order_id)
            submitted = OutcomeExitLifecycle(
                self.wallet, market.outcome_id, lifecycle.coin, emergency_order_id,
                after_inventory, fresh_plan.limit_price, lifecycle.replacement_count, "EMERGENCY_EXIT_SUBMITTED",
            )
            self.store.record(submitted, reason=fresh_plan.reason, extra={
                "old_order_id": lifecycle.order_id, "requested_shares": str(after_inventory),
                "limit_price": str(fresh_plan.limit_price), "planned_executable_vwap": str(fresh_plan.executable_vwap),
                "planned_net_return_pct": str(fresh_plan.net_return_pct), "order_type": "price_protected_fak_ioc",
            })
            return EmergencyExitExecutionResult("emergency_exit_submitted", "cancel_confirmed_price_protected_ioc_submitted", lifecycle.order_id, emergency_order_id)
        finally:
            self._in_flight.discard(key)
