"""Pure planning and cancel-confirm handling for owned Outcome maker buys."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_entry_lifecycle import OutcomeEntryLifecycle, OutcomeEntryLifecycleStore


class EntryQuoteAction(StrEnum):
    KEEP = "KEEP"
    CANCEL = "CANCEL"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class OutcomeEntryQuotePlannerConfig:
    min_requote_interval_sec: float = 300.0
    min_price_delta_ticks: int = 2
    tick_size: Decimal = Decimal("0.00001")


@dataclass(frozen=True)
class EntryQuoteInput:
    current_side_index: int
    existing_price: Decimal
    desired_side_index: int | None
    desired_bid: Decimal | None
    decision_reason: str
    order_age_sec: float | None


@dataclass(frozen=True)
class EntryQuotePlan:
    action: EntryQuoteAction
    reason: str


class OutcomeEntryQuotePlanner:
    """A valid signal may refresh a stale quote; missing data may not."""

    def __init__(self, config: OutcomeEntryQuotePlannerConfig | None = None) -> None:
        self.config = config or OutcomeEntryQuotePlannerConfig()

    def plan(self, item: EntryQuoteInput) -> EntryQuotePlan:
        if item.order_age_sec is None or item.order_age_sec < 0:
            return EntryQuotePlan(EntryQuoteAction.BLOCK, "entry_order_age_unavailable")
        if item.order_age_sec < self.config.min_requote_interval_sec:
            return EntryQuotePlan(EntryQuoteAction.KEEP, "entry_requote_interval_not_elapsed")
        if item.desired_side_index not in (0, 1):
            # Only an explicit fresh contradiction invalidates a quote.  A
            # stale/missing feed is never used as a cancellation instruction.
            if item.decision_reason in {"directional_confirmation_not_met", "selected_bid_in_no_trade_band"}:
                return EntryQuotePlan(EntryQuoteAction.CANCEL, "entry_signal_no_longer_confirmed")
            return EntryQuotePlan(EntryQuoteAction.KEEP, "entry_signal_not_actionable")
        if item.desired_bid is None or not Decimal("0") < item.desired_bid < Decimal("1"):
            return EntryQuotePlan(EntryQuoteAction.BLOCK, "entry_requote_book_unavailable")
        if item.desired_side_index != item.current_side_index:
            return EntryQuotePlan(EntryQuoteAction.CANCEL, "entry_side_changed_after_fresh_confirmation")
        minimum_delta = self.config.tick_size * max(1, self.config.min_price_delta_ticks)
        if abs(item.desired_bid - item.existing_price) < minimum_delta:
            return EntryQuotePlan(EntryQuoteAction.KEEP, "entry_quote_hysteresis")
        return EntryQuotePlan(EntryQuoteAction.CANCEL, "entry_first_level_bid_changed")


class EntryAccountReader(Protocol):
    def get_spot_clearinghouse_state_sync(self, user: str) -> dict[str, Any]: ...
    def get_open_orders_sync(self, user: str) -> list[dict[str, Any]]: ...


def _inventory(snapshot: dict[str, Any], coin: str) -> Decimal:
    alternate = "+" + coin[1:] if coin.startswith("#") else coin
    for row in snapshot.get("balances", []):
        if row.get("coin") in {coin, alternate}:
            return Decimal(str(row.get("total", "0")))
    return Decimal("0")


@dataclass(frozen=True)
class EntryRequoteResult:
    state: str
    detail: str
    old_order_id: str | None = None


class OutcomeEntryRequoteController:
    """One mutation only: cancel, confirm account truth, then next tick rebooks."""

    def __init__(self, *, account: EntryAccountReader, gateway: Any, store: OutcomeEntryLifecycleStore, wallet: str) -> None:
        self.account, self.gateway, self.store, self.wallet = account, gateway, store, wallet
        self._in_flight: set[tuple[int, str]] = set()

    def execute_cancel(self, *, market: OutcomeMarketSpec, side_index: int, lifecycle: OutcomeEntryLifecycle,
                       plan: EntryQuotePlan) -> EntryRequoteResult:
        key = (market.outcome_id, lifecycle.coin)
        if plan.action is not EntryQuoteAction.CANCEL:
            return EntryRequoteResult("blocked", "entry_plan_does_not_authorize_cancel", lifecycle.order_id)
        if key in self._in_flight:
            return EntryRequoteResult("blocked", "entry_requote_already_in_flight", lifecycle.order_id)
        self._in_flight.add(key)
        try:
            before_orders = self.account.get_open_orders_sync(self.wallet)
            owned = self.store.recover_or_adopt_audited_submit(
                wallet=self.wallet, outcome_id=market.outcome_id, coin=lifecycle.coin, open_orders=before_orders,
            )
            if owned is None or owned.order_id != lifecycle.order_id:
                return EntryRequoteResult("reconcile_required", "owned_entry_not_verified_from_account_truth", lifecycle.order_id)
            if _inventory(self.account.get_spot_clearinghouse_state_sync(self.wallet), lifecycle.coin) != 0:
                self.store.record(lifecycle, reason="inventory_present_before_entry_cancel", extra={"state": "RECONCILE_REQUIRED"})
                return EntryRequoteResult("reconcile_required", "inventory_present_before_entry_cancel", lifecycle.order_id)
            self.store.record(lifecycle, reason=plan.reason, extra={"state": "CANCEL_SUBMITTED"})
            try:
                self.gateway.cancel_owned_order(market=market, side_index=side_index, order_id=lifecycle.order_id)
            except Exception as exc:
                self.store.record(lifecycle, reason=f"entry_cancel_exception:{type(exc).__name__}", extra={"state": "RECONCILE_REQUIRED"})
                return EntryRequoteResult("reconcile_required", "entry_cancel_request_failed", lifecycle.order_id)
            after_orders = self.account.get_open_orders_sync(self.wallet)
            if any(str(row.get("oid")) == lifecycle.order_id for row in after_orders):
                self.store.record(lifecycle, reason="entry_cancel_not_confirmed", extra={"state": "RECONCILE_REQUIRED"})
                return EntryRequoteResult("reconcile_required", "old_entry_still_open_after_cancel", lifecycle.order_id)
            if _inventory(self.account.get_spot_clearinghouse_state_sync(self.wallet), lifecycle.coin) != 0:
                self.store.record(lifecycle, reason="entry_fill_or_inventory_change_during_cancel", extra={"state": "RECONCILE_REQUIRED"})
                return EntryRequoteResult("reconcile_required", "entry_inventory_changed_during_cancel", lifecycle.order_id)
            self.store.record(lifecycle, reason=plan.reason, extra={"state": "CANCELLED"})
            return EntryRequoteResult("cancelled", "entry_cancel_confirmed_requote_next_tick", lifecycle.order_id)
        finally:
            self._in_flight.discard(key)
