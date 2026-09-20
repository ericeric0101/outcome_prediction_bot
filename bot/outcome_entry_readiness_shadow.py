"""Read-only forward shadow for short-horizon Outcome entry readiness.

This module owns only bounded public WebSocket observations.  It deliberately
does not import an account, gateway, controller, or mutation primitive.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable


@dataclass(frozen=True)
class EntryReadinessConfig:
    """Frozen V1/V2 forward-validation thresholds; not live entry controls."""

    bilateral_min_depth: Decimal = Decimal("1160")
    btc_favorable_velocity_30s_bps: Decimal = Decimal("1.85")
    participation_accel_30v120: Decimal = Decimal("1.00")
    cross_side_confirmation_30s_bps: Decimal = Decimal("300")
    votes_required: int = 3
    history_sec: float = 240.0
    max_samples_per_series: int = 20_000
    max_snapshot_lag_sec: float = 6.0


@dataclass(frozen=True)
class _Book:
    ts: float
    bid: Decimal
    ask: Decimal
    depth: Decimal


class OutcomeEntryReadinessShadow:
    """Thread-safe, best-effort observer with strictly zero execution authority."""

    def __init__(self, config: EntryReadinessConfig | None = None) -> None:
        self.config = config or EntryReadinessConfig()
        self._lock = threading.RLock()
        self._books: dict[str, deque[_Book]] = {}
        # Participation is deliberately market-level.  The same public trade
        # may appear in both YES/NO feeds, so side-local counts would turn a
        # mirrored record into false acceleration evidence.
        self._market_trades: deque[tuple[float, str]] = deque()
        self._market_trade_ids: set[str] = set()
        self._btc: deque[tuple[float, Decimal]] = deque()

    @staticmethod
    def _dec(value: Any) -> Decimal | None:
        try:
            result = Decimal(str(value))
        except (ArithmeticError, TypeError, ValueError):
            return None
        return result if result.is_finite() else None

    @staticmethod
    def _trade_id(row: dict[str, Any]) -> str:
        for key in ("tid", "tradeId", "trade_id", "hash", "txHash", "tx_hash"):
            if row.get(key) not in (None, ""):
                return f"{key}:{row[key]}"
        # A synthetic identity is last resort only; it is still market-wide
        # so mirrored rows cannot double-count by their Outcome side.
        return "fallback:" + "|".join(str(row.get(key, "")) for key in (
            "time", "timestamp", "px", "price", "sz", "size",
        ))

    @classmethod
    def _parse_book(cls, payload: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal] | None:
        levels = payload.get("levels")
        if not isinstance(levels, list) or len(levels) < 2:
            return None
        bids, asks = levels[0], levels[1]
        if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
            return None
        first_bid, first_ask = bids[0], asks[0]
        if not isinstance(first_bid, dict) or not isinstance(first_ask, dict):
            return None
        bid = cls._dec(first_bid.get("px", first_bid.get("price")))
        ask = cls._dec(first_ask.get("px", first_ask.get("price")))
        if bid is None or ask is None or not (Decimal("0") < bid < ask < Decimal("1")):
            return None
        depth = sum((
            cls._dec(row.get("sz", row.get("size"))) or Decimal("0")
            for row in bids[:3] if isinstance(row, dict)
        ), Decimal("0"))
        return bid, ask, depth

    def _trim_locked(self, now: float) -> None:
        cutoff = now - self.config.history_sec
        for rows in self._books.values():
            while rows and rows[0].ts < cutoff:
                rows.popleft()
            while len(rows) > self.config.max_samples_per_series:
                rows.popleft()
        while self._market_trades and self._market_trades[0][0] < cutoff:
            self._market_trade_ids.discard(self._market_trades.popleft()[1])
        while len(self._market_trades) > self.config.max_samples_per_series:
            self._market_trade_ids.discard(self._market_trades.popleft()[1])
        while self._btc and self._btc[0][0] < cutoff:
            self._btc.popleft()

    def observe_l2(self, *, coin: str, timestamp: float, payload: dict[str, Any]) -> None:
        parsed = self._parse_book(payload)
        if parsed is None:
            return
        bid, ask, depth = parsed
        with self._lock:
            self._books.setdefault(coin, deque()).append(_Book(float(timestamp), bid, ask, depth))
            self._trim_locked(float(timestamp))

    def observe_trades(self, *, items: Iterable[dict[str, Any]], received_at: float) -> None:
        with self._lock:
            for row in items:
                if not isinstance(row, dict):
                    continue
                raw_timestamp = row.get("time", row.get("timestamp"))
                try:
                    timestamp = float(raw_timestamp)
                    timestamp = timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
                except (TypeError, ValueError):
                    timestamp = float(received_at)
                trade_id = self._trade_id(row)
                if trade_id in self._market_trade_ids:
                    continue
                self._market_trade_ids.add(trade_id)
                self._market_trades.append((timestamp, trade_id))
            self._trim_locked(float(received_at))

    def observe_btc_mid(self, *, timestamp: float, price: Any) -> None:
        mid = self._dec(price)
        if mid is None or mid <= 0:
            return
        with self._lock:
            self._btc.append((float(timestamp), mid))
            self._trim_locked(float(timestamp))

    @staticmethod
    def _latest_at(rows: Iterable[Any], target: float, timestamp_of: Any, max_lag_sec: float) -> Any | None:
        for row in reversed(tuple(rows)):
            timestamp = timestamp_of(row)
            if timestamp <= target:
                return row if target - timestamp <= max_lag_sec else None
        return None

    def _book_at(self, coin: str, target: float) -> _Book | None:
        return self._latest_at(self._books.get(coin, ()), target, lambda row: row.ts, self.config.max_snapshot_lag_sec)

    def _btc_at(self, target: float) -> tuple[float, Decimal] | None:
        return self._latest_at(self._btc, target, lambda row: row[0], self.config.max_snapshot_lag_sec)

    def _market_trade_rate(self, lo: float, hi: float) -> Decimal | None:
        if hi <= lo:
            return None
        count = sum(1 for timestamp, _ in self._market_trades if lo < timestamp <= hi)
        return Decimal(count) / Decimal(str((hi - lo) / 60))

    def _snapshot(self, *, held_coin: str, opposite_coin: str, side_index: int, timestamp: float) -> dict[str, Any]:
        held, opposite = self._book_at(held_coin, timestamp), self._book_at(opposite_coin, timestamp)
        held_30, opposite_30 = self._book_at(held_coin, timestamp - 30), self._book_at(opposite_coin, timestamp - 30)
        btc, btc_30 = self._btc_at(timestamp), self._btc_at(timestamp - 30)
        features: dict[str, Decimal | None] = {
            "bilateral_min_depth": min(held.depth, opposite.depth) if held is not None and opposite is not None else None,
            "btc_favorable_velocity_30s_bps": None,
            "participation_accel_30v120": None,
            "cross_side_confirmation_30s_bps": None,
        }
        if btc is not None and btc_30 is not None and btc_30[1] > 0:
            raw_bps = (btc[1] / btc_30[1] - Decimal("1")) * Decimal("10000")
            features["btc_favorable_velocity_30s_bps"] = raw_bps if side_index == 0 else -raw_bps
        if held is not None and held_30 is not None and opposite is not None and opposite_30 is not None:
            held_bps = (held.bid / held_30.bid - Decimal("1")) * Decimal("10000")
            opposite_bps = (opposite.bid / opposite_30.bid - Decimal("1")) * Decimal("10000")
            features["cross_side_confirmation_30s_bps"] = held_bps - opposite_bps
        recent = self._market_trade_rate(timestamp - 30, timestamp)
        baseline = self._market_trade_rate(timestamp - 150, timestamp - 30)
        if recent is not None and baseline is not None and baseline > 0:
            features["participation_accel_30v120"] = recent / baseline
        complete = all(value is not None for value in features.values())
        votes = 0
        if complete:
            votes = sum((
                int(features["bilateral_min_depth"] >= self.config.bilateral_min_depth),  # type: ignore[operator]
                int(features["btc_favorable_velocity_30s_bps"] >= self.config.btc_favorable_velocity_30s_bps),  # type: ignore[operator]
                int(features["participation_accel_30v120"] >= self.config.participation_accel_30v120),  # type: ignore[operator]
                int(features["cross_side_confirmation_30s_bps"] >= self.config.cross_side_confirmation_30s_bps),  # type: ignore[operator]
            ))
        return {
            "timestamp": timestamp, "complete": complete, "votes": votes,
            "ready": bool(complete and votes >= self.config.votes_required),
            "features": {key: str(value) if value is not None else None for key, value in features.items()},
        }

    def evaluate(self, *, outcome_id: int, period: str, side_index: int, yes_coin: str, no_coin: str,
                 now: float, decision_observed_at_ms: int | None = None) -> dict[str, Any]:
        """Evaluate a candidate without waiting for the WS callback lock."""
        held_coin, opposite_coin = (yes_coin, no_coin) if side_index == 0 else (no_coin, yes_coin)
        if not self._lock.acquire(blocking=False):
            observations = []
            current = {"timestamp": now, "complete": False, "votes": 0, "ready": False, "features": {}}
            reason = "observer_busy"
        else:
            try:
                observations = [self._snapshot(held_coin=held_coin, opposite_coin=opposite_coin,
                                               side_index=side_index, timestamp=now - offset)
                                for offset in (30, 20, 10, 0)]
                current = observations[-1]
                reason = None
            finally:
                self._lock.release()
        complete_count = sum(int(row["complete"]) for row in observations)
        ready_count = sum(int(row["ready"]) for row in observations)
        majority = complete_count == 4 and ready_count >= 3
        state = ("ENTRY_READINESS_PERSISTENT_SHADOW" if majority else
                 "ENTRY_READINESS_READY_SHADOW" if current["ready"] else
                 "ENTRY_READINESS_NOT_READY_SHADOW")
        return {
            "schema_version": 1, "venue": "hyperliquid_outcome", "read_only": True,
            "live_authority": False, "execution_submitted": False, "outcome_id": outcome_id,
            "period": period, "side_index": side_index, "held_coin": held_coin,
            "opposite_coin": opposite_coin, "timestamp": now,
            "decision_observed_at_ms": decision_observed_at_ms, "reason": reason,
            "thresholds": {
                "bilateral_min_depth": str(self.config.bilateral_min_depth),
                "btc_favorable_velocity_30s_bps": str(self.config.btc_favorable_velocity_30s_bps),
                "participation_accel_30v120": str(self.config.participation_accel_30v120),
                "cross_side_confirmation_30s_bps": str(self.config.cross_side_confirmation_30s_bps),
                "votes_required": self.config.votes_required,
                "threshold_source": "V2 LOMO fold-median; frozen for forward validation",
            },
            "current": current,
            "pre30_persistence": {"observation_offsets_sec": [-30, -20, -10, 0],
                                   "complete_observations": complete_count,
                                   "ready_observations": ready_count, "majority_ready": majority,
                                   "all_ready": complete_count == 4 and ready_count == 4,
                                   "observations": observations},
            "candidate": bool(current["ready"]), "persistent_candidate": majority, "state": state,
            "promotion_boundary": {"shadow_only": True, "may_submit_order": False,
                                   "may_cancel_order": False, "may_replace_order": False,
                                   "may_block_entry": False, "may_change_stale_cancel_age": False},
        }
