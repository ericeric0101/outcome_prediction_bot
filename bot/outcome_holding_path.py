"""Durable, as-of telemetry for an open Outcome inventory.

The module is deliberately journal-only: it does not decide or submit an
order.  Every observation is an event-time fact which can later be joined to
the eventual official fill or settlement without manufacturing a price path.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from monitoring.trade_journal_db import TradeJournalDB


@dataclass(frozen=True)
class OutcomeHoldingPathObservation:
    outcome_id: int
    period: str
    coin: str
    inventory: Decimal
    fill_vwap: Decimal
    best_bid: Decimal
    best_ask: Decimal
    maker_close_fee_rate: Decimal
    holding_age_sec: float
    time_left_sec: float
    book_health: str
    oi_evidence: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        executable_exit = self.best_bid * (Decimal("1") - self.maker_close_fee_rate)
        midpoint = (self.best_bid + self.best_ask) / Decimal("2")
        return {
            "venue": "hyperliquid_outcome", "outcome_id": self.outcome_id,
            "period": self.period, "coin": self.coin,
            "inventory": str(self.inventory), "fill_vwap": str(self.fill_vwap),
            "best_bid": str(self.best_bid), "best_ask": str(self.best_ask),
            "midpoint": str(midpoint), "executable_exit_price": str(executable_exit),
            "net_exit_vs_entry_pct": str(executable_exit / self.fill_vwap - Decimal("1")),
            "holding_age_sec": self.holding_age_sec, "time_left_sec": self.time_left_sec,
            "book_health": self.book_health, "oi_evidence": self.oi_evidence,
        }


class OutcomeHoldingPathRecorder:
    EVENT = "OUTCOME_HOLDING_PATH_OBSERVATION"

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id

    def record(self, observation: OutcomeHoldingPathObservation) -> None:
        self.journal.log_strategy_event(self.run_id, self.EVENT, observation.payload())
