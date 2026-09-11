"""B5 live observer for the active challenger; permanently mutation-free."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from bot.outcome_active_dataset import feature_vector
from bot.outcome_active_decision import ActiveSideInput, OutcomeActiveActionOptimizer
from bot.outcome_active_model import OutcomeActiveModel
from bot.outcome_efficiency_shadow import live_feature_view


class OutcomeActiveChallengerShadow:
    def __init__(self, model: OutcomeActiveModel | None = None, optimizer: OutcomeActiveActionOptimizer | None = None) -> None:
        self.model = model or OutcomeActiveModel()
        self.optimizer = optimizer or OutcomeActiveActionOptimizer()

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        try:
            result = Decimal(str(value))
        except (ArithmeticError, TypeError, ValueError):
            return None
        return result

    def evaluate_entry(
        self,
        *,
        context: Mapping[str, object],
        time_left_sec: float,
        production_side_index: int | None,
        production_reason: str,
        regime: Mapping[str, object] | None,
    ) -> dict[str, Any]:
        features = live_feature_view(context, time_left_sec=time_left_sec)
        candidates: list[ActiveSideInput] = []
        scores: dict[str, Any] = {}
        for side_index, prefix in ((0, "yes"), (1, "no")):
            vector = feature_vector(features, side_index)
            bid, ask = self._decimal(features.get(f"{prefix}_bid")), self._decimal(features.get(f"{prefix}_ask"))
            if vector is None or bid is None or ask is None or bid <= 0:
                score = {"available": False, "reason": "active_live_features_incomplete", "live_authority": False}
            else:
                spread_bps = float((ask - bid) / ((ask + bid) / Decimal("2")) * Decimal("10000")) if ask > bid else None
                score = self.model.score(vector, spread_bps=spread_bps)
                if score.get("available"):
                    candidates.append(ActiveSideInput(side_index, bid, ask, score))
            scores[str(side_index)] = score
        regime_state = str(regime.get("state")) if isinstance(regime, Mapping) and regime.get("state") is not None else None
        decision = self.optimizer.choose_entry(tuple(candidates), regime=regime_state)
        return {
            "schema_version": 1,
            "read_only": True,
            "execution_submitted": False,
            "live_authority": False,
            "decision_kind": "entry",
            "production_side_index": production_side_index,
            "production_reason": production_reason,
            "regime_state": regime_state,
            "observed_context": {
                "yes_bid": features.get("yes_bid"), "yes_ask": features.get("yes_ask"),
                "no_bid": features.get("no_bid"), "no_ask": features.get("no_ask"),
                "time_left_sec": features.get("time_left_sec"),
                "btc_mark_return_300s_bps": features.get("btc_mark_return_300s_bps"),
                "btc_mark_return_900s_bps": features.get("btc_mark_return_900s_bps"),
                "btc_mark_return_3600s_bps": features.get("btc_mark_return_3600s_bps"),
                "oi_return_300s_bps": features.get("oi_return_300s_bps"),
            },
            "fee_assumptions": {"taker_open": "0.0007", "maker_close": "0.0004"},
            "scores": scores,
            "challenger": decision.payload(),
        }

    def evaluate_holding(
        self,
        *,
        context: Mapping[str, object],
        time_left_sec: float,
        side_index: int,
        fill_vwap: Decimal,
        marketable_net_exit_price: Decimal | None,
        regime: Mapping[str, object] | None,
        toxic_state: str | None = None,
    ) -> dict[str, Any]:
        features = live_feature_view(context, time_left_sec=time_left_sec)
        vector = feature_vector(features, side_index)
        prefix = "yes" if side_index == 0 else "no"
        bid, ask = self._decimal(features.get(f"{prefix}_bid")), self._decimal(features.get(f"{prefix}_ask"))
        if vector is None or bid is None or ask is None or bid <= 0:
            score = {"available": False, "reason": "active_holding_features_incomplete", "live_authority": False}
        else:
            spread_bps = float((ask - bid) / ((ask + bid) / Decimal("2")) * Decimal("10000")) if ask > bid else None
            score = self.model.score(vector, spread_bps=spread_bps)
        regime_state = str(regime.get("state")) if isinstance(regime, Mapping) and regime.get("state") is not None else None
        if marketable_net_exit_price is None or fill_vwap <= 0 or not score.get("available"):
            action = {
                "action": "HOLD_OR_PASSIVE_EXIT", "side_index": side_index, "quote": None,
                "utility": "0", "reason": "full_depth_or_active_score_unavailable",
                "evidence": {}, "execution_submitted": False, "live_authority": False,
            }
        else:
            action = self.optimizer.choose_holding(
                side_index=side_index,
                net_executable_return=marketable_net_exit_price / fill_vwap - Decimal("1"),
                score=score,
                regime=regime_state,
                toxic_state=toxic_state,
            ).payload()
        return {
            "schema_version": 1,
            "read_only": True,
            "execution_submitted": False,
            "live_authority": False,
            "decision_kind": "holding",
            "side_index": side_index,
            "regime_state": regime_state,
            "observed_context": {
                "side_bid": str(bid) if bid is not None else None,
                "side_ask": str(ask) if ask is not None else None,
                "time_left_sec": features.get("time_left_sec"),
            },
            "score": score,
            "challenger": action,
        }
