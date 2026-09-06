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
    daily_gross_entry_usdc: Decimal
    daily_realized_net_usdc: Decimal
    daily_gross_entry_limit_usdc: Decimal
    daily_realized_loss_limit_usdc: Decimal
    consecutive_loss_exits: int
    market_loss_exits: int


class OutcomePortfolioGuard:
    """Fail closed only for the approved $20 phase, not historical $11 live."""

    PHASE_NOTIONAL = Decimal("20")
    # Scale from the configured entry cap, rather than requiring a future
    # operator to remember to edit a second set of dollar limits.  At the
    # approved $20 canary these retain the documented $200 / -$8 limits.
    DAILY_GROSS_ENTRY_MULTIPLE = Decimal("10")
    DAILY_REALIZED_LOSS_FRACTION = Decimal("0.40")
    PAUSE_AFTER_CONSECUTIVE_LOSSES = 2
    PAUSE_SEC = 30 * 60
    STOP_MARKET_AFTER_LOSSES = 3

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    @staticmethod
    def _today_bounds() -> tuple[str, str]:
        now = datetime.now(TAIPEI)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc).isoformat(), (start + timedelta(days=1)).astimezone(timezone.utc).isoformat()

    @staticmethod
    def _d(value: object) -> Decimal:
        try:
            return Decimal(str(value or "0"))
        except Exception:
            return Decimal("0")

    def evaluate(self, *, outcome_id: int, prospective_notional: Decimal,
                 phase_entry_cap: Decimal, phase_exposure_cap: Decimal) -> OutcomePortfolioDecision:
        enabled = phase_entry_cap >= self.PHASE_NOTIONAL and phase_exposure_cap >= self.PHASE_NOTIONAL
        gross_limit = phase_entry_cap * self.DAILY_GROSS_ENTRY_MULTIPLE
        loss_limit = -(phase_entry_cap * self.DAILY_REALIZED_LOSS_FRACTION)
        zero = OutcomePortfolioDecision(
            True, "portfolio_guard_not_active_before_20_canary", False,
            Decimal("0"), Decimal("0"), gross_limit, loss_limit, 0, 0,
        )
        if not enabled:
            return zero
        start, end = self._today_bounds()
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                gross_raw = conn.execute(
                    """SELECT COALESCE(SUM(CAST(price AS REAL) * CAST(qty AS REAL)), 0)
                       FROM order_events
                       WHERE event_type='ORDER_FILLED' AND side='BUY' AND ts>=? AND ts<?
                         AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                         AND json_extract(payload_json, '$.actual_fill')=1""", (start, end),
                ).fetchone()[0]
                rows = conn.execute(
                    """SELECT close_trade_id, outcome_id, SUM(CAST(realized_net_usdc AS REAL)), MAX(recorded_at)
                       FROM outcome_realized_pnl_lots WHERE recorded_at>=? AND recorded_at<?
                       GROUP BY close_trade_id, outcome_id ORDER BY MAX(recorded_at) DESC""", (start, end),
                ).fetchall()
        except sqlite3.Error:
            return OutcomePortfolioDecision(
                False, "portfolio_guard_journal_unavailable", True,
                Decimal("0"), Decimal("0"), gross_limit, loss_limit, 0, 0,
            )
        gross = self._d(gross_raw)
        realized = sum((self._d(row[2]) for row in rows), Decimal("0"))
        consecutive = 0
        for row in rows:
            if self._d(row[2]) >= 0:
                break
            consecutive += 1
        market_losses = sum(1 for row in rows if int(row[1]) == int(outcome_id) and self._d(row[2]) < 0)
        decision = OutcomePortfolioDecision(
            True, "portfolio_canary_approved", True, gross, realized, gross_limit, loss_limit, consecutive, market_losses,
        )
        if gross + prospective_notional > gross_limit:
            return OutcomePortfolioDecision(
                False, "portfolio_daily_gross_entry_cap", True, gross, realized, gross_limit, loss_limit, consecutive, market_losses,
            )
        if realized <= loss_limit:
            return OutcomePortfolioDecision(
                False, "portfolio_daily_realized_loss_cap", True, gross, realized, gross_limit, loss_limit, consecutive, market_losses,
            )
        if market_losses >= self.STOP_MARKET_AFTER_LOSSES:
            return OutcomePortfolioDecision(
                False, "portfolio_daily_market_loss_stop", True, gross, realized, gross_limit, loss_limit, consecutive, market_losses,
            )
        if consecutive >= self.PAUSE_AFTER_CONSECUTIVE_LOSSES:
            latest = datetime.fromisoformat(str(rows[0][3])) if rows else datetime.now(timezone.utc)
            if (datetime.now(timezone.utc) - latest).total_seconds() < self.PAUSE_SEC:
                return OutcomePortfolioDecision(
                    False, "portfolio_consecutive_loss_pause", True, gross, realized, gross_limit, loss_limit, consecutive, market_losses,
                )
        return decision
