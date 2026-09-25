"""Optional, read-only BTC spot feature capture on the existing journal DB."""
from __future__ import annotations

import os
import json
import sqlite3
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from bot.hyperliquid_spot import SpotMarket, resolve_btc_usdc_spot
from bot.outcome_settlement_probability import estimate_settlement_probability
from monitoring.trade_journal_db import TradeJournalDB


def _top_levels(book: Mapping[str, Any], side: int, count: int = 3) -> list[dict[str, str]]:
    levels = book.get("levels")
    if not isinstance(levels, list) or len(levels) <= side or not isinstance(levels[side], list):
        return []
    result = []
    for row in levels[side][:count]:
        if isinstance(row, Mapping) and row.get("px") is not None and row.get("sz") is not None:
            result.append({"px": str(row["px"]), "sz": str(row["sz"])})
    return result


def _perp_btc_context(client: Any) -> dict[str, Any]:
    try:
        response = client.get_meta_and_asset_ctxs_sync()
        if not isinstance(response, list) or len(response) < 2 or not isinstance(response[0], Mapping):
            return {"status": "invalid_response"}
        universe, contexts = response[0].get("universe", []), response[1]
        if not isinstance(universe, list) or not isinstance(contexts, list):
            return {"status": "invalid_response"}
        for index, row in enumerate(universe):
            if isinstance(row, Mapping) and str(row.get("name", "")).upper() == "BTC" and index < len(contexts):
                context = contexts[index]
                if not isinstance(context, Mapping):
                    break
                fields = ("markPx", "oraclePx", "funding", "openInterest", "dayNtlVlm")
                return {"status": "observed", **{key: context.get(key) for key in fields}}
        return {"status": "btc_perp_context_missing"}
    except Exception as exc:
        return {"status": "unavailable", "error_type": type(exc).__name__}


