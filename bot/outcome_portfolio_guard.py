"""Code-owned portfolio limits for the explicitly bounded $20 Outcome canary."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal


TAIPEI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class OutcomePortfolioDecision:
    allowed: bool
    reason: str
    enabled: bool
    market_session_gross_entry_usdc: Decimal
    market_session_realized_net_usdc: Decimal
    market_session_gross_entry_limit_usdc: Decimal
    market_session_realized_loss_limit_usdc: Decimal
    rolling_24h_realized_net_usdc: Decimal
    rolling_24h_realized_loss_limit_usdc: Decimal
    consecutive_loss_exits: int
    market_loss_exits: int


class OutcomePortfolioGuard:
    """Outcome-session limits plus a wider rolling account-loss backstop."""

    PHASE_NOTIONAL = Decimal("20")
    MARKET_SESSION_ROLLOVER_HOUR = 14
    SETTLEMENT_ROLLOVER_GRACE_SEC = 5 * 60
    DAILY_GROSS_ENTRY_MULTIPLE = Decimal("10")
    MARKET_SESSION_REALIZED_LOSS_FRACTION = Decimal("0.40")
    ROLLING_24H_REALIZED_LOSS_MULTIPLE = Decimal("2")
    PAUSE_AFTER_CONSECUTIVE_LOSSES = 2
    PAUSE_SEC = 30 * 60
    STOP_MARKET_AFTER_LOSSES = 3

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    @classmethod
    def _market_session_bounds(cls, now: datetime | None = None) -> tuple[str, str, str]:
        current = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
        start = current.replace(hour=cls.MARKET_SESSION_ROLLOVER_HOUR, minute=0, second=0, microsecond=0)
        if current < start:
            start -= timedelta(days=1)
        end = start + timedelta(days=1)
        grace_end = start + timedelta(seconds=cls.SETTLEMENT_ROLLOVER_GRACE_SEC)
        return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(), grace_end.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _d(value: object) -> Decimal:
        try:
            return Decimal(str(value or "0"))
        except Exception:
            return Decimal("0")

    def evaluate(
        self, *, outcome_id: int, prospective_notional: Decimal,
        phase_entry_cap: Decimal, phase_exposure_cap: Decimal,
        now: datetime | None = None,
    ) -> OutcomePortfolioDecision:
        enabled = phase_entry_cap >= self.PHASE_NOTIONAL and phase_exposure_cap >= self.PHASE_NOTIONAL
        gross_limit = phase_entry_cap * self.DAILY_GROSS_ENTRY_MULTIPLE
        session_loss_limit = -(phase_entry_cap * self.MARKET_SESSION_REALIZED_LOSS_FRACTION)
        rolling_loss_limit = -(phase_entry_cap * self.ROLLING_24H_REALIZED_LOSS_MULTIPLE)
        zero = OutcomePortfolioDecision(
            True, "portfolio_guard_not_active_before_20_canary", False,
            Decimal("0"), Decimal("0"), gross_limit, session_loss_limit,
            Decimal("0"), rolling_loss_limit, 0, 0,
        )
        if not enabled:
            return zero
        current = (now or datetime.now(TAIPEI)).astimezone(timezone.utc)
        start, end, settlement_grace_end = self._market_session_bounds(current)
        rolling_start = (current - timedelta(hours=24)).isoformat()
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                gross_raw = conn.execute(
                    """SELECT COALESCE(SUM(CAST(price AS REAL) * CAST(qty AS REAL)), 0)
                       FROM order_events WHERE event_type='ORDER_FILLED' AND side='BUY' AND ts>? AND ts<?
                         AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                         AND json_extract(payload_json, '$.actual_fill')=1""", (start, end),
                ).fetchone()[0]
                rows = conn.execute(
                    """SELECT close_trade_id, outcome_id, SUM(CAST(realized_net_usdc AS REAL)), MAX(recorded_at)
                       FROM outcome_realized_pnl_lots
                       WHERE recorded_at>? AND recorded_at<?
                         AND NOT (close_kind='settlement' AND recorded_at<=?)
                       GROUP BY close_trade_id, outcome_id ORDER BY MAX(recorded_at) DESC""",
                    (start, end, settlement_grace_end),
                ).fetchall()
                rolling_raw = conn.execute(
                    "SELECT COALESCE(SUM(CAST(realized_net_usdc AS REAL)), 0) FROM outcome_realized_pnl_lots WHERE recorded_at>=?",
                    (rolling_start,),
                ).fetchone()[0]
        except sqlite3.Error:
            return OutcomePortfolioDecision(
                False, "portfolio_guard_journal_unavailable", True,
                Decimal("0"), Decimal("0"), gross_limit, session_loss_limit,
                Decimal("0"), rolling_loss_limit, 0, 0,
            )
        gross, realized, rolling = self._d(gross_raw), sum((self._d(row[2]) for row in rows), Decimal("0")), self._d(rolling_raw)
        consecutive = 0
        for row in rows:
            if self._d(row[2]) >= 0:
                break
            consecutive += 1
        market_losses = sum(1 for row in rows if int(row[1]) == int(outcome_id) and self._d(row[2]) < 0)
        fields = (gross, realized, gross_limit, session_loss_limit, rolling, rolling_loss_limit, consecutive, market_losses)
        if gross + prospective_notional > gross_limit:
            return OutcomePortfolioDecision(False, "portfolio_market_session_gross_entry_cap", True, *fields)
        if realized <= session_loss_limit:
            return OutcomePortfolioDecision(False, "portfolio_market_session_realized_loss_cap", True, *fields)
        if rolling <= rolling_loss_limit:
            return OutcomePortfolioDecision(False, "portfolio_rolling_24h_realized_loss_cap", True, *fields)
        if market_losses >= self.STOP_MARKET_AFTER_LOSSES:
            return OutcomePortfolioDecision(False, "portfolio_market_session_market_loss_stop", True, *fields)
        if consecutive >= self.PAUSE_AFTER_CONSECUTIVE_LOSSES:
            latest = datetime.fromisoformat(str(rows[0][3])) if rows else current
            if (current - latest).total_seconds() < self.PAUSE_SEC:
                return OutcomePortfolioDecision(False, "portfolio_market_session_consecutive_loss_pause", True, *fields)
        return OutcomePortfolioDecision(True, "portfolio_market_session_approved", True, *fields)
