"""Read-only post-exit executable-path evidence for an IOC-closed lifecycle."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from monitoring.trade_journal_db import TradeJournalDB


@dataclass(frozen=True)
class ExitContinuation:
    exit_id: str
    outcome_id: int
    coin: str
    side_index: int
    entry_vwap: Decimal
    inventory: Decimal
    exit_timestamp: float
    targets_due_sec: tuple[int, ...]


class OutcomeExitContinuationObserver:
    """Persist only fixed post-IOC checkpoints; it owns no trade authority."""

    REGISTER_EVENT = "OUTCOME_EXIT_CONTINUATION_REGISTERED"
    SAMPLE_EVENT = "OUTCOME_EXIT_CONTINUATION_OBSERVATION"
    TARGETS_SEC = (300, 900, 1800)

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id

    def register(self, *, outcome_id: int, coin: str, side_index: int, entry_vwap: Decimal, inventory: Decimal,
                 exit_order_id: str, execution_type: str, exit_timestamp: float | None = None) -> None:
        when = time.time() if exit_timestamp is None else float(exit_timestamp)
        exit_id = f"{outcome_id}:{coin}:{exit_order_id}"
        self.journal.log_best_effort_strategy_event(self.run_id, self.REGISTER_EVENT, {
            "read_only": True, "live_authority": False, "execution_submitted": False,
            "exit_id": exit_id, "outcome_id": outcome_id, "coin": coin, "side_index": side_index,
            "entry_vwap": str(entry_vwap), "inventory": str(inventory), "exit_timestamp": when,
            "targets_sec": list(self.TARGETS_SEC), "execution_type": execution_type,
        })

    def due(self, *, outcome_id: int, now: float | None = None) -> list[tuple[ExitContinuation, int]]:
        current = time.time() if now is None else float(now)
        registrations: dict[str, dict[str, Any]] = {}
        samples: set[tuple[str, int]] = set()
        with self.journal._connect(timeout_sec=0.05) as conn:  # read-only query; never blocks execution
            for _ts, raw in conn.execute("SELECT ts,payload_json FROM strategy_events WHERE event_type=? ORDER BY id", (self.REGISTER_EVENT,)):
                try:
                    item = json.loads(raw or "{}")
                    if int(item.get("outcome_id")) == outcome_id:
                        registrations[str(item["exit_id"])] = item
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    continue
            for _ts, raw in conn.execute("SELECT ts,payload_json FROM strategy_events WHERE event_type=? ORDER BY id", (self.SAMPLE_EVENT,)):
                try:
                    item = json.loads(raw or "{}")
                    samples.add((str(item["exit_id"]), int(item["target_sec"])))
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    continue
        result: list[tuple[ExitContinuation, int]] = []
        for item in registrations.values():
            try:
                record = ExitContinuation(str(item["exit_id"]), int(item["outcome_id"]), str(item["coin"]),
                    int(item["side_index"]), Decimal(str(item["entry_vwap"])), Decimal(str(item["inventory"])), float(item["exit_timestamp"]),
                    tuple(int(v) for v in item.get("targets_sec", self.TARGETS_SEC)))
            except (TypeError, ValueError, ArithmeticError):
                continue
            for target in record.targets_due_sec:
                if current >= record.exit_timestamp + target and (record.exit_id, target) not in samples:
                    result.append((record, target))
        return result

    def record(self, *, continuation: ExitContinuation, target_sec: int, best_bid: Decimal,
               best_ask: Decimal, marketable_vwap: Decimal | None, depth_shares: Decimal) -> bool:
        return self.journal.log_best_effort_strategy_event(self.run_id, self.SAMPLE_EVENT, {
            "read_only": True, "live_authority": False, "execution_submitted": False,
            "exit_id": continuation.exit_id, "outcome_id": continuation.outcome_id, "coin": continuation.coin,
            "side_index": continuation.side_index, "target_sec": target_sec,
            "entry_vwap": str(continuation.entry_vwap), "best_bid": str(best_bid), "best_ask": str(best_ask),
            "marketable_exit_vwap": str(marketable_vwap) if marketable_vwap is not None else None,
            "marketable_exit_depth_shares": str(depth_shares),
            "recorded_after_exit_sec": max(0.0, time.time() - continuation.exit_timestamp),
        }) is not None
