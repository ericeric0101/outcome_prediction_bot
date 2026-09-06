"""Per-runtime-invocation cache for read-only Outcome account truth."""
from __future__ import annotations

from typing import Any, Callable


class OutcomeAccountReadCache:
    """Share identical account reads inside one decision, never across ticks.

    The cache is deliberately tiny and explicit: it only covers read-only
    account endpoints.  Every exchange mutation must call ``invalidate()``
    before any confirmation readback, preserving cancel/fill-race safety.
    """

    def __init__(self, account: Any) -> None:
        self._account = account
        self._cache: dict[tuple[str, tuple[object, ...]], Any] = {}

    def begin_tick(self) -> None:
        self._cache.clear()

    def invalidate(self) -> None:
        self._cache.clear()

    def _read(self, method: str, *args: object) -> Any:
        key = (method, args)
        if key not in self._cache:
            self._cache[key] = getattr(self._account, method)(*args)
        return self._cache[key]

    def get_spot_clearinghouse_state_sync(self, user: str) -> dict[str, Any]:
        return self._read("get_spot_clearinghouse_state_sync", user)

    def get_open_orders_sync(self, user: str) -> list[dict[str, Any]]:
        return self._read("get_open_orders_sync", user)

    def get_user_fills_sync(self, user: str) -> list[dict[str, Any]]:
        return self._read("get_user_fills_sync", user)

    def get_user_fees_sync(self, user: str) -> dict[str, Any]:
        return self._read("get_user_fees_sync", user)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._account, name)
