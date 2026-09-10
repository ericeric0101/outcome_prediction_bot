"""Read-only settlement and canonical-PnL scheduler outside the order lane."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
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

    # The documented userFillsByTime endpoint is bounded.  This overlap makes
    # delayed/duplicate delivery harmless because evidence is deduplicated by
    # official trade id, while the persisted high-water mark prevents a
    # short generic userFills window from losing a late payout after restart.
    PAYOUT_CURSOR_OVERLAP_MS = 60_000
    PAYOUT_INITIAL_LOOKBACK_MS = 7 * 24 * 60 * 60 * 1000
    PAYOUT_MAX_RESPONSE_FILLS = 2_000

    @staticmethod
    def _fill_timestamp_ms(fill: Any) -> int | None:
        if not isinstance(fill, dict):
            return None
        try:
            return int(fill.get("time", fill.get("timestamp")))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _deduplicate_fills(*collections: Iterable[Any]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for collection in collections:
            for fill in collection:
                if not isinstance(fill, dict):
                    continue
                key = str(fill.get("tid") or fill.get("tradeId") or "")
                if not key:
                    key = "fallback:" + ":".join(str(fill.get(field, "")) for field in (
                        "hash", "oid", "coin", "side", "px", "sz", "time", "dir",
                    ))
                if key in seen:
                    continue
                seen.add(key)
                output.append(fill)
        return output

    def _capture_payout_fill_window(self) -> list[dict[str, Any]]:
        """Persist a complete official payout window before it ages out.

        This never controls active-market orders.  If a response is saturated
        at the venue's documented limit, it refuses to advance the cursor and
        falls back to existing current-window evidence rather than pretending
        the historical range was complete.
        """
        method = getattr(self.account, "get_user_fills_by_time_sync", None)
        if not callable(method):
            return self.journal.load_outcome_settlement_payout_fills(self.account.wallet_address)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        cursor = self.journal.load_outcome_settlement_fill_cursor(self.account.wallet_address)
        start_ms = (
            max(0, cursor - self.PAYOUT_CURSOR_OVERLAP_MS)
            if cursor is not None else now_ms - self.PAYOUT_INITIAL_LOOKBACK_MS
        )
        try:
            fetched = method(
                self.account.wallet_address, start_time_ms=start_ms, end_time_ms=now_ms,
            )
            if not isinstance(fetched, list):
                raise TypeError("userFillsByTime response is not a list")
            if len(fetched) >= self.PAYOUT_MAX_RESPONSE_FILLS:
                self.journal.log_strategy_event("outcome-settlement-error", "OUTCOME_SETTLEMENT_PAYOUT_CURSOR_BLOCKED", {
                    "venue": "hyperliquid_outcome", "reason": "user_fills_by_time_response_saturated",
                    "start_time_ms": start_ms, "end_time_ms": now_ms, "response_count": len(fetched),
                })
                return self.journal.load_outcome_settlement_payout_fills(self.account.wallet_address)
            timestamps = [timestamp for item in fetched if (timestamp := self._fill_timestamp_ms(item)) is not None]
            # A successful empty interval must still advance the high-water
            # mark, otherwise every future cycle repeatedly reads seven days.
            self.journal.record_outcome_settlement_payout_fills(
                wallet_address=self.account.wallet_address,
                fills=[item for item in fetched if isinstance(item, dict)],
                cursor_timestamp_ms=max(timestamps, default=now_ms),
            )
        except Exception as exc:
            self.journal.log_strategy_event("outcome-settlement-error", "OUTCOME_SETTLEMENT_PAYOUT_CURSOR_ERROR", {
                "venue": "hyperliquid_outcome", "error_type": type(exc).__name__, "error": str(exc),
                "start_time_ms": start_ms,
            })
        return self.journal.load_outcome_settlement_payout_fills(self.account.wallet_address)

    def _log_status_transition(self, outcome_id: int, status: str, detail: str) -> None:
        if self.journal.update_outcome_settlement_status(outcome_id=outcome_id, status=status):
            logger.info(f"[OUTCOME SETTLEMENT] #{outcome_id} {detail}")

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
        payout_history = self._capture_payout_fill_window()
        for outcome_id in sorted(candidates - self._settled_ids):
            if now - self._last_attempt_at.get(outcome_id, 0.0) < self.interval_sec:
                continue
            self._last_attempt_at[outcome_id] = now
            try:
                settlement = self.settlement_adapter.fetch_outcome_id(outcome_id)
                if not settlement.settled:
                    self._log_status_transition(
                        outcome_id, "pending_official_sdk",
                        "not yet confirmed by official SDK; holding state.",
                    )
                    continue
                current_fills = self.account.get_user_fills_sync(self.account.wallet_address)
                status = self.pnl_reconciler.reconcile_settlement(
                    settlement=settlement,
                    raw_fills=self._deduplicate_fills(current_fills, payout_history),
                    clearinghouse=self.account.get_spot_clearinghouse_state_sync(self.account.wallet_address),
                )
                if status in {"recorded", "already_recorded"}:
                    self._settled_ids.add(outcome_id)
                    self._log_status_transition(
                        outcome_id, status,
                        f"official SDK confirmed fraction={settlement.settle_fraction}; canonical PnL {status}",
                    )
                else:
                    self._log_status_transition(
                        outcome_id, status,
                        f"official SDK confirmed fraction={settlement.settle_fraction}; canonical PnL {status}",
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
