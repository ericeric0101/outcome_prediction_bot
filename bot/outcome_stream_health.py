"""Fail-closed market-data health for Outcome execution."""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec


@dataclass(frozen=True)
class OutcomeStreamHealthStatus:
    ready: bool
    reason: str


class OutcomeStreamHealth:
    # Outcome's public L2 stream commonly publishes a paired update roughly
    # every 5--6 seconds in quiet books.  Three seconds therefore rejected a
    # healthy subscribed stream between normal venue updates.  Fifteen seconds
    # permits two missed normal intervals while still failing closed promptly
    # on a stopped feed.
    def __init__(self, *, max_book_age_sec: float = 15.0) -> None:
        self.max_book_age_sec = max_book_age_sec
        self.market_id: int | None = None
        self.coins: tuple[str, str] = ("", "")
        self.connected = False
        self.resync_required = True
        self.book_received_at: dict[str, float] = {}
        # This is deliberately only a cheap *keep* hint.  Execution still
        # obtains REST L2 after a plan authorizes cancel/rebook/IOC.
        self.book_top: dict[str, tuple[Decimal, Decimal, float]] = {}

    def configure_market(self, market: OutcomeMarketSpec) -> None:
        if self.market_id != market.outcome_id:
            self.market_id, self.coins = market.outcome_id, (market.yes_coin, market.no_coin)
            self.book_received_at = {}
            self.book_top = {}
            self.resync_required = True

    def on_lifecycle(self, event: str) -> None:
        self.connected = event == "connected"
        if event in {"connected", "disconnected", "reconnect_exhausted"}:
            self.resync_required = True

    def mark_rest_resynced(self) -> None:
        if self.connected:
            self.resync_required = False

    def on_l2_book(self, coin: str, received_at: float | None = None,
                   payload: dict[str, Any] | None = None) -> None:
        if coin in self.coins:
            observed_at = received_at if received_at is not None else time.monotonic()
            self.book_received_at[coin] = observed_at
            if payload is not None:
                try:
                    levels = payload["levels"]
                    bid = Decimal(str(levels[0][0]["px"]))
                    ask = Decimal(str(levels[1][0]["px"]))
                    if Decimal("0") < bid < ask < Decimal("1"):
                        self.book_top[coin] = (bid, ask, observed_at)
                except (KeyError, IndexError, TypeError, ValueError, ArithmeticError):
                    # Freshness may still be useful even when a malformed
                    # payload cannot provide a BBO keep hint.
                    self.book_top.pop(coin, None)

    def fresh_bbo(self, market: OutcomeMarketSpec, coin: str) -> tuple[Decimal, Decimal] | None:
        """Return a healthy WS BBO only for a no-mutation keep decision."""
        if not self.check(market).ready:
            return None
        row = self.book_top.get(coin)
        if row is None:
            return None
        bid, ask, observed_at = row
        if time.monotonic() - observed_at > self.max_book_age_sec:
            return None
        return bid, ask

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
