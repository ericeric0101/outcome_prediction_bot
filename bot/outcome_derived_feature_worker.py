"""Bounded, read-only-derived feature scheduling for Outcome research.

Raw P2, Binance OI, and Deribit observations are captured by their own
workers.  The X3/D2 joins used to require a manual script invocation, which
silently left current raw data unusable for walk-forward research.  This
worker closes that operational gap without joining the execution lane: it has
no venue client, no order methods, uses small SQLite transactions, and gives
up quickly when the journal is contended.
"""
from __future__ import annotations

from dataclasses import asdict
import threading
import time
from typing import Callable

from bot.outcome_deribit_features import OutcomeDeribitFeaturePipeline
from bot.outcome_oi_features import OutcomeOiFeaturePipeline
from monitoring.trade_journal_db import TradeJournalDB


class OutcomeDerivedFeatureWorker:
    """Periodically materialize X3/D2 rows outside live order execution."""

    EVENT = "OUTCOME_DERIVED_FEATURE_BUILD"

    def __init__(
        self,
        *,
        journal: TradeJournalDB,
        run_id: str,
        interval_sec: float = 300.0,
        batch_size: int = 25,
        write_timeout_sec: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_sec <= 0 or batch_size <= 0 or write_timeout_sec < 0:
            raise ValueError("derived feature worker bounds must be positive")
        self.journal = journal
        self.run_id = run_id
        self.interval_sec = float(interval_sec)
        self.batch_size = int(batch_size)
        self.write_timeout_sec = float(write_timeout_sec)
        self._monotonic = monotonic
        self._next_due = float("-inf")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _emit(self, *, state: str, **payload: object) -> None:
        self.journal.log_best_effort_strategy_event(self.run_id, self.EVENT, {
            "venue": "hyperliquid_outcome", "read_only": True,
            "live_authority": False, "execution_submitted": False,
            "state": state, "batch_size": self.batch_size,
            "write_timeout_sec": self.write_timeout_sec,
            **payload,
        })

    def run_once(self) -> bool:
        now = self._monotonic()
        if now < self._next_due:
            return False
        # Schedule before work so a slow/rejected build never turns the
        # worker into a tight retry loop against a contended journal.
        self._next_due = now + self.interval_sec
        try:
            oi = OutcomeOiFeaturePipeline(self.journal).build(
                batch_size=self.batch_size, write_timeout_sec=self.write_timeout_sec,
            )
            deribit = OutcomeDeribitFeaturePipeline(self.journal).build(
                batch_size=self.batch_size, write_timeout_sec=self.write_timeout_sec,
            )
        except Exception as exc:
            self._emit(state="build_skipped_or_failed_will_retry", error_type=type(exc).__name__)
            return False
        self._emit(state="built", oi=asdict(oi), deribit=asdict(deribit))
        return True

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="outcome-derived-features")
        self._thread.start()

    def stop(self, *, timeout_sec: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout_sec))
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(0.25)
