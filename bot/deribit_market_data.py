"""Public-only Deribit BTC research feed.

This module intentionally has no credential, account, order, or Outcome
execution dependency.  It reconstructs only a bounded BTC-PERPETUAL top book
and emits derived, time-stamped research evidence.  A sequence gap or stale
source clears the book: callers must treat the resulting feature vector as
unavailable rather than extrapolating it.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

import websockets

from monitoring.trade_journal_db import TradeJournalDB


DERIBIT_WS_URL = "wss://www.deribit.com/ws/api/v2"
DERIBIT_SOURCE = "deribit_public_ws"
DERIBIT_FEATURE_SCHEMA_VERSION = 1
_CORE_CHANNELS = (
    "deribit_price_index.btc_usd",
    "ticker.BTC-PERPETUAL.100ms",
    "book.BTC-PERPETUAL.100ms",
    "trades.BTC-PERPETUAL.100ms",
    "instrument.state.option.BTC",
)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _number(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


@dataclass(frozen=True)
class DeribitFeatureSnapshot:
    """One immutable, bounded as-of public-market observation."""

    source_timestamp_ms: int | None
    local_received_at_ms: int
    connection_generation: int
    valid: bool
    unavailable_reason: str | None
    index_price: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    top_bid_size: Decimal | None
    top_ask_size: Decimal | None
    mark_price: Decimal | None
    open_interest: Decimal | None
    funding_8h: Decimal | None
    trade_buy_notional_1s: Decimal
    trade_sell_notional_1s: Decimal
    book_change_id: int | None

    def payload(self) -> dict[str, Any]:
        bid, ask = self.best_bid, self.best_ask
        mid = (bid + ask) / Decimal("2") if bid is not None and ask is not None and bid <= ask else None
        spread_bps = ((ask - bid) / mid * Decimal("10000")) if mid and mid > 0 else None
        depth = (self.top_bid_size or Decimal("0")) + (self.top_ask_size or Decimal("0"))
        imbalance = (
            ((self.top_bid_size or Decimal("0")) - (self.top_ask_size or Decimal("0"))) / depth
            if depth > 0 else None
        )
        flow_total = self.trade_buy_notional_1s + self.trade_sell_notional_1s
        flow_imbalance = (
            (self.trade_buy_notional_1s - self.trade_sell_notional_1s) / flow_total
            if flow_total > 0 else None
        )
        return {
            "feature_schema_version": DERIBIT_FEATURE_SCHEMA_VERSION,
            "source": DERIBIT_SOURCE,
            "instrument": "BTC-PERPETUAL",
            "read_only": True,
            "live_authority": False,
            "source_timestamp_ms": self.source_timestamp_ms,
            "local_received_at_ms": self.local_received_at_ms,
            "connection_generation": self.connection_generation,
            "valid": self.valid,
            "unavailable_reason": self.unavailable_reason,
            "index_price": _number(self.index_price),
            "best_bid": _number(bid),
            "best_ask": _number(ask),
            "mid": _number(mid),
            "spread_bps": _number(spread_bps),
            "top_bid_size": _number(self.top_bid_size),
            "top_ask_size": _number(self.top_ask_size),
            "top_imbalance": _number(imbalance),
            "mark_price": _number(self.mark_price),
            "open_interest": _number(self.open_interest),
            "funding_8h": _number(self.funding_8h),
            "trade_buy_notional_1s": _number(self.trade_buy_notional_1s),
            "trade_sell_notional_1s": _number(self.trade_sell_notional_1s),
            "trade_flow_imbalance_1s": _number(flow_imbalance),
            "book_change_id": self.book_change_id,
            "recording_scope": "derived_perpetual_features_only_v1",
        }


class DeribitMarketDataWorker:
    """Own an isolated public Deribit WebSocket and bounded research journal.

    There is deliberately no callback into an Outcome strategy.  The sole
    write is read-only strategy telemetry; a broken or stale book clears its
    local state and is reported as unavailable.
    """

    SNAPSHOT_EVENT = "DERIBIT_FEATURE_SNAPSHOT"
    STATUS_EVENT = "DERIBIT_RESEARCH_STATUS"

    def __init__(
        self, *, journal: TradeJournalDB, run_id: str,
        url: str = DERIBIT_WS_URL, snapshot_interval_sec: float | None = None,
        max_age_sec: float | None = None,
    ) -> None:
        self.journal, self.run_id, self.url = journal, run_id, url
        self.snapshot_interval_sec = snapshot_interval_sec if snapshot_interval_sec is not None else float(
            os.getenv("DERIBIT_RESEARCH_SNAPSHOT_INTERVAL_SEC", "1")
        )
        self.max_age_sec = max_age_sec if max_age_sec is not None else float(
            os.getenv("DERIBIT_RESEARCH_MAX_AGE_SEC", "3")
        )
        if self.snapshot_interval_sec <= 0 or self.max_age_sec <= 0:
            raise ValueError("Deribit snapshot and freshness intervals must be positive")
        # A malformed snapshot may clear state from an in-flight message
        # handler.  Re-entrancy prevents the health path itself from becoming
        # a stuck data-collection thread.
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._book_change_id: int | None = None
        self._book_received_at_ms: int | None = None
        self._book_source_timestamp_ms: int | None = None
        self._index_price: Decimal | None = None
        self._ticker: dict[str, Decimal | None] = {}
        self._ticker_received_at_ms: int | None = None
        self._trades: deque[tuple[int, str, Decimal]] = deque()
        self._last_status: tuple[str, str] | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="deribit-research-ws")
        self._thread.start()

    def stop(self, *, timeout_sec: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            try:
                self._thread.join(timeout=max(0.0, timeout_sec))
            except KeyboardInterrupt:
                pass
        self._thread = None

    def _log_status(self, state: str, detail: str) -> None:
        status = (state, detail)
        if status == self._last_status:
            return
        self._last_status = status
        self.journal.log_strategy_event(self.run_id, self.STATUS_EVENT, {
            "source": DERIBIT_SOURCE, "read_only": True, "live_authority": False,
            "state": state, "detail": detail, "connection_generation": self._generation,
            "local_received_at_ms": int(time.time() * 1000),
            "action": "features_unavailable_no_execution_effect" if state != "ready" else "derived_capture_active",
        })

    def _clear_book(self, reason: str) -> None:
        with self._lock:
            self._bids.clear()
            self._asks.clear()
            self._book_change_id = None
            self._book_received_at_ms = None
            self._book_source_timestamp_ms = None
        self._log_status("unavailable", reason)

    @staticmethod
    def _level(level: Any) -> tuple[str, Decimal, Decimal] | None:
        if not isinstance(level, (list, tuple)):
            return None
        if len(level) == 2:
            action, price, size = "new", level[0], level[1]
        elif len(level) >= 3:
            action, price, size = str(level[0]).lower(), level[1], level[2]
        else:
            return None
        parsed_price, parsed_size = _decimal(price), _decimal(size)
        if parsed_price is None or parsed_size is None or parsed_price <= 0 or parsed_size < 0:
            return None
        return action, parsed_price, parsed_size

    @staticmethod
    def _apply_levels(target: dict[Decimal, Decimal], levels: Any) -> bool:
        if not isinstance(levels, list):
            return False
        for item in levels:
            parsed = DeribitMarketDataWorker._level(item)
            if parsed is None:
                return False
            _action, price, size = parsed
            if size == 0:
                target.pop(price, None)
            else:
                target[price] = size
        return True

    def _on_book(self, payload: Mapping[str, Any], *, received_at_ms: int | None = None) -> None:
        received = received_at_ms or int(time.time() * 1000)
        kind = str(payload.get("type", "")).lower()
        change_id = payload.get("change_id")
        previous = payload.get("prev_change_id")
        try:
            change_id = int(change_id)
        except (TypeError, ValueError):
            self._clear_book("book_missing_change_id")
            return
        try:
            previous = int(previous) if previous is not None else None
        except (TypeError, ValueError):
            self._clear_book("book_invalid_prev_change_id")
            return
        with self._lock:
            if kind == "snapshot":
                bids: dict[Decimal, Decimal] = {}
                asks: dict[Decimal, Decimal] = {}
                if not self._apply_levels(bids, payload.get("bids")) or not self._apply_levels(asks, payload.get("asks")):
                    self._clear_book("book_malformed_snapshot")
                    return
                self._bids, self._asks = bids, asks
            elif kind == "change":
                if self._book_change_id is None or previous != self._book_change_id:
                    # Do not infer a book across a missed update.
                    self._bids.clear()
                    self._asks.clear()
                    self._book_change_id = None
                    self._book_received_at_ms = None
                    self._book_source_timestamp_ms = None
                    gap = True
                else:
                    gap = not self._apply_levels(self._bids, payload.get("bids")) or not self._apply_levels(self._asks, payload.get("asks"))
                if gap:
                    self._log_status("unavailable", "book_sequence_gap_or_malformed_change")
                    return
            else:
                self._clear_book("book_unknown_message_type")
                return
            self._book_change_id = change_id
            self._book_received_at_ms = received
            timestamp = payload.get("timestamp")
            self._book_source_timestamp_ms = int(timestamp) if isinstance(timestamp, int) else None
        self._log_status("ready", "book_snapshot_or_sequence_continuous")

    def _on_ticker(self, payload: Mapping[str, Any], *, received_at_ms: int | None = None) -> None:
        values = {
            "best_bid": _decimal(payload.get("best_bid_price")),
            "best_ask": _decimal(payload.get("best_ask_price")),
            "mark_price": _decimal(payload.get("mark_price")),
            "index_price": _decimal(payload.get("index_price")),
            "open_interest": _decimal(payload.get("open_interest")),
            "funding_8h": _decimal(payload.get("funding_8h")),
        }
        with self._lock:
            self._ticker = values
            self._ticker_received_at_ms = received_at_ms or int(time.time() * 1000)

    def _on_index(self, payload: Mapping[str, Any]) -> None:
        value = _decimal(payload.get("price"))
        if value is not None:
            with self._lock:
                self._index_price = value

    def _on_trades(self, payload: Any, *, received_at_ms: int | None = None) -> None:
        rows = payload if isinstance(payload, list) else [payload]
        received = received_at_ms or int(time.time() * 1000)
        with self._lock:
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                direction = str(row.get("direction", "")).lower()
                price, amount = _decimal(row.get("price")), _decimal(row.get("amount"))
                if direction not in {"buy", "sell"} or price is None or amount is None or price <= 0 or amount <= 0:
                    continue
                timestamp = row.get("timestamp")
                at = int(timestamp) if isinstance(timestamp, int) else received
                self._trades.append((at, direction, price * amount))
            cutoff = received - 1_000
            while self._trades and self._trades[0][0] < cutoff:
                self._trades.popleft()

    def on_message(self, message: Mapping[str, Any], *, received_at_ms: int | None = None) -> None:
        """Route a decoded Deribit JSON-RPC subscription notification."""
        params = message.get("params")
        if message.get("method") != "subscription" or not isinstance(params, Mapping):
            return
        channel, data = str(params.get("channel", "")), params.get("data")
        if channel == "book.BTC-PERPETUAL.100ms" and isinstance(data, Mapping):
            self._on_book(data, received_at_ms=received_at_ms)
        elif channel == "ticker.BTC-PERPETUAL.100ms" and isinstance(data, Mapping):
            self._on_ticker(data, received_at_ms=received_at_ms)
        elif channel == "deribit_price_index.btc_usd" and isinstance(data, Mapping):
            self._on_index(data)
        elif channel == "trades.BTC-PERPETUAL.100ms":
            self._on_trades(data, received_at_ms=received_at_ms)

    def snapshot(self, *, now_ms: int | None = None) -> DeribitFeatureSnapshot:
        now = now_ms or int(time.time() * 1000)
        with self._lock:
            book_age = (now - self._book_received_at_ms) if self._book_received_at_ms is not None else None
            valid = bool(self._book_change_id is not None and book_age is not None and book_age <= int(self.max_age_sec * 1000))
            reason = None if valid else ("book_not_ready" if book_age is None else "book_stale")
            bid_item = max(self._bids.items()) if self._bids else (None, None)
            ask_item = min(self._asks.items()) if self._asks else (None, None)
            buy = sum((notional for timestamp, side, notional in self._trades if timestamp >= now - 1_000 and side == "buy"), Decimal("0"))
            sell = sum((notional for timestamp, side, notional in self._trades if timestamp >= now - 1_000 and side == "sell"), Decimal("0"))
            index = self._index_price or self._ticker.get("index_price")
            return DeribitFeatureSnapshot(
                source_timestamp_ms=self._book_source_timestamp_ms,
                local_received_at_ms=now,
                connection_generation=self._generation,
                valid=valid,
                unavailable_reason=reason,
                index_price=index,
                best_bid=bid_item[0] if valid else None,
                best_ask=ask_item[0] if valid else None,
                top_bid_size=bid_item[1] if valid else None,
                top_ask_size=ask_item[1] if valid else None,
                mark_price=self._ticker.get("mark_price") if valid else None,
                open_interest=self._ticker.get("open_interest") if valid else None,
                funding_8h=self._ticker.get("funding_8h") if valid else None,
                trade_buy_notional_1s=buy,
                trade_sell_notional_1s=sell,
                book_change_id=self._book_change_id if valid else None,
            )

    def record_snapshot(self, *, now_ms: int | None = None) -> DeribitFeatureSnapshot:
        snapshot = self.snapshot(now_ms=now_ms)
        self.journal.log_strategy_event(self.run_id, self.SNAPSHOT_EVENT, snapshot.payload())
        return snapshot

    async def _serve(self) -> None:
        while not self._stop.is_set():
            self._generation += 1
            self._clear_book("connecting_or_reconnecting")
            try:
                async with websockets.connect(self.url, open_timeout=10, close_timeout=3, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": self._generation,
                        "method": "public/subscribe", "params": {"channels": list(_CORE_CHANNELS)},
                    }))
                    self._log_status("subscribing", "public_channels_requested")
                    next_snapshot = time.monotonic()
                    while not self._stop.is_set():
                        timeout = max(0.05, next_snapshot - time.monotonic())
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                            decoded = json.loads(raw)
                            if isinstance(decoded, Mapping):
                                self.on_message(decoded)
                        except asyncio.TimeoutError:
                            self.record_snapshot()
                            next_snapshot += self.snapshot_interval_sec
                        except (json.JSONDecodeError, TypeError):
                            self._log_status("unavailable", "malformed_json_message")
                    return
            except Exception as exc:
                self._clear_book(f"connection_error:{type(exc).__name__}")
                self._stop.wait(1.0)

    def _run(self) -> None:
        asyncio.run(self._serve())
