"""P0 audit trail for recurring Outcome market specifications and settlement evidence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from monitoring.trade_journal_db import TradeJournalDB


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class OutcomeMarketSpecEvidence:
    outcome_id: int
    description: str
    quote_token: str
    side_names: tuple[str, ...]
    raw_hash: str
    raw: Mapping[str, Any]

    @classmethod
    def from_meta(cls, market: OutcomeMarketSpec, raw: Mapping[str, Any]) -> "OutcomeMarketSpecEvidence":
        side_specs = raw.get("sideSpecs") or []
        names = tuple(str(item.get("name", "")) for item in side_specs if isinstance(item, Mapping))
        if len(names) != 2:
            raise ValueError(f"Outcome {market.outcome_id} must expose exactly two sideSpecs: {raw!r}")
        return cls(
            outcome_id=market.outcome_id,
            description=str(raw.get("description", "")),
            quote_token=str(raw.get("quoteToken", "")),
            side_names=names,
            raw_hash=_canonical_hash(raw), raw=raw,
        )


class OutcomeSpecAudit:
    """Persist facts; it deliberately cannot infer a winning side from a price."""

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal = journal
        self.run_id = run_id
        self._observed_hashes: set[str] = set()
        self._pending_outcomes: set[int] = set()

    def observe(self, market: OutcomeMarketSpec, raw: Mapping[str, Any]) -> None:
        evidence = OutcomeMarketSpecEvidence.from_meta(market, raw)
        if evidence.raw_hash in self._observed_hashes:
            return
        self._observed_hashes.add(evidence.raw_hash)
        self.journal.log_strategy_event(self.run_id, "OUTCOME_MARKET_SPEC_OBSERVED", {
            "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
            "underlying": market.underlying, "period": market.period,
            "expiry_timestamp": market.expiry_timestamp, "expiry_str": market.expiry_str,
            "target_price": market.target_price, "quote_token": evidence.quote_token,
            "side_names": evidence.side_names, "raw_spec_hash": evidence.raw_hash,
            "raw_outcome_meta": evidence.raw,
        })

    def mark_pending_resolution(self, market: OutcomeMarketSpec, now_ts: int) -> None:
        if now_ts < market.expiry_timestamp or market.outcome_id in self._pending_outcomes:
            return
        self._pending_outcomes.add(market.outcome_id)
        self.journal.log_strategy_event(self.run_id, "OUTCOME_RESOLUTION_PENDING", {
            "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
            "expiry_timestamp": market.expiry_timestamp,
            "reason": "awaiting_official_resolution_evidence",
            "winning_side": None, "settlement_fee": None,
        })

    def record_official_resolution(
        self, *, outcome_id: int, winning_side_index: int, payout_per_share: Decimal,
        settlement_fee_per_share: Optional[Decimal], source: str, raw: Mapping[str, Any],
    ) -> None:
        """Record only a caller-supplied official resolution payload."""
        if winning_side_index not in (0, 1):
            raise ValueError("winning_side_index must be 0 or 1")
        if payout_per_share < 0 or payout_per_share > 1:
            raise ValueError("payout_per_share must be in [0, 1]")
        if not source.startswith("official_"):
            raise ValueError("resolution source must be explicitly official")
        self.journal.log_strategy_event(self.run_id, "OUTCOME_RESOLUTION_CONFIRMED", {
            "venue": "hyperliquid_outcome", "outcome_id": outcome_id,
            "winning_side_index": winning_side_index, "payout_per_share": payout_per_share,
            "settlement_fee_per_share": settlement_fee_per_share,
            "settlement_source": source, "raw_resolution": dict(raw),
        })
