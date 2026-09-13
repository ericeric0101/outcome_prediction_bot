"""Cross-validated user open-order snapshots for steady-state recovery."""
from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Any, Mapping


class OutcomeOpenOrdersStreamCache:
    """Trust WS orders only after an identical REST snapshot in this connection."""

    def __init__(self, wallet: str, *, rest_reconcile_interval_sec: float = 60.0) -> None:
        self.wallet = wallet.lower()
        self.rest_reconcile_interval_sec = max(1.0, float(rest_reconcile_interval_sec))
        self._lock = threading.Lock()
        self._connected = False
        self._generation = 0
        self._snapshot_generation: int | None = None
        self._verified_generation: int | None = None
        self._rest_verified_at = float("-inf")
        self._orders: list[dict[str, Any]] | None = None

    @staticmethod
    def _normalize_order(row: Mapping[str, Any]) -> dict[str, Any] | None:
        nested = row.get("order")
        source = nested if isinstance(nested, Mapping) else row
        try:
            oid = str(source["oid"])
            coin = str(source["coin"])
            side = str(source["side"])
            size = Decimal(str(source.get("sz", source.get("size", "0"))))
            price = Decimal(str(source.get("limitPx", source.get("px", "0"))))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None
        if not oid or not coin or side not in {"A", "B"} or size < 0 or price < 0:
            return None
        # Preserve the official payload's field types for existing recovery
        # consumers.  Decimal coercion above is validation/signature logic,
        # not a response-schema transformation.
        return dict(source)

    @classmethod
    def _normalized(cls, rows: object) -> list[dict[str, Any]] | None:
        if not isinstance(rows, list):
            return None
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                return None
            normalized = cls._normalize_order(row)
            if normalized is None:
                return None
            result.append(normalized)
        return result

    @staticmethod
    def _signature(rows: list[dict[str, Any]]) -> tuple[tuple[str, str, str, Decimal, Decimal], ...]:
        return tuple(sorted(
            (
                str(row["oid"]), str(row["coin"]), str(row["side"]),
                Decimal(str(row.get("sz", row.get("size", "0")))),
                Decimal(str(row.get("limitPx", row.get("px", "0")))),
            )
            for row in rows
        ))

    def on_lifecycle(self, event: str) -> None:
        if event not in {"connected", "disconnected"}:
            return
        with self._lock:
            if event == "connected":
                self._generation += 1
                self._connected = True
            else:
                self._connected = False
            self._snapshot_generation = None
            self._verified_generation = None
            self._rest_verified_at = float("-inf")
            self._orders = None

    def on_message(self, payload: Mapping[str, Any]) -> bool:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return False
        user = str(data.get("user") or "").lower()
        orders = self._normalized(data.get("orders"))
        if user != self.wallet or orders is None:
            return False
        with self._lock:
            if not self._connected:
                return False
            self._orders = orders
            self._snapshot_generation = self._generation
        return True

    def mark_rest_verified(self, user: str, orders: object, *, now: float | None = None) -> bool:
        normalized = self._normalized(orders)
        if user.lower() != self.wallet or normalized is None:
            return False
        observed = time.monotonic() if now is None else now
        with self._lock:
            matches = bool(
                self._connected and self._orders is not None
                and self._snapshot_generation == self._generation
                and self._signature(self._orders) == self._signature(normalized)
            )
            if matches:
                self._verified_generation = self._generation
                self._rest_verified_at = observed
            else:
                self._verified_generation = None
                self._rest_verified_at = float("-inf")
            return matches

    def trusted_orders(self, user: str, *, now: float | None = None) -> list[dict[str, Any]] | None:
        observed = time.monotonic() if now is None else now
        with self._lock:
            if not (
                user.lower() == self.wallet and self._connected and self._orders is not None
                and self._snapshot_generation == self._generation
                and self._verified_generation == self._generation
                and observed - self._rest_verified_at < self.rest_reconcile_interval_sec
            ):
                return None
            return [dict(row) for row in self._orders]

    def invalidate_for_mutation(self) -> None:
        """Require a new REST/WS equality fence after exchange mutation."""
        with self._lock:
            self._verified_generation = None
            self._rest_verified_at = float("-inf")
