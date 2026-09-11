"""B3: deterministic action optimizer for active-strategy shadow scores.

It has no exchange or journal dependency.  The optimizer converts calibrated
model outputs into comparable actions while keeping all marketable actions
explicitly hypothetical until B6 receives separate operator authorization.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Mapping


@dataclass(frozen=True)
class ActiveSideInput:
    side_index: int
    bid: Decimal
    ask: Decimal
    score: Mapping[str, Any]


@dataclass(frozen=True)
class ActiveActionDecision:
    action: str
    side_index: int | None
    quote: Decimal | None
    utility: Decimal
    reason: str
    evidence: dict[str, Any]
    execution_submitted: bool = False
    live_authority: bool = False

    def payload(self) -> dict[str, Any]:
        result = asdict(self)
        result["quote"] = str(self.quote) if self.quote is not None else None
        result["utility"] = str(self.utility)
        return result


class OutcomeActiveActionOptimizer:
    """Choose the best shadow action by fee-aware lower-bound utility."""

    marketable_min_net_edge = Decimal("0.01")
    maker_min_net_edge = Decimal("0.0025")
    max_tail_probability = Decimal("0.35")
    tail_penalty = Decimal("0.03")
    capital_time_penalty_per_hour = Decimal("0.001")

    @staticmethod
    def _number(value: object) -> Decimal | None:
        try:
            return Decimal(str(value))
        except (ArithmeticError, TypeError, ValueError):
            return None

    def _candidate(
        self,
        item: ActiveSideInput,
        *,
        maker_close_fee: Decimal,
        taker_open_fee: Decimal,
        expected_holding_hours: Decimal,
    ) -> ActiveActionDecision:
        if not item.score.get("available") or not Decimal("0") < item.bid < item.ask < Decimal("1"):
            return ActiveActionDecision("WAIT", None, None, Decimal("-1"), "active_score_or_bbo_unavailable", {})
        outputs = item.score.get("outputs")
        outputs = outputs if isinstance(outputs, Mapping) else {}
        fair = outputs.get("future_bid_900s") or outputs.get("future_bid_300s")
        fair = fair if isinstance(fair, Mapping) else {}
        q10 = self._number(fair.get("q10"))
        mean_fair = self._number(fair.get("mean"))
        tail = self._number(outputs.get("breach_minus_10pct_1h")) or Decimal("0.5")
        target = self._number(outputs.get("hit_plus_1pct_1h")) or Decimal("0")
        fill_probability = self._number(item.score.get("maker_fill_probability")) or Decimal("0.5")
        if q10 is None or mean_fair is None:
            return ActiveActionDecision("WAIT", None, None, Decimal("-1"), "active_fair_lower_bound_unavailable", {})
        # A passive quote can remain unfilled, so its opportunity score uses
        # the calibrated mean and is discounted by measured fill probability.
        # A marketable entry pays immediately and therefore retains the much
        # stricter q10 lower-bound test.
        maker_edge = mean_fair * (Decimal("1") - maker_close_fee) / item.bid - Decimal("1")
        marketable_cost = item.ask * (Decimal("1") + taker_open_fee)
        marketable_edge = q10 * (Decimal("1") - maker_close_fee) / marketable_cost - Decimal("1")
        time_cost = expected_holding_hours * self.capital_time_penalty_per_hour
        maker_utility = fill_probability * maker_edge - tail * self.tail_penalty - time_cost
        marketable_utility = marketable_edge - tail * self.tail_penalty - time_cost
        evidence = {
            "fair_bid_q10_15m": str(q10),
            "fair_bid_mean_15m": str(mean_fair),
            "maker_net_edge": str(maker_edge),
            "marketable_net_edge": str(marketable_edge),
            "tail_probability_minus_10pct": str(tail),
            "target_probability_plus_1pct": str(target),
            "maker_fill_probability": str(fill_probability),
            "expected_holding_hours": str(expected_holding_hours),
        }
        if marketable_edge >= self.marketable_min_net_edge and tail <= self.max_tail_probability and target >= Decimal("0.55"):
            return ActiveActionDecision("BOUNDED_MARKETABLE_BUY", item.side_index, item.ask, marketable_utility, "robust_fee_after_marketable_edge", evidence)
        if maker_edge < self.maker_min_net_edge:
            # Retain the best rejected side so B5 can measure what happened
            # after a WAIT without pretending that an order was submitted.
            return ActiveActionDecision("WAIT", item.side_index, None, maker_utility, "fair_lower_bound_does_not_cover_maker_threshold", evidence)
        spread = item.ask - item.bid
        if spread >= Decimal("0.005") and fill_probability < Decimal("0.50"):
            tick = Decimal("0.00001")
            quote = min(item.bid + tick, item.ask - tick)
            return ActiveActionDecision("IMPROVE_ONE_TICK", item.side_index, quote, maker_utility, "positive_edge_but_low_join_fill_probability", evidence)
        return ActiveActionDecision("JOIN_BEST_BID", item.side_index, item.bid, maker_utility, "positive_fee_after_maker_lower_bound", evidence)

    def choose_entry(
        self,
        candidates: tuple[ActiveSideInput, ...],
        *,
        regime: str | None,
        maker_close_fee: Decimal = Decimal("0.0004"),
        taker_open_fee: Decimal = Decimal("0.0007"),
        expected_holding_hours: Decimal = Decimal("0.25"),
    ) -> ActiveActionDecision:
        if regime in {"TRANSITION", "RANGE"}:
            return ActiveActionDecision("WAIT", None, None, Decimal("0"), "regime_prohibits_active_entry", {"regime": regime})
        decisions = [self._candidate(item, maker_close_fee=maker_close_fee, taker_open_fee=taker_open_fee, expected_holding_hours=expected_holding_hours) for item in candidates]
        actionable = [decision for decision in decisions if decision.side_index in (0, 1) and decision.action != "WAIT"]
        if not actionable:
            best = max(decisions, key=lambda decision: decision.utility, default=None)
            return best or ActiveActionDecision("WAIT", None, None, Decimal("0"), "no_active_candidate", {})
        return max(actionable, key=lambda decision: decision.utility)

    def choose_holding(
        self,
        *,
        side_index: int,
        net_executable_return: Decimal,
        score: Mapping[str, Any],
        regime: str | None,
        toxic_state: str | None = None,
    ) -> ActiveActionDecision:
        outputs = score.get("outputs") if isinstance(score, Mapping) else None
        outputs = outputs if isinstance(outputs, Mapping) else {}
        recovery = self._number(outputs.get("recovered_after_minus_10pct_1h"))
        tail = self._number(outputs.get("breach_minus_20pct_1h"))
        evidence = {
            "net_executable_return": str(net_executable_return),
            "recovery_probability_after_minus_10pct": str(recovery) if recovery is not None else None,
            "tail_probability_minus_20pct": str(tail) if tail is not None else None,
            "regime": regime,
            "toxic_state": toxic_state,
        }
        if net_executable_return >= Decimal("0.01"):
            return ActiveActionDecision("MARKETABLE_PROFIT_EXIT", side_index, None, net_executable_return, "fee_after_profit_available", evidence)
        thesis_bad = regime in {"TRANSITION", "RANGE"} or toxic_state == "TOXIC_FILL"
        if (
            net_executable_return <= Decimal("-0.10")
            and recovery is not None and recovery < Decimal("0.35")
            and tail is not None and tail > Decimal("0.50")
            and thesis_bad
        ):
            return ActiveActionDecision("BOUNDED_RISK_EXIT", side_index, None, net_executable_return, "joint_low_recovery_tail_and_thesis_failure", evidence)
        return ActiveActionDecision("HOLD_OR_PASSIVE_EXIT", side_index, None, net_executable_return, "marketable_exit_not_justified", evidence)
