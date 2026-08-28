"""Durable ownership evidence for one Outcome passive exit lifecycle.

The exchange remains the source of truth for inventory and open orders.  This
store only proves which order IDs this runtime is allowed to manage after a
restart; an unrecorded order is never treated as owned.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from monitoring.trade_journal_db import TradeJournalDB


@dataclass(frozen=True)
class OutcomeExitLifecycle:
    wallet: str
    outcome_id: int
    coin: str
    order_id: str
    inventory: Decimal
    target_price: Decimal
    replacement_count: int
    state: str
    updated_event_id: int | None = None


class OutcomeExitLifecycleStore:
    EVENT = "OUTCOME_EXIT_LIFECYCLE"

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id

    def record(self, lifecycle: OutcomeExitLifecycle, *, reason: str, extra: dict[str, Any] | None = None) -> None:
        self.journal.log_strategy_event(self.run_id, self.EVENT, {
            "venue": "hyperliquid_outcome", "wallet": lifecycle.wallet,
            "outcome_id": lifecycle.outcome_id, "coin": lifecycle.coin,
            "order_id": lifecycle.order_id, "inventory": str(lifecycle.inventory),
            "target_price": str(lifecycle.target_price), "replacement_count": lifecycle.replacement_count,
            "state": lifecycle.state, "reason": reason, **(extra or {}),
        })

    def recover(self, *, wallet: str, outcome_id: int, coin: str) -> OutcomeExitLifecycle | None:
        try:
            with sqlite3.connect(self.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id, payload_json FROM strategy_events
                    WHERE event_type=?
                      AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                      AND json_extract(payload_json, '$.wallet')=?
                      AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                      AND json_extract(payload_json, '$.coin')=?
                    ORDER BY id DESC LIMIT 1
                    """, (self.EVENT, wallet, outcome_id, coin),
                ).fetchone()
            if not row:
                return None
            payload = json.loads(row[1])
            if not isinstance(payload, dict) or payload.get("state") not in {"SELL_RESTING", "CANCEL_SUBMITTED", "RECONCILE_REQUIRED"}:
                return None
            return OutcomeExitLifecycle(
                wallet=str(payload["wallet"]), outcome_id=int(payload["outcome_id"]), coin=str(payload["coin"]),
                order_id=str(payload["order_id"]), inventory=Decimal(str(payload["inventory"])),
                target_price=Decimal(str(payload["target_price"])), replacement_count=int(payload.get("replacement_count", 0)),
                state=str(payload["state"]), updated_event_id=int(row[0]),
            )
        except (KeyError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError):
            return None

    def reconcile_owned_sell(self, *, wallet: str, outcome_id: int, coin: str, inventory: Decimal,
                              open_orders: list[dict[str, Any]]) -> OutcomeExitLifecycle | None:
        lifecycle = self.recover(wallet=wallet, outcome_id=outcome_id, coin=coin)
        if lifecycle is None:
            return None
        matching = [row for row in open_orders if str(row.get("oid")) == lifecycle.order_id and row.get("coin") == coin and row.get("side") == "A"]
        if len(matching) != 1 or inventory <= 0 or Decimal(str(matching[0].get("sz", "0"))) < inventory:
            self.record(lifecycle, reason="account_truth_does_not_match_owned_sell", extra={"state": "RECONCILE_REQUIRED"})
            return None
        return lifecycle
