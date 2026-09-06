"""Read-only settlement and canonical-PnL scheduler outside the order lane."""
from __future__ import annotations

import threading
import time
from typing import Any, Iterable

from loguru import logger

from bot.outcome_pnl_reconciliation import OutcomePnLReconciler
from bot.outcome_settlement import OutcomeSettlementAdapter
from monitoring.trade_journal_db import TradeJournalDB


class OutcomeSettlementWorker:
    """Keep slow SDK settlement evidence off the live entry/reprice loop.

    It owns no exchange mutation.  The supplied account client is a dedicated
    read-only client, never the runtime's wallet writer.  Candidate ids are
    monotonic: a pending old market remains checked until payout/zero-balance
    evidence lets the reconciler record canonical settlement.
    """

    def __init__(self, *, account: Any, settlement_adapter: OutcomeSettlementAdapter,
                 pnl_reconciler: OutcomePnLReconciler, journal: TradeJournalDB,
                 interval_sec: float = 30.0) -> None:
        self.account = account
        self.settlement_adapter = settlement_adapter
        self.pnl_reconciler = pnl_reconciler
        self.journal = journal
        self.interval_sec = max(1.0, float(interval_sec))
        self._candidate_lock = threading.Lock()
        self._candidate_ids: set[int] = set()
        self._settled_ids: set[int] = set()
        self._last_attempt_at: dict[int, float] = {}
        self._last_pnl_at = 0.0
        self._last_cycle_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add_candidates(self, outcome_ids: Iterable[int]) -> None:
        with self._candidate_lock:
            self._candidate_ids.update(int(outcome_id) for outcome_id in outcome_ids if int(outcome_id) > 0)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="outcome-settlement-worker")
        self._thread.start()

    def stop(self, *, timeout_sec: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout_sec))
        self._thread = None

    def _candidate_snapshot(self) -> set[int]:
        with self._candidate_lock:
            return set(self._candidate_ids)

    def run_once(self, *, now_monotonic: float | None = None) -> None:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        # The thread wakes frequently only so stop() is responsive.  Its
        # historical journal work and SDK calls retain a hard 30-second
        # cadence rather than repeatedly scanning lots four times per second.
        if now - self._last_cycle_at < self.interval_sec:
            return
        self._last_cycle_at = now
        # FIFO attribution is a journal-only write.  Its historical scan must
        # never delay an ALO entry, cancellation or protective exit.
        if now - self._last_pnl_at >= self.interval_sec:
            self._last_pnl_at = now
            try:
                written = self.pnl_reconciler.reconcile_sells()
                if written:
                    logger.info(f"[OUTCOME PNL] recorded {written} canonical FIFO sell lots")
            except Exception as exc:
                self.journal.log_strategy_event("outcome-pnl-error", "OUTCOME_PNL_RECONCILE_ERROR", {
                    "venue": "hyperliquid_outcome", "stage": "sell_fifo",
                    "error_type": type(exc).__name__, "error": str(exc),
                })
        try:
            candidates = self._candidate_snapshot() | self.pnl_reconciler.unresolved_outcome_ids()
        except Exception as exc:
            self.journal.log_strategy_event("outcome-settlement-error", "OUTCOME_SETTLEMENT_CANDIDATE_ERROR", {
                "venue": "hyperliquid_outcome", "error_type": type(exc).__name__, "error": str(exc),
            })
            return
        for outcome_id in sorted(candidates - self._settled_ids):
            if now - self._last_attempt_at.get(outcome_id, 0.0) < self.interval_sec:
                continue
            self._last_attempt_at[outcome_id] = now
            try:
                settlement = self.settlement_adapter.fetch_outcome_id(outcome_id)
                if not settlement.settled:
                    logger.info(f"[OUTCOME SETTLEMENT] #{outcome_id} not yet confirmed by official SDK; holding state.")
                    continue
                logger.info(
                    f"[OUTCOME SETTLEMENT] #{outcome_id} confirmed by official SDK "
                    f"fraction={settlement.settle_fraction} details={settlement.details}"
                )
                status = self.pnl_reconciler.reconcile_settlement(
                    settlement=settlement,
                    raw_fills=self.account.get_user_fills_sync(self.account.wallet_address),
                    clearinghouse=self.account.get_spot_clearinghouse_state_sync(self.account.wallet_address),
                )
                if status in {"recorded", "already_recorded"}:
                    self._settled_ids.add(outcome_id)
                    logger.info(f"[OUTCOME SETTLEMENT] #{outcome_id} canonical PnL {status}")
                else:
                    logger.info(
                        f"[OUTCOME SETTLEMENT] #{outcome_id} confirmation retained; canonical PnL {status}"
                    )
            except Exception as exc:
                self.journal.log_strategy_event("outcome-settlement-error", "OUTCOME_SETTLEMENT_WORKER_ERROR", {
                    "venue": "hyperliquid_outcome", "outcome_id": outcome_id,
                    "error_type": type(exc).__name__, "error": str(exc),
                })
                logger.warning(f"[OUTCOME SETTLEMENT] #{outcome_id} official confirmation unavailable: {exc}")

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(0.25)
