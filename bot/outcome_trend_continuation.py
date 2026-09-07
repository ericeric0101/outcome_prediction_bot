"""Compact, read-only evidence capture for the S0 trend-continuation experiment.

The recorder never owns exchange credentials or submits an order.  It samples
the public BBO path of a candidate at a low fixed cadence so later research
does not need to replay the full raw L2 journal.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass
class _CandidatePath:
    outcome_id: int
    period: str
    episode_id: str
    side_index: int
    started_at: float
    entry_bid: Decimal


class OutcomeTrendContinuationRecorder:
    """Journal one compact BBO observation per candidate episode per 30 sec.

    Candidate observations expire after two hours.  This is deliberately a
    counterfactual BBO path, not a claim that a passive order would have
    filled at its starting bid.
    """

    MIN_INTERVAL_SEC = 30.0
    HORIZON_SEC = 2 * 60 * 60

    def __init__(self, journal: Any, run_id: str) -> None:
        self.journal = journal
        self.run_id = run_id
        self._paths: dict[tuple[int, str], _CandidatePath] = {}
        self._last_recorded_at: dict[tuple[int, str], float] = {}
        # A candidate episode is a bounded two-hour counterfactual.  Retain
        # its terminal identity for the life of this recorder so an eligible
        # signal that persists past the horizon cannot silently start a
        # second path with the same episode id and merge two samples in the
        # report.
        self._closed_episode_keys: set[tuple[int, str]] = set()

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        try:
            result = Decimal(str(value))
        except Exception:
            return None
        return result if result > 0 else None

    def observe(
        self,
        *,
        outcome_id: int,
        period: str,
        candidate: dict[str, object] | None,
        bbo_by_side: dict[int, tuple[object, object]],
    ) -> None:
        now = time.monotonic()
        if isinstance(candidate, dict) and bool(candidate.get("eligible")):
            try:
                side_index = int(candidate["side_index"])
                episode_id = str(candidate["episode_id"])
            except (KeyError, TypeError, ValueError):
                side_index, episode_id = -1, ""
            bid_ask = bbo_by_side.get(side_index)
            bid = self._decimal(bid_ask[0]) if bid_ask else None
            if side_index in (0, 1) and episode_id and bid is not None:
                key = (outcome_id, episode_id)
                if key not in self._paths and key not in self._closed_episode_keys:
                    self._paths[key] = _CandidatePath(
                        outcome_id=outcome_id, period=period, episode_id=episode_id,
                        side_index=side_index, started_at=now, entry_bid=bid,
                    )

        for key, path in list(self._paths.items()):
            if path.outcome_id != outcome_id:
                continue
            age = now - path.started_at
            terminal = age >= self.HORIZON_SEC
            if not terminal and now - self._last_recorded_at.get(key, float("-inf")) < self.MIN_INTERVAL_SEC:
                continue
            bid_ask = bbo_by_side.get(path.side_index)
            bid = self._decimal(bid_ask[0]) if bid_ask else None
            ask = self._decimal(bid_ask[1]) if bid_ask else None
            if bid is None or ask is None or ask <= bid:
                if terminal:
                    # A terminal BBO must be executable-quality evidence;
                    # do not manufacture a final price from stale/missing
                    # data.  The report will correctly retain this episode
                    # as incomplete rather than treat it as a two-hour path.
                    self._paths.pop(key, None)
                    self._last_recorded_at.pop(key, None)
                    self._closed_episode_keys.add(key)
                continue
            self.journal.log_strategy_event(self.run_id, "OUTCOME_TREND_CONTINUATION_PATH", {
                "venue": "hyperliquid_outcome", "read_only": True,
                "outcome_id": path.outcome_id, "period": path.period,
                "episode_id": path.episode_id, "side_index": path.side_index,
                "entry_bid": str(path.entry_bid), "best_bid": str(bid), "best_ask": str(ask),
                "age_sec": round(age, 3),
                "gross_bid_return_pct": str(bid / path.entry_bid - Decimal("1")),
                "terminal_observation": terminal,
                "counterfactual_limit": "public_bbo_path_only_no_maker_fill_or_pnl_inference",
            })
            self._last_recorded_at[key] = now
            if terminal:
                self._paths.pop(key, None)
                self._last_recorded_at.pop(key, None)
                self._closed_episode_keys.add(key)
