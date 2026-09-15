"""Shadow-only quality labels for resting Outcome BUYs and fresh fills.

This module is deliberately a decision *producer*, not an execution policy.
It has no account, gateway, controller, SDK, journal, or mutation imports.
The runtime may persist its compact output for later comparison with official
fills and P3 markouts, but none of these labels can cancel, reprice, or cross
the book.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


@dataclass(frozen=True)
class OutcomeRestingBuyQualityInput:
    outcome_id: int
    period: str
    coin: str
    order_id: str
    side_index: int
    order_price: Decimal
    order_age_sec: float | None
    current_signal_side_index: int | None
    current_signal_reason: str
    entry_audit: Mapping[str, object]
    market_context: Mapping[str, object]


@dataclass(frozen=True)
class OutcomePostFillQualityInput:
    outcome_id: int
    period: str
    coin: str
    order_id: str
    fill_trade_id: str
    fill_vwap: Decimal
    holding_age_sec: float
    best_bid: Decimal
    best_ask: Decimal
    top3_bid_depth: Decimal


def _decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return result


class OutcomeEntryQualityShadow:
    """Predeclare counterfactual entry actions without granting authority.

    A 30-second age is intentionally an *analysis bucket*, not a cancellation
    threshold.  It lets the report compare signal-decayed orders with fresh
    orders before anyone proposes a live stale-order cancellation canary.
    """

    schema_version = 1
    stale_quote_age_sec = 30.0
    post_fill_watch_loss_pct = Decimal("-0.03")

    @staticmethod
    def _bbo(context: Mapping[str, object], side_index: int) -> tuple[Decimal | None, Decimal | None]:
        prefix = "yes" if side_index == 0 else "no"
        bid = _decimal(context.get(f"{prefix}_best_bid"))
        ask = _decimal(context.get(f"{prefix}_best_ask"))
        if bid is None or ask is None or not Decimal("0") < bid < ask < Decimal("1"):
            return None, None
        return bid, ask

    def evaluate_resting_buy(self, item: OutcomeRestingBuyQualityInput) -> dict[str, Any]:
        bid, ask = self._bbo(item.market_context, item.side_index)
        if item.current_signal_side_index not in (0, 1):
            signal_state = "SIGNAL_DECAY"
        elif item.current_signal_side_index != item.side_index:
            signal_state = "SIDE_FLIP"
        else:
            signal_state = "SAME_SIDE_CONFIRMED"
        age = item.order_age_sec
        stale_eligible = bool(
            age is not None and age >= self.stale_quote_age_sec
            and signal_state in {"SIGNAL_DECAY", "SIDE_FLIP"}
        )
        quote_off_touch = bool(bid is not None and item.order_price > bid)
        crossing_cost_bps = (
            (ask / item.order_price - Decimal("1")) * Decimal("10000")
            if ask is not None and item.order_price > 0 else None
        )
        # This is intentionally a policy comparison, not a recommendation.
        hybrid_action = (
            "IMPROVE_ONE_TICK_SHADOW"
            if signal_state == "SAME_SIDE_CONFIRMED" and bid is not None and item.order_price == bid
            else "JOIN_BASELINE_SHADOW"
        )
        return {
            "schema_version": self.schema_version, "read_only": True,
            "live_authority": False, "execution_submitted": False,
            "outcome_id": item.outcome_id, "period": item.period,
            "coin": item.coin, "order_id": item.order_id, "side_index": item.side_index,
            "order_price": str(item.order_price), "order_age_sec": round(age, 3) if age is not None else None,
            "current_signal_side_index": item.current_signal_side_index,
            "current_signal_reason": item.current_signal_reason,
            "signal_state": signal_state,
            "best_bid": str(bid) if bid is not None else None,
            "best_ask": str(ask) if ask is not None else None,
            "quote_off_touch": quote_off_touch if bid is not None else None,
            "ioc_cross_cost_bps_from_resting_quote": str(crossing_cost_bps) if crossing_cost_bps is not None else None,
            "entry_tier": item.entry_audit.get("entry_tier"),
            "entry_time_left_sec": item.entry_audit.get("entry_time_left_sec"),
            "baseline_policy": {"action": "JOIN_BEST_BID", "live_authority": True},
            "fast_hybrid_shadow": {"action": hybrid_action, "live_authority": False},
            "stale_cancel_shadow": {
                "action": "CANCEL_STALE_SHADOW" if stale_eligible else "KEEP_SHADOW",
                "eligible": stale_eligible, "minimum_age_sec": self.stale_quote_age_sec,
                "reason": "signal_no_longer_same_side" if stale_eligible else "not_predeclared_stale_cancel_condition",
                "live_authority": False,
            },
            "ioc_counterfactual": {
                "action": "PRICE_PROTECTED_IOC_COUNTERFACTUAL" if ask is not None else "UNAVAILABLE_NO_ASOF_BBO",
                "ask": str(ask) if ask is not None else None,
                "cross_cost_bps_from_resting_quote": str(crossing_cost_bps) if crossing_cost_bps is not None else None,
                "live_authority": False,
            },
            "limits": [
                "no cancel/reprice/IOC authority",
                "no new REST request; BBO is only caller-provided as-of context",
                "L2 cannot determine queue priority or passive fill probability",
            ],
        }

    def evaluate_post_fill(self, item: OutcomePostFillQualityInput) -> dict[str, Any]:
        executable_return = item.best_bid / item.fill_vwap - Decimal("1") if item.fill_vwap > 0 else None
        scratch_watch = bool(
            executable_return is not None
            and executable_return <= self.post_fill_watch_loss_pct
            and item.holding_age_sec <= 120.0
        )
        return {
            "schema_version": self.schema_version, "read_only": True,
            "live_authority": False, "execution_submitted": False,
            "outcome_id": item.outcome_id, "period": item.period,
            "coin": item.coin, "order_id": item.order_id, "fill_trade_id": item.fill_trade_id,
            "fill_vwap": str(item.fill_vwap), "holding_age_sec": round(item.holding_age_sec, 3),
            "best_bid": str(item.best_bid), "best_ask": str(item.best_ask),
            "top3_bid_depth": str(item.top3_bid_depth),
            "executable_return_pct": str(executable_return) if executable_return is not None else None,
            "post_fill_scratch_shadow": {
                "action": "SCRATCH_IOC_COUNTERFACTUAL" if scratch_watch else "KEEP_NORMAL_EXIT_SHADOW",
                "watch_loss_pct": str(self.post_fill_watch_loss_pct),
                "eligible": scratch_watch,
                "live_authority": False,
            },
            "limits": [
                "not a stop-loss", "does not replace protective SELL or existing fast-failure/S3",
                "future P3/holding-path labels decide whether this would be a false positive",
            ],
        }