class BTCSpotShadowCapture:
    def __init__(self, *, client: Any, journal: TradeJournalDB, run_id: str | None = None,
                 interval_sec: float = 5.0, context_interval_sec: float = 15.0) -> None:
        if interval_sec < 1 or context_interval_sec < interval_sec:
            raise ValueError("invalid BTC spot shadow intervals")
        self.client, self.journal = client, journal
        self.run_id = run_id or f"btc-spot-shadow-{uuid.uuid4().hex[:10]}"
        self.interval_ms = int(interval_sec * 1000)
        self.context_interval_ms = int(context_interval_sec * 1000)
        self.market: SpotMarket | None = None
        self._last_capture_ms = 0
        self._last_context_ms = 0
        self._context_observed_ms: int | None = None
        self._perp_mid: str | None = None
        self._perp_mid_observed_ms: int | None = None
        self._last_error_event_ms = 0
        self._context: dict[str, Any] = {"status": "pending"}
        self._history_lock = threading.Lock()
        self._spot_history: list[tuple[int, float]] = self._load_recent_spot_history()

    def _load_recent_spot_history(self) -> list[tuple[int, float]]:
        """Seed warmup from already-persisted spot shadow rows, read-only."""
        raw_path = getattr(self.journal, "db_path", None)
        if not raw_path:
            return []
        try:
            path = Path(raw_path).expanduser().resolve()
            if not path.exists():
                return []
            now_ms = int(time.time() * 1000)
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1) as conn:
                conn.execute("PRAGMA busy_timeout=100")
                rows = conn.execute(
                    "SELECT payload_json FROM strategy_events "
                    "WHERE event_type='BTC_SPOT_SHADOW_SNAPSHOT' ORDER BY id DESC LIMIT 1000"
                ).fetchall()
            points: list[tuple[int, float]] = []
            for (raw,) in reversed(rows):
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or payload.get("read_only") is not True:
                        continue
                    ts = int(payload.get("server_timestamp_ms") or payload.get("local_received_at_ms")
                             or payload["snapshot_timestamp_ms"])
                    mid = float(payload["mid"])
                    if now_ms - 3_660_000 <= ts <= now_ms and mid > 0:
                        points.append((ts, mid))
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    continue
            return points
        except (OSError, sqlite3.Error, TypeError, ValueError):
            # Startup history is an optimization for warmup only.  A busy or
            # unreadable journal simply means the bounded observer warms up
            # from its next live samples; it cannot block bot startup.
            return []

    def run_once(self, *, now_ms: int | None = None) -> bool:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        if self._last_capture_ms and now - self._last_capture_ms < self.interval_ms:
            return False
        if self.market is None:
            meta_reader = getattr(self.client, "get_spot_meta_sync", None) or self.client.spot_meta
            self.market = resolve_btc_usdc_spot(meta_reader())
        if hasattr(self.client, "get_l2_book_sync"):
            book = self.client.get_l2_book_sync(self.market.coin, ttl_sec=0.0)
        else:
            book = self.client.l2_book(self.market.coin)
        received_ms = int(time.time() * 1000)
        bids, asks = _top_levels(book, 0), _top_levels(book, 1)
        bid = Decimal(bids[0]["px"]) if bids else None
        ask = Decimal(asks[0]["px"]) if asks else None
        if not bids or not asks or bid is None or ask is None or bid <= 0 or ask < bid:
            raise ValueError("BTC spot L2 is incomplete or crossed")
        if now - self._last_context_ms >= self.context_interval_ms:
            self._context = _perp_btc_context(self.client)
            mids_reader = getattr(self.client, "get_all_mids_sync", None)
            mids = mids_reader(ttl_sec=0.0) if mids_reader else self.client.post_info({"type": "allMids"})
            self._perp_mid = mids.get("BTC")
            self._perp_mid_observed_ms = int(time.time() * 1000)
            self._last_context_ms = now
            self._context_observed_ms = received_ms
        event_id = self.journal.log_best_effort_strategy_event(self.run_id, "BTC_SPOT_SHADOW_SNAPSHOT", {
            "schema_version": 1, "venue": "hyperliquid_spot", "read_only": True,
            "live_authority": False, "execution_enabled": False,
            "snapshot_timestamp_ms": now, "local_received_at_ms": received_ms,
            "server_timestamp_ms": book.get("time"), "market_pair": self.market.pair,
            "coin": self.market.coin, "spot_index": self.market.index,
            "spot_asset_id": self.market.asset_id, "base_token": self.market.base_token,
            "quote_token": self.market.quote_token, "bid": str(bid), "ask": str(ask),
            "mid": str((bid + ask) / Decimal("2")), "spread": str(ask - bid),
            "bids_top3": bids, "asks_top3": asks, "btc_perp_mid": self._perp_mid,
            "btc_perp_mid_observed_at_ms": self._perp_mid_observed_ms,
            "perp_context": self._context, "perp_context_observed_at_ms": self._context_observed_ms,
        }, timeout_sec=0.05)
        if event_id is None:
            raise RuntimeError("BTC spot shadow event was not durably accepted")
        observation_ms = int(book.get("time") or received_ms)
        with self._history_lock:
            self._spot_history.append((observation_ms, float((bid + ask) / Decimal("2"))))
            cutoff_ms = observation_ms - 3_660_000
            self._spot_history = [point for point in self._spot_history if point[0] >= cutoff_ms]
        self._last_capture_ms = now
        return True

    def settlement_probability_shadow(
        self, *, strike: Decimal | float, time_left_sec: float, as_of_ms: int,
    ) -> dict[str, Any]:
        """Return a read-only settlement probability from captured spot L2 mids."""
        with self._history_lock:
            points = tuple(self._spot_history)
        latest = max((point for point in points if point[0] <= as_of_ms), default=None, key=lambda item: item[0])
        if latest is None:
            return {
                "status": "unavailable", "reason": "btc_spot_shadow_history_not_started",
                "source": "hyperliquid_spot_l2_mid", "live_authority": False,
                "execution_enabled": False,
            }
        result = estimate_settlement_probability(
            spot_price=latest[1], strike=float(strike), time_left_sec=float(time_left_sec),
            as_of_ms=as_of_ms, price_points=points, max_spot_age_ms=15_000,
        )
        return {
            **result, "source": "hyperliquid_spot_l2_mid",
            "volatility_source": "captured_hyperliquid_spot_l2_mid",
            "decision_timestamp_ms": as_of_ms,
            "live_authority": False, "execution_enabled": False,
        }


class BTCSpotShadowWorker:
    def __init__(self, *, capture: BTCSpotShadowCapture, journal: TradeJournalDB) -> None:
        self.capture, self.journal = capture, journal
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="btc-spot-shadow")
        self._thread.start()

    def stop(self, *, timeout_sec: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout_sec))
        self._thread = None
        client = self.capture.client
        sync_client = getattr(client, "_sync_client", None)
        if sync_client is not None and not sync_client.is_closed:
            sync_client.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.capture.run_once()
            except Exception as exc:
                now_ms = int(time.time() * 1000)
                if now_ms - self.capture._last_error_event_ms >= 60_000:
                    self.journal.log_best_effort_strategy_event(self.capture.run_id, "BTC_SPOT_SHADOW_STATUS", {
                        "venue": "hyperliquid_spot", "read_only": True,
                        "status": "capture_error", "error_type": type(exc).__name__,
                        "timestamp_ms": now_ms,
                    }, timeout_sec=0.05)
                    self.capture._last_error_event_ms = now_ms
            remaining = max(0.05, self.capture.interval_ms / 1000 - (time.monotonic() - started))
            self._stop.wait(remaining)
