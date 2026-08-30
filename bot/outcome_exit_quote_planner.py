"""Pure, fail-closed planning for Outcome passive exit quote replacement.

This module deliberately has no SDK, account, journal, or clock side effects.
It turns already-verified account truth and a fresh book into a decision that a
separate lifecycle controller may later execute.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from enum import StrEnum


class ExitQuoteAction(StrEnum):
    KEEP = "KEEP"
    CANCEL_REPLACE = "CANCEL_REPLACE"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class OutcomeExitQuotePlannerConfig:
    tick_size: Decimal = Decimal("0.00001")
    min_price_delta_ticks: int = 2
    min_requote_interval_sec: float = 60.0
    # Production E6 re-prices only when a durable lifecycle policy changes;
    # there is no arbitrary per-process replacement quota.  Rate limiting,
    # ownership confirmation and ALO non-crossing checks remain mandatory.
    max_replacements: int | None = None
    max_book_age_sec: float = 15.0


@dataclass(frozen=True)
class ExitQuoteInput:
    inventory: Decimal
    fill_vwap: Decimal | None
    maker_close_fee_rate: Decimal | None
    minimum_return_pct: Decimal | None
    loss_reprice_pct: Decimal | None
    existing_order_id: str | None
    existing_price: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    book_age_sec: float | None
    now_ts: float
    last_requote_ts: float | None = None
    replacement_count: int = 0


@dataclass(frozen=True)
class ExitQuotePlan:
    action: ExitQuoteAction
    reason: str
    target_price: Decimal | None = None
    floor_price: Decimal | None = None
    requested_shares: Decimal | None = None
    exit_mode: str | None = None


def _ceil_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


class OutcomeExitQuotePlanner:
    def __init__(self, config: OutcomeExitQuotePlannerConfig | None = None) -> None:
        self.config = config or OutcomeExitQuotePlannerConfig()

    def plan(self, item: ExitQuoteInput) -> ExitQuotePlan:
        cfg = self.config
        if item.inventory <= 0:
            return ExitQuotePlan(ExitQuoteAction.BLOCK, "no_verified_inventory")
        if item.fill_vwap is None or item.fill_vwap <= 0:
            return ExitQuotePlan(ExitQuoteAction.BLOCK, "missing_verified_fill_vwap")
        if item.maker_close_fee_rate is None or not Decimal("0") <= item.maker_close_fee_rate < Decimal("1"):
            return ExitQuotePlan(ExitQuoteAction.BLOCK, "invalid_maker_close_fee")
        if item.minimum_return_pct is None or not Decimal("0") <= item.minimum_return_pct < Decimal("1"):
            return ExitQuotePlan(ExitQuoteAction.BLOCK, "missing_verified_exit_policy")
        if not item.existing_order_id or item.existing_price is None or item.existing_price <= 0:
            return ExitQuotePlan(ExitQuoteAction.BLOCK, "no_owned_resting_sell")
        if item.best_bid is None or item.best_ask is None or item.best_bid <= 0 or item.best_ask <= item.best_bid:
            return ExitQuotePlan(ExitQuoteAction.BLOCK, "invalid_book")
        if item.book_age_sec is None or item.book_age_sec > cfg.max_book_age_sec:
            return ExitQuotePlan(ExitQuoteAction.BLOCK, "stale_book")
        if cfg.max_replacements is not None and item.replacement_count >= cfg.max_replacements:
            return ExitQuotePlan(ExitQuoteAction.BLOCK, "replacement_attempt_cap")
        if item.last_requote_ts is not None and item.now_ts - item.last_requote_ts < cfg.min_requote_interval_sec:
            return ExitQuotePlan(ExitQuoteAction.KEEP, "requote_interval_not_elapsed")

        fee_denominator = Decimal("1") - item.maker_close_fee_rate
        profit_target = item.fill_vwap * (Decimal("1") + item.minimum_return_pct) / fee_denominator
        floor = None
        midpoint = (item.best_bid + item.best_ask) / Decimal("2")
        if item.loss_reprice_pct is not None and Decimal("0") <= item.loss_reprice_pct < Decimal("1"):
            floor = item.fill_vwap * (Decimal("1") - item.loss_reprice_pct) / fee_denominator
        loss_mode = floor is not None and midpoint <= item.fill_vwap * (Decimal("1") - item.loss_reprice_pct)
        desired = floor if loss_mode else profit_target
        # ALO sell must be strictly above the latest bid.  Best ask is the
        # least aggressive passive anchor; the policy floor may be higher.
        desired = max(desired, item.best_ask, item.best_bid + cfg.tick_size)
        desired = _ceil_to_tick(desired, cfg.tick_size)
        if not Decimal("0") < desired < Decimal("1"):
            return ExitQuotePlan(ExitQuoteAction.BLOCK, "target_outside_outcome_bounds", floor_price=floor)
        if desired <= item.best_bid:
            return ExitQuotePlan(ExitQuoteAction.BLOCK, "replacement_would_cross_bid", floor_price=floor)

        delta = abs(desired - item.existing_price)
        min_delta = cfg.tick_size * max(1, cfg.min_price_delta_ticks)
        if delta < min_delta:
            return ExitQuotePlan(ExitQuoteAction.KEEP, "requote_hysteresis", desired, floor, item.inventory, "loss_band" if loss_mode else "take_profit")
        return ExitQuotePlan(ExitQuoteAction.CANCEL_REPLACE, "loss_band_reprice" if loss_mode else "target_reprice", desired, floor, item.inventory, "loss_band" if loss_mode else "take_profit")
