"""Fail-closed market-data health for Outcome execution."""
from __future__ import annotations

import time
from dataclasses import dataclass

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec


@dataclass(frozen=True)
class OutcomeStreamHealthStatus:
    ready: bool
    reason: str


class OutcomeStreamHealth:
    def __init__(self, *, max_book_age_sec: float = 3.0) -> None:
        self.max_book_age_sec = max_book_age_sec
        self.market_id: int | None = None
        self.coins: tuple[str, str] = ("", "")
        self.connected = False
        self.resync_required = True
        self.book_received_at: dict[str, float] = {}

    def configure_market(self, market: OutcomeMarketSpec) -> None:
        if self.market_id != market.outcome_id:
            self.market_id, self.coins = market.outcome_id, (market.yes_coin, market.no_coin)
            self.book_received_at = {}
            self.resync_required = True

    def on_lifecycle(self, event: str) -> None:
        self.connected = event == "connected"
        if event in {"connected", "disconnected", "reconnect_exhausted"}:
            self.resync_required = True

    def mark_rest_resynced(self) -> None:
        if self.connected:
            self.resync_required = False

    def on_l2_book(self, coin: str, received_at: float | None = None) -> None:
        if coin in self.coins:
            self.book_received_at[coin] = received_at if received_at is not None else time.monotonic()

    def check(self, market: OutcomeMarketSpec, *, now: float | None = None) -> OutcomeStreamHealthStatus:
        self.configure_market(market)
        if not self.connected:
            return OutcomeStreamHealthStatus(False, "ws_disconnected")
        if self.resync_required:
            return OutcomeStreamHealthStatus(False, "ws_rest_resync_required")
        now = now if now is not None else time.monotonic()
        missing = [coin for coin in self.coins if coin not in self.book_received_at]
        if missing:
            return OutcomeStreamHealthStatus(False, "ws_book_missing")
        stale = [coin for coin in self.coins if now - self.book_received_at[coin] > self.max_book_age_sec]
        if stale:
            return OutcomeStreamHealthStatus(False, "ws_book_stale")
        return OutcomeStreamHealthStatus(True, "ws_fresh")
