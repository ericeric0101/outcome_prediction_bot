"""Durable post-loss re-entry guard based only on official fill evidence."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from monitoring.trade_journal_db import TradeJournalDB


@dataclass(frozen=True)
class OutcomeLossReentryDecision:
    allowed: bool
    reason: str


class OutcomeLossReentryGate:
    EVENT = "OUTCOME_LOSS_EXIT_CONFIRMED"

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id

    def record_confirmed_loss_exit(self, *, outcome_id: int, period: str, coin: str, order_id: str) -> bool:
        """Record only if a matching official userFill-derived SELL exists."""
        try:
            with sqlite3.connect(self.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id, ts, payload_json FROM order_events
                    WHERE event_type='ORDER_FILLED' AND instrument_id=? AND venue_order_id=? AND side='SELL'
                      AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                      AND json_extract(payload_json, '$.actual_fill')=1
                    ORDER BY id DESC LIMIT 1
                    """, (coin, order_id),
                ).fetchone()
                already = conn.execute(
                    """
                    SELECT 1 FROM strategy_events WHERE event_type=?
                      AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                      AND json_extract(payload_json, '$.coin')=? LIMIT 1
                    """, (self.EVENT, outcome_id, coin),
                ).fetchone()
            if row is None or already is not None:
                return False
            payload = json.loads(row[2] or "{}")
            self.journal.log_strategy_event(self.run_id, self.EVENT, {
                "venue": "hyperliquid_outcome", "outcome_id": outcome_id,
                "period": period, "coin": coin, "order_id": order_id,
                "official_fill_order_event_id": int(row[0]), "official_fill_ts": row[1],
                "fill_provenance": payload.get("fill_provenance"),
                "reentry_policy": "block_same_market_until_rollover",
            })
            return True
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            return False

    def evaluate(self, *, outcome_id: int) -> OutcomeLossReentryDecision:
        try:
            with sqlite3.connect(f"file:{Path(self.journal.db_path).resolve()}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """SELECT 1 FROM strategy_events WHERE event_type=?
                       AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=? LIMIT 1""",
                    (self.EVENT, outcome_id),
                ).fetchone()
            return OutcomeLossReentryDecision(row is None, "no_confirmed_loss_exit" if row is None else "loss_reentry_blocked_until_market_rollover")
        except sqlite3.Error:
            return OutcomeLossReentryDecision(False, "loss_reentry_journal_unavailable")
