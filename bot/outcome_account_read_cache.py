"""Per-runtime-invocation cache for read-only Outcome account truth."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any


class OutcomeAccountReadCache:
    """Share identical account reads inside one decision, never across ticks.

    The cache is deliberately tiny and explicit: it only covers read-only
    account endpoints.  Every exchange mutation must call ``invalidate()``
    before any confirmation readback, preserving cancel/fill-race safety.
    """

    def __init__(self, account: Any) -> None:
        self._account = account
        self._cache: dict[tuple[str, tuple[object, ...]], Any] = {}
        self._timings: list[dict[str, object]] = []
        self._open_orders_stream: Any | None = None

    def set_open_orders_stream(self, stream: Any | None) -> None:
        self._open_orders_stream = stream
        self._cache.clear()

    def begin_tick(self) -> None:
        self._cache.clear()
        self._timings.clear()

    def invalidate(self) -> None:
        self._cache.clear()
        if self._open_orders_stream is not None:
            self._open_orders_stream.invalidate_for_mutation()

    def _read(self, method: str, *args: object) -> Any:
        key = (method, args)
        cache_hit = key in self._cache
        started_at = time.monotonic()
        if not cache_hit:
            self._cache[key] = getattr(self._account, method)(*args)
        self._timings.append({
            "method": method,
            "cache_hit": cache_hit,
            "elapsed_ms": round((time.monotonic() - started_at) * 1000, 3),
        })
        return self._cache[key]

    def timing_summary(self) -> dict[str, dict[str, int | float]]:
        """Return this tick's account-read cost without exposing account data."""
        summary: dict[str, dict[str, int | float]] = defaultdict(
            lambda: {
                "calls": 0, "cache_hits": 0, "ws_hits": 0,
                "network_calls": 0, "elapsed_ms": 0.0, "max_ms": 0.0,
            }
        )
        for timing in self._timings:
            method = str(timing["method"])
            row = summary[method]
            row["calls"] = int(row["calls"]) + 1
            if bool(timing["cache_hit"]):
                row["cache_hits"] = int(row["cache_hits"]) + 1
                if timing.get("source") == "cross_validated_ws":
                    row["ws_hits"] = int(row["ws_hits"]) + 1
                continue
            elapsed = float(timing["elapsed_ms"])
            row["network_calls"] = int(row["network_calls"]) + 1
            row["elapsed_ms"] = round(float(row["elapsed_ms"]) + elapsed, 3)
            row["max_ms"] = max(float(row["max_ms"]), elapsed)
        return dict(summary)

    def get_spot_clearinghouse_state_sync(self, user: str) -> dict[str, Any]:
        return self._read("get_spot_clearinghouse_state_sync", user)

    def get_open_orders_sync(self, user: str) -> list[dict[str, Any]]:
        key = ("get_open_orders_sync", (user,))
        started_at = time.monotonic()
        if key in self._cache:
            result = self._cache[key]
            source, cache_hit = "tick_cache", True
        else:
            result = self._open_orders_stream.trusted_orders(user) if self._open_orders_stream is not None else None
            if result is not None:
                source, cache_hit = "cross_validated_ws", True
            else:
                result = self._account.get_open_orders_sync(user)
                source, cache_hit = "rest", False
                if self._open_orders_stream is not None:
                    self._open_orders_stream.mark_rest_verified(user, result)
            self._cache[key] = result
        self._timings.append({
            "method": "get_open_orders_sync", "cache_hit": cache_hit, "source": source,
            "elapsed_ms": round((time.monotonic() - started_at) * 1000, 3),
        })
        return result

    def force_open_orders_reconciliation_sync(self, user: str) -> list[dict[str, Any]]:
        """Bypass WS/tick caches at periodic and mutation safety boundaries."""
        key = ("get_open_orders_sync", (user,))
        started_at = time.monotonic()
        result = self._account.get_open_orders_sync(user)
        if self._open_orders_stream is not None:
            self._open_orders_stream.mark_rest_verified(user, result)
        self._cache[key] = result
        self._timings.append({
            "method": "get_open_orders_sync", "cache_hit": False, "source": "forced_rest",
            "elapsed_ms": round((time.monotonic() - started_at) * 1000, 3),
        })
        return result

    def get_user_fills_sync(self, user: str) -> list[dict[str, Any]]:
        return self._read("get_user_fills_sync", user)

    def get_user_fees_sync(self, user: str) -> dict[str, Any]:
        return self._read("get_user_fees_sync", user)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._account, name)
