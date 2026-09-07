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
    tier_b_enabled: bool = False
    # A deliberately temporary, explicit kill switch for the only new live
    # strategy branch.  It is false by default; its numeric policy is
    # code-owned so an operator cannot accidentally tune it per launch.
    trend_continuation_enabled: bool = False

    @classmethod
    def from_env(cls) -> "OutcomeLiveStrategyConfig":
        value = cls(
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
            tier_b_enabled=os.environ.get("OUTCOME_TIER_B_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"},
            trend_continuation_enabled=os.environ.get("OUTCOME_TREND_CONTINUATION_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"},
        )
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
        self._spot_trend_side: int | None = None
        self._spot_trend_started_ms: int | None = None
        self._continuation_episode_id: str | None = None

    @staticmethod
    def _at_or_before(rows: list[tuple[Any, ...]], target_ms: int) -> tuple[Any, ...] | None:
        return next((row for row in rows[1:] if int(row[1]) <= target_ms), None)

    @staticmethod
    def _return_bps(now: Decimal, then: Decimal) -> Decimal | None:
        if now <= 0 or then <= 0:
            return None
        return (now / then - Decimal("1")) * Decimal("10000")

    @staticmethod
    def _percentile(values: list[Decimal], percentile: Decimal) -> Decimal | None:
        if not values:
            return None
        ordered = sorted(values)
        index = int((len(ordered) - 1) * float(percentile))
        return ordered[index]

    def _update_spot_trend(self, *, spot_strike_bps: Decimal, now_ms: int) -> tuple[int | None, int]:
        if spot_strike_bps >= self.config.spot_strike_min_bps:
            side = 0
        elif spot_strike_bps <= -self.config.spot_strike_min_bps:
            side = 1
        else:
            self._spot_trend_side, self._spot_trend_started_ms = None, None
            return None, 0
        if side != self._spot_trend_side:
            self._spot_trend_side, self._spot_trend_started_ms = side, now_ms
            self._continuation_episode_id = None
        return side, max(0, now_ms - int(self._spot_trend_started_ms or now_ms))

    def _five_min_abs_move_p75(self, rows: list[tuple[Any, ...]]) -> Decimal | None:
        """Use only live OI observations; no backfill or synthetic candles."""
        returns: list[Decimal] = []
        # Rows are newest first.  Find a roughly five-minute predecessor for
        # each sparse anchor, avoiding an assumed fixed collector cadence.
        for current in rows[::10]:
            previous = self._at_or_before(rows, int(current[1]) - 5 * 60 * 1000)
            if previous is None:
                continue
            try:
                value = abs(self._return_bps(Decimal(str(current[3])), Decimal(str(previous[3]))) or Decimal("0"))
            except (ValueError, ArithmeticError):
                continue
            if value > 0:
                returns.append(value)
        return self._percentile(returns, Decimal("0.75")) if len(returns) >= 8 else None

    def _trend_continuation(
        self,
        *,
        rows: list[tuple[Any, ...]],
        now_ms: int,
        mark_now: Decimal,
        spot_strike_bps: Decimal,
        mark_5m_bps: Decimal,
    ) -> dict[str, Any]:
        """Classify a small counter-trend 5m pullback without relaxing S0.

        This is evidence-first.  Its result is always journalled; only the
        explicit canary switch below permits it to become an entry decision.
        A restart resets the required 15-minute spot persistence, fail-closed.
        """
        side, persistence_ms = self._update_spot_trend(spot_strike_bps=spot_strike_bps, now_ms=now_ms)
        result: dict[str, Any] = {
            "eligible": False, "side_index": side,
            "spot_persistence_sec": round(persistence_ms / 1000, 3),
            "mark_5m_bps": str(mark_5m_bps), "reason": "continuation_spot_not_persistent",
        }
        if side not in (0, 1) or persistence_ms < 15 * 60 * 1000:
            self._continuation_episode_id = None
            return result
        direction = Decimal("1") if side == 0 else Decimal("-1")
        prior_15m = self._at_or_before(rows, int(rows[0][1]) - 15 * 60 * 1000)
        prior_60m = self._at_or_before(rows, int(rows[0][1]) - 60 * 60 * 1000)
        if prior_15m is None or prior_60m is None:
            self._continuation_episode_id = None
            result["reason"] = "continuation_long_horizon_unavailable"
            return result
        try:
            mark_15m = self._return_bps(mark_now, Decimal(str(prior_15m[3])))
            mark_60m = self._return_bps(mark_now, Decimal(str(prior_60m[3])))
        except (ValueError, ArithmeticError):
            mark_15m, mark_60m = None, None
        result.update({
            "mark_15m_bps": str(mark_15m) if mark_15m is not None else None,
            "mark_60m_bps": str(mark_60m) if mark_60m is not None else None,
        })
        if mark_15m is None or mark_60m is None or (
            direction * mark_15m < self.config.mark_return_min_bps
            or direction * mark_60m < self.config.mark_return_min_bps
        ):
            self._continuation_episode_id = None
            result["reason"] = "continuation_long_horizon_not_confirmed"
            return result
        # Strict S0 already handles a current 5m move in the trend direction.
        # Continuation is solely a *small opposing* pullback.
        if direction * mark_5m_bps >= 0:
            self._continuation_episode_id = None
            result["reason"] = "continuation_not_countertrend_pullback"
            return result
        p75 = self._five_min_abs_move_p75(rows)
        result["five_min_abs_move_p75_bps"] = str(p75) if p75 is not None else None
        if p75 is None:
            self._continuation_episode_id = None
            result["reason"] = "continuation_volatility_history_insufficient"
            return result
        if abs(mark_5m_bps) > p75:
            self._continuation_episode_id = None
            result["reason"] = "continuation_pullback_exceeds_volatility_scale"
            return result
        if self._continuation_episode_id is None:
            # The spot trend may persist through several independent small
            # pullbacks.  Episode identity starts at the pullback, not at the
            # broader trend, so C3 never merges separate candidate paths.
            self._continuation_episode_id = f"{side}:{now_ms}"
        result.update({"eligible": True, "episode_id": self._continuation_episode_id,
                       "reason": "continuation_small_countertrend_pullback"})
        return result

    def _gate_variants(self, *, spot_strike_bps: Decimal, mark_return_bps: Decimal,
                       oi_return_bps: Decimal) -> dict[str, dict[str, Any]]:
        """Return pre-registered counterfactual entry gates without changing S0.

        OI is an activity condition, not a directional sign: for DOWN the
        directional input remains the negative spot/mark move while an OI
        increase only confirms activity.  These rows are telemetry for later
        ablation; ``evaluate`` continues to execute *only* ``spot_mark_oi``.
        """
        up_spot = spot_strike_bps >= self.config.spot_strike_min_bps
        down_spot = spot_strike_bps <= -self.config.spot_strike_min_bps
        up_mark = mark_return_bps >= self.config.mark_return_min_bps
        down_mark = mark_return_bps <= -self.config.mark_return_min_bps
        oi_active = oi_return_bps >= self.config.oi_return_min_bps

        def choice(up: bool, down: bool) -> dict[str, Any]:
            # The thresholds cannot make both sides true, but preserve an
            # explicit fail-closed representation if future config changes.
            side = 0 if up and not down else 1 if down and not up else None
            return {"eligible": side is not None, "side_index": side}

        return {
            "spot_mark_oi": choice(up_spot and up_mark and oi_active, down_spot and down_mark and oi_active),
            "spot_mark": choice(up_spot and up_mark, down_spot and down_mark),
            "spot_mark_or_oi": choice(
                up_spot and (up_mark or oi_active),
                down_spot and (down_mark or oi_active),
            ),
        }

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
        prior = self._at_or_before(rows, prior_target)
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
        continuation = self._trend_continuation(
            rows=rows, now_ms=now_ms, mark_now=mark_now, spot_strike_bps=spot_strike_bps,
            mark_5m_bps=mark_return_bps,
        )
        evidence = {
            "oi_current_id": int(current[0]), "oi_prior_id": int(prior[0]), "oi_age_ms": age_ms,
            "spot_strike_bps": str(spot_strike_bps), "oi_return_bps": str(oi_return_bps),
            "mark_return_bps": str(mark_return_bps), "oi_lookback_sec": self.config.oi_lookback_sec,
            # Persist all alternatives on the ordinary S0 decision event so
            # an ablation report never needs to recompute a historical signal
            # from changed thresholds or revised OI observations.
            "gate_variants": self._gate_variants(
                spot_strike_bps=spot_strike_bps,
                mark_return_bps=mark_return_bps,
                oi_return_bps=oi_return_bps,
            ),
            "trend_continuation": continuation,
        }
        baseline = evidence["gate_variants"]["spot_mark_oi"]
        tier_b = evidence["gate_variants"]["spot_mark"]
        evidence["tier_b_enabled"] = self.config.tier_b_enabled
        # Keep the proven baseline as the preferred path.  Tier B relaxes
        # only the OI-direction/activity predicate: fresh OI observations
        # remain mandatory because they supply the Binance mark series.
        if baseline["eligible"]:
            side_index = int(baseline["side_index"])
            evidence["entry_tier"] = "tier_a_spot_mark_oi"
            return OutcomeOiEntryDecision(
                side_index,
                "up_spot_mark_oi_confirmed" if side_index == 0 else "down_spot_mark_oi_confirmed",
                evidence,
            )
        if self.config.tier_b_enabled and tier_b["eligible"]:
            side_index = int(tier_b["side_index"])
            evidence["entry_tier"] = "tier_b_spot_mark"
            return OutcomeOiEntryDecision(
                side_index,
                "up_spot_mark_tier_b_confirmed" if side_index == 0 else "down_spot_mark_tier_b_confirmed",
                evidence,
            )
        # A continuation decision is intentionally lower priority than both
        # existing S0 paths.  Until the canary is explicitly enabled it stays
        # a read-only counterfactual visible in every admission decision.
        if self.config.trend_continuation_enabled and continuation["eligible"]:
            side_index = int(continuation["side_index"])
            evidence["entry_tier"] = "tier_c_trend_continuation"
            return OutcomeOiEntryDecision(
                side_index,
                "up_trend_continuation_confirmed" if side_index == 0 else "down_trend_continuation_confirmed",
                evidence,
            )
        evidence["entry_tier"] = None
        return OutcomeOiEntryDecision(None, "directional_confirmation_not_met", evidence)
