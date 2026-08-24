"""Durable execution lifecycle evidence for the official Outcome runtime."""
from __future__ import annotations

from typing import Any

from bot.outcome_event_bridge import OutcomeFillEvent, OutcomeJournalBridge
from bot.outcome_maker_state_machine import MakerTickResult
from monitoring.trade_journal_db import TradeJournalDB


class OutcomeExecutionLedger:
    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id
        self.fill_bridge = OutcomeJournalBridge(journal, run_id)
        self._fill_ids: set[str] = set()

    def record_transition(self, *, market_id: int, coin: str | None, result: MakerTickResult) -> None:
        event = {
            "buy_placed": ("ORDER_SUBMIT", "BUY", "RESTING"),
            "sell_placed": ("ORDER_SUBMIT", "SELL", "RESTING"),
            "buy_resting": ("ORDER_RECONCILED", "BUY", "RESTING"),
            "sell_resting": ("ORDER_RECONCILED", "SELL", "RESTING"),
            "blocked": ("ORDER_RECONCILE_BLOCKED", None, "BLOCKED"),
        }.get(result.state)
        if not event:
            return
        event_type, side, status = event
        self.journal.log_order_event(
            self.run_id, event_type, venue_order_id=result.order_id, side=side, status=status,
            instrument_id=coin, reason=result.detail,
            payload={"venue": "hyperliquid_outcome", "outcome_id": market_id, "coin": coin, "runtime_state": result.state},
        )

    def sync_fills(self, *, fills: list[dict[str, Any]], market_key: str) -> int:
        recorded = 0
        for raw in fills:
            trade_id = str(raw.get("tid") or raw.get("hash") or "")
            if not trade_id or trade_id in self._fill_ids:
                continue
            try:
                fill = OutcomeFillEvent.from_user_fill(raw)
            except ValueError:
                continue
            self.fill_bridge.record_fill(fill, market_key=market_key)
            self._fill_ids.add(trade_id)
            recorded += 1
        return recorded
