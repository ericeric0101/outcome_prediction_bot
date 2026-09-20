"""Bounded, public-data-only structural-collapse forward-validation observer.

This module intentionally imports no account, gateway, controller or SDK code.
It can only consume public books/trades and return compact research evidence.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any, Iterable


@dataclass(frozen=True)
class StructuralCollapseConfig:
    adverse_impulse_pp: Decimal = Decimal("0.10")
    minimum_recovery_fraction: Decimal = Decimal("0.40")
    renewed_drop_pp: Decimal = Decimal("0.08")
    depth_retention_threshold: Decimal = Decimal("0.70")
    participation_retention_threshold: Decimal = Decimal("0.25")
    bilateral_spread_bps_threshold: Decimal = Decimal("200")
    max_history_sec: float = 35 * 60
    max_samples_per_coin: int = 20_000


@dataclass(frozen=True)
class _Book:
    ts: float
    bid: Decimal
    ask: Decimal
    depth: Decimal

    @property
    def spread_bps(self) -> Decimal:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 10_000 if mid > 0 else Decimal("0")


class OutcomeStructuralCollapseShadow:
    """Process-local shadow state.  Its public API cannot mutate execution."""

    _BAR_SEC = 300.0

    def __init__(self, config: StructuralCollapseConfig | None = None) -> None:
        self.config = config or StructuralCollapseConfig()
        self._books: dict[str, deque[_Book]] = {}
        self._trades: dict[str, deque[tuple[float, str]]] = {}
        self._trade_ids: dict[str, set[str]] = {}
        self._bar_closes: dict[str, dict[int, Decimal]] = {}
        self._current_bar: dict[str, tuple[int, Decimal]] = {}
        self._watch: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        try:
            candidate = Decimal(str(value))
        except (ArithmeticError, TypeError, ValueError):
            return None
        return candidate if candidate.is_finite() else None

    @staticmethod
    def _trade_id(row: dict[str, Any]) -> str:
        # Historical A/B mirrors describe the same economic trade.  Side is
        # deliberately excluded from the identity.
        for key in ("tid", "tradeId", "trade_id", "hash", "txHash", "tx_hash"):
            if row.get(key) not in (None, ""):
                return f"{key}:{row[key]}"
        return "fallback:" + "|".join(str(row.get(key, "")) for key in ("time", "timestamp", "px", "price", "sz", "size"))

    @staticmethod
    def _book(payload: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal] | None:
        levels = payload.get("levels")
        if not isinstance(levels, list) or len(levels) < 2 or not isinstance(levels[0], list) or not isinstance(levels[1], list):
            return None
        if not levels[0] or not levels[1]:
            return None
        bid = OutcomeStructuralCollapseShadow._decimal(levels[0][0].get("px", levels[0][0].get("price")))
        ask = OutcomeStructuralCollapseShadow._decimal(levels[1][0].get("px", levels[1][0].get("price")))
        if bid is None or ask is None or not (Decimal("0") < bid < ask < Decimal("1")):
            return None
        depth = sum((OutcomeStructuralCollapseShadow._decimal(row.get("sz", row.get("size"))) or Decimal("0") for row in levels[0][:3]), Decimal("0"))
        return bid, ask, depth

    def observe_l2(self, *, coin: str, timestamp: float, payload: dict[str, Any]) -> None:
        parsed = self._book(payload)
        if parsed is None:
            return
        bid, ask, depth = parsed
        books = self._books.setdefault(coin, deque())
        books.append(_Book(timestamp, bid, ask, depth))
        self._trim(coin, timestamp)
        bucket = int(timestamp // self._BAR_SEC)
        # A bucket becomes usable only when a later bucket is observed.  This
        # avoids treating the current five-minute midpoint as a completed bar.
        previous = self._current_bar.get(coin)
        if previous is not None and bucket > previous[0]:
            self._bar_closes.setdefault(coin, {})[previous[0]] = previous[1]
            self._update_watch(coin)
        self._current_bar[coin] = (bucket, (bid + ask) / 2)

    def observe_trades(self, *, coin: str, items: Iterable[dict[str, Any]], received_at: float) -> None:
        trades, ids = self._trades.setdefault(coin, deque()), self._trade_ids.setdefault(coin, set())
        for row in items:
            if not isinstance(row, dict):
                continue
            raw = row.get("time", row.get("timestamp"))
            try:
                ts = float(raw)
                ts = ts / 1000 if ts > 10_000_000_000 else ts
            except (TypeError, ValueError):
                ts = received_at
            key = self._trade_id(row)
            if key not in ids:
                trades.append((ts, key)); ids.add(key)
        self._trim(coin, received_at)

    def _trim(self, coin: str, now: float) -> None:
        cutoff = now - self.config.max_history_sec
        while self._books.get(coin) and self._books[coin][0].ts < cutoff:
            self._books[coin].popleft()
        while len(self._books.get(coin, ())) > self.config.max_samples_per_coin:
            self._books[coin].popleft()
        ids = self._trade_ids.setdefault(coin, set())
        while self._trades.get(coin) and self._trades[coin][0][0] < cutoff:
            ids.discard(self._trades[coin].popleft()[1])
        while len(self._trades.get(coin, ())) > self.config.max_samples_per_coin:
            ids.discard(self._trades[coin].popleft()[1])
        oldest_bucket = int(cutoff // self._BAR_SEC) - 1
        for bucket in list(self._bar_closes.get(coin, {})):
            if bucket < oldest_bucket:
                del self._bar_closes[coin][bucket]

    def _update_watch(self, coin: str) -> None:
        if coin in self._watch:
            return
        closes = [(bucket, price) for bucket, price in sorted(self._bar_closes.get(coin, {}).items())]
        if len(closes) < 4:
            return
        peak = closes[0][1]; trough: Decimal | None = None; recovery_high: Decimal | None = None; recovered = False
        for bucket, price in closes[1:]:
            if trough is None:
                peak = max(peak, price)
                if peak - price >= self.config.adverse_impulse_pp:
                    trough, recovery_high = price, price
                continue
            recovery_high = max(recovery_high or price, price)
            drawdown = max(peak - trough, Decimal("0.000000001"))
            if price >= trough + drawdown * self.config.minimum_recovery_fraction:
                recovered = True
            if recovered and recovery_high - price >= self.config.renewed_drop_pp:
                self._watch[coin] = {"trigger_ts": (bucket + 1) * self._BAR_SEC, "trigger_price": price, "peak": peak, "trough": trough, "recovery_high": recovery_high}
                return

    @staticmethod
    def _median(values: list[Decimal]) -> Decimal | None:
        return Decimal(str(median(values))) if values else None

    def _books_in(self, coin: str, lo: float, hi: float) -> list[_Book]:
        return [row for row in self._books.get(coin, ()) if lo < row.ts <= hi]

    def _rate(self, coin: str, lo: float, hi: float) -> Decimal | None:
        if hi <= lo:
            return None
        return Decimal(sum(1 for ts, _ in self._trades.get(coin, ()) if lo < ts <= hi)) / Decimal(str(hi - lo)) * 60

    def _retention(self, coin: str, trigger: float, lo: float, hi: float) -> Decimal | None:
        baseline, current = self._rate(coin, trigger - 900, trigger), self._rate(coin, trigger + lo, trigger + hi)
        return current / baseline if baseline is not None and current is not None and baseline > 0 else None

    def evaluate(self, *, lifecycle_id: str, outcome_id: int, period: str, held_coin: str, yes_coin: str, no_coin: str, now: float, position_size: Decimal | None = None, entry_price: Decimal | None = None) -> dict[str, Any]:
        watch = self._watch.get(held_coin)
        base: dict[str, Any] = {
            "schema_version": 1, "read_only": True, "live_authority": False, "execution_submitted": False,
            "outcome_id": outcome_id, "period": period, "entry_lifecycle_id": lifecycle_id, "held_coin": held_coin, "yes_coin": yes_coin, "no_coin": no_coin, "timestamp": now,
            "entry_price": str(entry_price) if entry_price is not None else None, "position_size": str(position_size) if position_size is not None else None,
            "failed_recovery": {"active": watch is not None, "trigger_ts": watch.get("trigger_ts") if watch else None, "trigger_price": str(watch["trigger_price"]) if watch else None, "pre_shock_peak": str(watch["peak"]) if watch else None, "trough": str(watch["trough"]) if watch else None, "recovery_high": str(watch["recovery_high"]) if watch else None},
            "branch_a": {"eligible": False, "reason": "watch_not_started"}, "branch_b": {"eligible": False, "reason": "watch_not_started"}, "candidate": False, "candidate_branches": [],
            "state": "WAITING_FOR_FAILED_RECOVERY", "promotion_boundary": {"shadow_only": True, "may_submit_order": False, "may_cancel_order": False, "may_replace_order": False, "may_veto_existing_safety_lane": False},
        }
        if watch is None:
            return base
        trigger, age = float(watch["trigger_ts"]), now - float(watch["trigger_ts"])
        branches: list[str] = []
        if age >= 900:
            pre, post = self._books_in(held_coin, trigger - 300, trigger), self._books_in(held_coin, trigger, trigger + 900)
            a, b = self._median([x.depth for x in pre]), self._median([x.depth for x in post])
            retention = b / a if a is not None and b is not None and a > 0 else None
            eligible = retention is not None and retention < self.config.depth_retention_threshold
            base["branch_a"] = {"eligible": eligible, "reason": "depth_retention_below_threshold" if eligible else "insufficient_or_above_threshold", "confirm_after_sec": 900, "pre_depth_median": str(a) if a is not None else None, "post_depth_median": str(b) if b is not None else None, "depth_retention": str(retention) if retention is not None else None, "threshold": str(self.config.depth_retention_threshold)}
            if eligible: branches.append("A_DEPTH_FAILURE")
        windows: dict[str, Any] = {}
        for name, lo, hi in (("early", 0, 300), ("late", 300, 900)):
            if age < hi:
                windows[name] = {"eligible": False, "reason": "window_not_complete"}; continue
            yr, nr = self._retention(yes_coin, trigger, lo, hi), self._retention(no_coin, trigger, lo, hi)
            ys = self._median([x.spread_bps for x in self._books_in(yes_coin, trigger + lo, trigger + hi)])
            ns = self._median([x.spread_bps for x in self._books_in(no_coin, trigger + lo, trigger + hi)])
            complete = all(x is not None for x in (yr, nr, ys, ns))
            max_ret, min_spread = (max(yr, nr), min(ys, ns)) if complete else (None, None)
            eligible = bool(complete and max_ret is not None and min_spread is not None and max_ret < self.config.participation_retention_threshold and min_spread >= self.config.bilateral_spread_bps_threshold)
            windows[name] = {"eligible": eligible, "reason": "bilateral_participation_failure" if eligible else ("insufficient_bilateral_evidence" if not complete else "criteria_not_met"), "yes_participation_retention": str(yr) if yr is not None else None, "no_participation_retention": str(nr) if nr is not None else None, "bilateral_participation_max": str(max_ret) if max_ret is not None else None, "yes_spread_bps": str(ys) if ys is not None else None, "no_spread_bps": str(ns) if ns is not None else None, "bilateral_spread_min_bps": str(min_spread) if min_spread is not None else None, "participation_threshold": str(self.config.participation_retention_threshold), "spread_threshold_bps": str(self.config.bilateral_spread_bps_threshold)}
            if eligible: branches.append("B_" + name.upper() + "_PARTICIPATION_FAILURE")
        base["branch_b"] = {"eligible": any(v.get("eligible") for v in windows.values()), "windows": windows}
        base.update({"candidate": bool(branches), "candidate_branches": branches, "state": "STRUCTURAL_COLLAPSE_CANDIDATE_SHADOW" if branches else "STRUCTURAL_RISK_WATCH", "watch_age_sec": max(0, age)})
        return base
