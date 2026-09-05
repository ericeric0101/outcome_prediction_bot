"""Dedicated read-only scheduler for Outcome P2/P3 observations.

The live execution loop may wait on wallet reconciliation or the official SDK.
Research snapshots must not inherit that latency: this worker owns the capture
cadence and obtains its own public L2 books.  It deliberately has no execution
gateway and no methods that can submit or cancel an order.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_research_capture import OutcomeResearchCapture, OutcomeResearchCaptureResult
from monitoring.trade_journal_db import TradeJournalDB


class OutcomeResearchWorker:
    """Keep P2/P3 capture cadence independent from the live order loop."""

    def __init__(self, *, client: Any, capture: OutcomeResearchCapture, journal: TradeJournalDB) -> None:
        self.client = client
        self.capture = capture
        self.journal = journal
        self._market_lock = threading.Lock()
        self._market: Optional[OutcomeMarketSpec] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def set_market(self, market: OutcomeMarketSpec) -> None:
        with self._market_lock:
            self._market = market

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="outcome-research-capture")
        self._thread.start()

    def stop(self, *, timeout_sec: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout_sec))
        self._thread = None

    def _market_snapshot(self) -> Optional[OutcomeMarketSpec]:
        with self._market_lock:
            return self._market

    def _due(self, now_ms: int) -> bool:
        last = self.capture._last_capture_ms  # worker is the sole cadence owner
        return not last or now_ms - last >= self.capture.interval_ms

    def run_once(self) -> OutcomeResearchCaptureResult:
        market = self._market_snapshot()
        if market is None:
            return OutcomeResearchCaptureResult(False)
        now_ms = int(time.time() * 1000)
        if not self._due(now_ms):
            return OutcomeResearchCaptureResult(False)
        try:
            # ttl=0 prevents a previous execution-loop response from being
            # labelled as a fresh research observation.
            yes_book = self.client.get_l2_book_sync(market.yes_coin, ttl_sec=0.0)
            yes_received_at_ms = int(time.time() * 1000)
            no_book = self.client.get_l2_book_sync(market.no_coin, ttl_sec=0.0)
            no_received_at_ms = int(time.time() * 1000)
            complete_at_ms = int(time.time() * 1000)
            return self.capture.capture_if_due(
                market=market, yes_book=yes_book, no_book=no_book,
                yes_local_received_at_ms=yes_received_at_ms,
                no_local_received_at_ms=no_received_at_ms,
                capture_complete_at_ms=complete_at_ms,
            )
        except Exception as exc:
            self.journal.log_strategy_event(self.capture.run_id, "OUTCOME_RESEARCH_CAPTURE_WORKER_ERROR", {
                "venue": "hyperliquid_outcome", "read_only": True,
                "market_id": market.outcome_id, "period": market.period,
                "error_type": type(exc).__name__, "error": str(exc),
                "action": "capture_skipped_will_retry_next_interval",
            })
            return OutcomeResearchCaptureResult(False)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            # A short interruptible wait reacts quickly to a daily-market
            # rollover while still leaving the exact 5-second cadence to
            # OutcomeResearchCapture's monotonic journal timestamp gate.
            self._stop.wait(0.20)
