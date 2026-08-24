"""Compact, journaled operational state for Outcome live infrastructure."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_stream_health import OutcomeStreamHealth
from monitoring.trade_journal_db import TradeJournalDB


@dataclass(frozen=True)
class OutcomeOperationalStatus:
    market_id: int
    period: str
    fallback_used: bool
    ws_ready: bool
    ws_reason: str
    automated_execution_enabled: bool


class OutcomeOperationsMonitor:
    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id
        self._last: OutcomeOperationalStatus | None = None

    def observe(self, *, market: OutcomeMarketSpec, fallback_used: bool, stream_health: OutcomeStreamHealth | None, automated_execution_enabled: bool) -> OutcomeOperationalStatus:
        health = stream_health.check(market) if stream_health else None
        status = OutcomeOperationalStatus(
            market_id=market.outcome_id, period=market.period, fallback_used=fallback_used,
            ws_ready=bool(health and health.ready), ws_reason=health.reason if health else "ws_health_not_configured",
            automated_execution_enabled=automated_execution_enabled,
        )
        if status != self._last:
            self.journal.log_strategy_event(self.run_id, "OUTCOME_OPERATIONAL_STATUS", asdict(status))
            self._last = status
        return status
