"""E3/E4 read-only confidence entry and queue-aware quote decisions."""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Mapping

from bot.outcome_fair_value_model import OutcomeFairValueShadow


def live_feature_view(context: Mapping[str, object], *, time_left_sec: float) -> dict[str, Any]:
    continuation = context.get("trend_continuation")
    continuation = continuation if isinstance(continuation, Mapping) else {}
    return {
        "yes_bid": context.get("yes_best_bid"), "yes_ask": context.get("yes_best_ask"),
        "no_bid": context.get("no_best_bid"), "no_ask": context.get("no_best_ask"),
        "time_left_sec": time_left_sec,
        "btc_mark_return_300s_bps": context.get("mark_return_bps"),
        "btc_mark_return_900s_bps": continuation.get("mark_15m_bps"),
        "btc_mark_return_3600s_bps": continuation.get("mark_60m_bps"),
        "oi_return_300s_bps": context.get("oi_return_bps"),
        # Every B5 scoring call is an observation.  This timestamp supplies
        # the same Taipei market-session calendar features used offline.
        "observation_timestamp_ms": int(time.time() * 1000),
    }


class OutcomeConfidenceEntryShadow:
    def __init__(self, fair_value: OutcomeFairValueShadow | None = None) -> None:
        self.fair_value = fair_value or OutcomeFairValueShadow()

    def evaluate(self, *, context: Mapping[str, object], time_left_sec: float) -> dict[str, Any]:
        features = live_feature_view(context, time_left_sec=time_left_sec)
        scores: dict[str, Any] = {}
        for side_index, prefix in ((0, "yes"), (1, "no")):
            try:
                bid = float(features[f"{prefix}_bid"])
            except (TypeError, ValueError):
                scores[str(side_index)] = {"available": False, "reason": "side_bbo_unavailable", "live_authority": False}
                continue
            scores[str(side_index)] = self.fair_value.score(side_index=side_index, features=features, entry_bid=bid)
        available = [score for score in scores.values() if score.get("available")]
        candidate = max(available, key=lambda score: float(score.get("one_sided_95pct_lower_edge") or -99)) if available else None
        return {
            "read_only": True, "execution_submitted": False, "scores": scores,
            "candidate_side_index": candidate.get("side_index") if candidate else None,
            "candidate_confidence": candidate.get("confidence") if candidate else "unavailable",
            "candidate_lower_edge": candidate.get("one_sided_95pct_lower_edge") if candidate else None,
            "reason": "confidence_candidate_observed" if candidate else "fair_value_score_unavailable",
        }


class OutcomeQueueAwarePricingShadow:
    """Evaluate join/improve/skip without changing the production bid."""

    @staticmethod
    def _level(level: Mapping[str, object]) -> tuple[Decimal, Decimal] | None:
        try:
            price = Decimal(str(level.get("price", level.get("px"))))
            size = Decimal(str(level.get("size", level.get("sz"))))
        except (TypeError, ValueError, ArithmeticError):
            return None
        return (price, size) if Decimal("0") < price < Decimal("1") and size >= 0 else None

    def evaluate(
        self, *, side_index: int, book: Mapping[str, object], requested_shares: int,
        fair_score: Mapping[str, object], current_bid: Decimal,
    ) -> dict[str, Any]:
        bids = [parsed for parsed in (self._level(row) for row in book.get("bids", ())) if parsed is not None]
        asks = [parsed for parsed in (self._level(row) for row in book.get("asks", ())) if parsed is not None]
        if not bids or not asks or asks[0][0] <= bids[0][0]:
            return {"available": False, "reason": "valid_book_unavailable", "live_authority": False}
        if not fair_score.get("available"):
            return {"available": False, "reason": "fair_value_score_unavailable", "live_authority": False}
        bid, ask = bids[0][0], asks[0][0]
        differences = [abs(bids[index][0] - bids[index + 1][0]) for index in range(len(bids) - 1)]
        differences += [abs(asks[index + 1][0] - asks[index][0]) for index in range(len(asks) - 1)]
        tick = min((value for value in differences if value > 0), default=Decimal("0.00001"))
        lower_edge = Decimal(str(fair_score["one_sided_95pct_lower_edge"]))
        improved = min(bid + tick, ask - tick)
        can_improve = improved > bid and improved < ask and lower_edge > improved - current_bid
        if lower_edge <= 0:
            action, quote = "SKIP", None
        elif can_improve and fair_score.get("confidence") == "high":
            action, quote = "IMPROVE_ONE_TICK", improved
        else:
            action, quote = "JOIN_BEST_BID", bid
        queue_ahead = bids[0][1] if quote == bid else Decimal("0")
        fill_share_proxy = Decimal(requested_shares) / (queue_ahead + Decimal(requested_shares)) if requested_shares > 0 else Decimal("0")
        return {
            "available": True, "read_only": True, "execution_submitted": False,
            "side_index": side_index, "action": action, "shadow_quote": str(quote) if quote is not None else None,
            "production_bid_unchanged": str(current_bid), "best_bid": str(bid), "best_ask": str(ask),
            "spread": str(ask - bid), "observed_tick": str(tick), "queue_ahead_shares": str(queue_ahead),
            "requested_shares": requested_shares, "fill_share_proxy": str(fill_share_proxy),
            "fair_value_lower_edge": str(lower_edge), "reason": "shadow_quote_observed",
            "live_authority": False,
        }
