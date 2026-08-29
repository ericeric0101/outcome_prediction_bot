"""S0: bounded, explainable Binance OI + Outcome 1d live-entry gate.

This is deliberately a small rule-based experiment, not a trained model or a
claim that OI is predictive.  Missing, stale or contradictory public data
always means no entry.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OutcomeLiveStrategyConfig:
    max_daily_entries: int = 1
    target_return_pct: Decimal = Decimal("0.05")
    narrow_after_sec: int = 3600
    narrow_return_pct: Decimal = Decimal("0.03")
    floor_after_sec: int = 7200
    floor_return_pct: Decimal = Decimal("0.02")
    spot_strike_min_bps: Decimal = Decimal("15")
    mark_return_min_bps: Decimal = Decimal("5")
    oi_return_min_bps: Decimal = Decimal("1")
    oi_lookback_sec: int = 300
    oi_max_age_sec: int = 90
    min_entry_price: Decimal = Decimal("0.55")

    @classmethod
    def from_env(cls) -> "OutcomeLiveStrategyConfig":
        value = cls(
            max_daily_entries=int(os.environ.get("OUTCOME_LIVE_STRATEGY_MAX_DAILY_ENTRIES", "1")),
            target_return_pct=Decimal(os.environ.get("OUTCOME_LIVE_STRATEGY_TARGET_RETURN_PCT", "0.05")),
            narrow_after_sec=int(os.environ.get("OUTCOME_LIVE_STRATEGY_NARROW_AFTER_SEC", "3600")),
            narrow_return_pct=Decimal(os.environ.get("OUTCOME_LIVE_STRATEGY_NARROW_RETURN_PCT", "0.03")),
            floor_after_sec=int(os.environ.get("OUTCOME_LIVE_STRATEGY_FLOOR_AFTER_SEC", "7200")),
            floor_return_pct=Decimal(os.environ.get("OUTCOME_LIVE_STRATEGY_FLOOR_RETURN_PCT", "0.02")),
            spot_strike_min_bps=Decimal(os.environ.get("OUTCOME_LIVE_STRATEGY_SPOT_STRIKE_MIN_BPS", "15")),
            mark_return_min_bps=Decimal(os.environ.get("OUTCOME_LIVE_STRATEGY_MARK_RETURN_MIN_BPS", "5")),
            oi_return_min_bps=Decimal(os.environ.get("OUTCOME_LIVE_STRATEGY_OI_RETURN_MIN_BPS", "1")),
            oi_lookback_sec=int(os.environ.get("OUTCOME_LIVE_STRATEGY_OI_LOOKBACK_SEC", "300")),
            oi_max_age_sec=int(os.environ.get("OUTCOME_LIVE_STRATEGY_OI_MAX_AGE_SEC", "90")),
            min_entry_price=Decimal(os.environ.get("OUTCOME_LIVE_STRATEGY_MIN_ENTRY_PRICE", "0.55")),
        )
        if not 1 <= value.max_daily_entries <= 10:
            raise ValueError("OUTCOME_LIVE_STRATEGY_MAX_DAILY_ENTRIES must be in [1, 10]")
        if not (Decimal("0") <= value.floor_return_pct <= value.narrow_return_pct <= value.target_return_pct < Decimal("1")):
            raise ValueError("live strategy return tiers must satisfy 0 <= floor <= narrow <= target < 1")
        if value.narrow_after_sec <= 0 or value.floor_after_sec < value.narrow_after_sec:
            raise ValueError("live strategy exit ages must satisfy 0 < narrow <= floor")
        if value.oi_lookback_sec <= 0 or value.oi_max_age_sec <= 0:
            raise ValueError("live strategy OI windows must be positive")
        if min(value.spot_strike_min_bps, value.mark_return_min_bps, value.oi_return_min_bps) < 0:
            raise ValueError("live strategy thresholds must be non-negative")
        if not Decimal("0") < value.min_entry_price < Decimal("1"):
            raise ValueError("OUTCOME_LIVE_STRATEGY_MIN_ENTRY_PRICE must be in (0, 1)")
        return value


@dataclass(frozen=True)
class OutcomeOiEntryDecision:
    side_index: int | None
    reason: str
    evidence: dict[str, Any]


class OutcomeOiEntryGate:
    """Read only the locally persisted, live (never backfilled) OI stream."""

    def __init__(self, db_path: str | Path, config: OutcomeLiveStrategyConfig | None = None) -> None:
        self.db_path = str(db_path)
        self.config = config or OutcomeLiveStrategyConfig.from_env()

    def evaluate(self, *, spot_price: Decimal | None, strike_price: Decimal | None, now_ms: int | None = None) -> OutcomeOiEntryDecision:
        if spot_price is None or strike_price is None or spot_price <= 0 or strike_price <= 0:
            return OutcomeOiEntryDecision(None, "missing_spot_or_strike", {})
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        if not Path(self.db_path).exists():
            return OutcomeOiEntryDecision(None, "oi_journal_missing", {})
        try:
            with sqlite3.connect(f"file:{Path(self.db_path).resolve()}?mode=ro", uri=True) as conn:
                rows = conn.execute(
                    """
                    SELECT id, local_received_at_ms, open_interest, mark_price
                    FROM binance_oi_observations
                    WHERE symbol='BTCUSDT' AND backfilled=0
                      AND local_received_at_ms <= ?
                    ORDER BY local_received_at_ms DESC LIMIT 250
                    """, (now_ms,)
                ).fetchall()
        except sqlite3.Error:
            return OutcomeOiEntryDecision(None, "oi_read_failed", {})
        if not rows:
            return OutcomeOiEntryDecision(None, "oi_live_observation_missing", {})
        current = rows[0]
        age_ms = now_ms - int(current[1])
        if age_ms < 0 or age_ms > self.config.oi_max_age_sec * 1000:
            return OutcomeOiEntryDecision(None, "oi_observation_stale", {"oi_age_ms": age_ms})
        prior_target = int(current[1]) - self.config.oi_lookback_sec * 1000
        prior = next((row for row in rows[1:] if int(row[1]) <= prior_target), None)
        if prior is None:
            return OutcomeOiEntryDecision(None, "oi_lookback_unavailable", {"oi_age_ms": age_ms})
        try:
            oi_now, oi_then = Decimal(str(current[2])), Decimal(str(prior[2]))
            mark_now, mark_then = Decimal(str(current[3])), Decimal(str(prior[3]))
            if min(oi_now, oi_then, mark_now, mark_then) <= 0:
                raise ValueError
        except (ValueError, ArithmeticError):
            return OutcomeOiEntryDecision(None, "oi_or_mark_invalid", {"oi_age_ms": age_ms})
        spot_strike_bps = (spot_price / strike_price - Decimal("1")) * Decimal("10000")
        oi_return_bps = (oi_now / oi_then - Decimal("1")) * Decimal("10000")
        mark_return_bps = (mark_now / mark_then - Decimal("1")) * Decimal("10000")
        evidence = {
            "oi_current_id": int(current[0]), "oi_prior_id": int(prior[0]), "oi_age_ms": age_ms,
            "spot_strike_bps": str(spot_strike_bps), "oi_return_bps": str(oi_return_bps),
            "mark_return_bps": str(mark_return_bps), "oi_lookback_sec": self.config.oi_lookback_sec,
        }
        if (spot_strike_bps >= self.config.spot_strike_min_bps
                and mark_return_bps >= self.config.mark_return_min_bps
                and oi_return_bps >= self.config.oi_return_min_bps):
            return OutcomeOiEntryDecision(0, "up_spot_mark_oi_confirmed", evidence)
        if (spot_strike_bps <= -self.config.spot_strike_min_bps
                and mark_return_bps <= -self.config.mark_return_min_bps
                and oi_return_bps >= self.config.oi_return_min_bps):
            return OutcomeOiEntryDecision(1, "down_spot_mark_oi_confirmed", evidence)
        return OutcomeOiEntryDecision(None, "directional_confirmation_not_met", evidence)
