"""Read-only evidence for otherwise-valid entries rejected by a wide spread."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from monitoring.trade_journal_db import TradeJournalDB


class OutcomeWideSpreadCandidateTracker:
    """Samples a rejected candidate every five minutes and labels later BBOs.

    It never changes an admission, order, or quote.  The five-minute sampling
    avoids turning a two-second strategy loop into a high-volume raw feed.
    """

    SAMPLE_INTERVAL_MS = 5 * 60 * 1000

    def __init__(self, *, journal: TradeJournalDB, run_id: str) -> None:
        self.journal = journal
        self.run_id = run_id

    @staticmethod
    def _value(value: object) -> str | None:
        return str(value) if value is not None else None

    def observe_rejection(
        self, *, market: OutcomeMarketSpec, coin: str, observed_at_ms: int,
        bid: Decimal, ask: Decimal | None, spread_bps: Decimal | None,
        entry_tier: str, time_left_sec: int, regime: Mapping[str, object] | None,
        requested_shares: int | None, safe_max_shares: Decimal | None,
        top3_depth_shares: Decimal | None, recent_trade_shares_5m: Decimal | None,
    ) -> bool:
        if not coin or ask is None or spread_bps is None:
            return False
        candidate_id = f"{market.outcome_id}:{coin}:{observed_at_ms // self.SAMPLE_INTERVAL_MS}"
        payload = {
            "venue": "hyperliquid_outcome", "read_only": True,
            "candidate_id": candidate_id, "outcome_id": market.outcome_id,
            "period": market.period, "coin": coin, "observed_at_ms": observed_at_ms,
            "entry_bid": self._value(bid), "entry_ask": self._value(ask),
            "entry_spread_bps": self._value(spread_bps), "entry_tier": entry_tier,
            "time_left_sec": time_left_sec, "requested_shares": requested_shares,
            "safe_max_shares": self._value(safe_max_shares),
            "top3_depth_shares": self._value(top3_depth_shares),
            "recent_trade_shares_5m": self._value(recent_trade_shares_5m),
            "regime_state": (regime or {}).get("state"),
            "regime_reason": (regime or {}).get("reason"),
            "reason": "entry_spread_exceeds_calibrated_ceiling",
        }
        inserted = self.journal.record_outcome_wide_spread_candidate(
            candidate_id=candidate_id, outcome_id=market.outcome_id, period=market.period,
            coin=coin, observed_at_ms=observed_at_ms, payload=payload,
        )
        if inserted:
            self.journal.log_strategy_event(self.run_id, "OUTCOME_WIDE_SPREAD_CANDIDATE", payload)
        return inserted

    def observe_book(
        self, *, market: OutcomeMarketSpec, coin: str, observed_at_ms: int,
        best_bid: Decimal | None, best_ask: Decimal | None,
    ) -> int:
        if best_bid is None or best_ask is None:
            return 0
        due = self.journal.due_outcome_wide_spread_candidate_paths(
            outcome_id=market.outcome_id, period=market.period, coin=coin,
            observed_at_ms=observed_at_ms,
        )
        written = 0
        for candidate_id, horizon_sec in due:
            payload = {
                "venue": "hyperliquid_outcome", "read_only": True,
                "candidate_id": candidate_id, "horizon_sec": horizon_sec,
                "observed_at_ms": observed_at_ms, "best_bid": str(best_bid),
                "best_ask": str(best_ask), "source": "p2_accepted_snapshot",
            }
            if self.journal.record_outcome_wide_spread_candidate_path(
                candidate_id=candidate_id, horizon_sec=horizon_sec, observed_at_ms=observed_at_ms,
                best_bid=str(best_bid), best_ask=str(best_ask), payload=payload,
            ):
                self.journal.log_strategy_event(self.run_id, "OUTCOME_WIDE_SPREAD_CANDIDATE_PATH", payload)
                written += 1
        return written
