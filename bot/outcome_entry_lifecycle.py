"""Durable ownership for Outcome maker-entry orders.

An entry order is allowed to be cancelled only when this runtime can prove it
created the exact exchange order.  A strategy signal alone never grants
ownership of a manually placed buy.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from monitoring.trade_journal_db import TradeJournalDB


@dataclass(frozen=True)
class OutcomeEntryLifecycle:
    wallet: str
    outcome_id: int
    coin: str
    order_id: str
    price: Decimal
    replacement_count: int
    state: str
    updated_at_ts: float | None = None


class OutcomeEntryLifecycleStore:
    EVENT = "OUTCOME_ENTRY_LIFECYCLE"

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id

    def record(self, lifecycle: OutcomeEntryLifecycle, *, reason: str,
               extra: dict[str, Any] | None = None) -> None:
        self.journal.log_strategy_event(self.run_id, self.EVENT, {
            "venue": "hyperliquid_outcome", "wallet": lifecycle.wallet,
            "outcome_id": lifecycle.outcome_id, "coin": lifecycle.coin,
            "order_id": lifecycle.order_id, "price": str(lifecycle.price),
            "replacement_count": lifecycle.replacement_count, "state": lifecycle.state,
            "reason": reason, **(extra or {}),
        })

    def recover(self, *, wallet: str, outcome_id: int, coin: str) -> OutcomeEntryLifecycle | None:
        try:
            with sqlite3.connect(self.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT ts, payload_json FROM strategy_events
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
            payload = json.loads(row[1] or "{}")
            if not isinstance(payload, dict) or payload.get("state") not in {
                "BUY_RESTING", "CANCEL_SUBMITTED", "FILL_PENDING_RECONCILIATION", "RECONCILE_REQUIRED",
            }:
                return None
            return OutcomeEntryLifecycle(
                wallet=str(payload["wallet"]), outcome_id=int(payload["outcome_id"]), coin=str(payload["coin"]),
                order_id=str(payload["order_id"]), price=Decimal(str(payload["price"])),
                replacement_count=int(payload.get("replacement_count", 0)), state=str(payload["state"]),
                updated_at_ts=datetime.fromisoformat(str(row[0])).timestamp(),
            )
        except (KeyError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError):
            return None

    def official_buy_fill(
        self, *, outcome_id: int, coin: str, order_id: str,
    ) -> dict[str, Any] | None:
        """Return one immutable official fill for this owned entry, if recorded.

        A user-fill can arrive before `spotClearinghouseState` exposes its
        resulting token balance.  The durable fill is therefore a safety
        fence, not a substitute for account truth when sizing a protective
        sell.
        """
        try:
            with sqlite3.connect(self.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id, ts, price, qty, payload_json FROM order_events
                    WHERE event_type='ORDER_FILLED' AND side='BUY'
                      AND venue_order_id=? AND instrument_id=?
                      AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                      AND json_extract(payload_json, '$.coin')=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (str(order_id), coin, int(outcome_id), coin),
                ).fetchone()
            if row is None:
                return None
            payload = json.loads(row[4] or "{}")
            if not isinstance(payload, dict) or payload.get("actual_fill") is not True:
                return None
            return {
                "event_id": int(row[0]), "recorded_at": str(row[1]),
                "price": str(row[2]), "quantity": str(row[3]),
                "trade_id": str(payload.get("trade_id") or ""),
            }
        except (TypeError, ValueError, sqlite3.Error, json.JSONDecodeError):
            return None

    def submit_audit(self, *, order_id: str, coin: str) -> dict[str, Any] | None:
        """Return the immutable audited entry context for one owned order."""
        try:
            with sqlite3.connect(f"file:{self.journal.db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """SELECT payload_json FROM order_events
                       WHERE event_type='ORDER_SUBMIT' AND side='BUY'
                         AND venue_order_id=? AND instrument_id=?
                       ORDER BY id DESC LIMIT 1""", (str(order_id), coin),
                ).fetchone()
            payload = json.loads(row[0] or "{}") if row else {}
            audit = payload.get("audit") if isinstance(payload, dict) else None
            return dict(audit) if isinstance(audit, dict) else None
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
            return None

    def fast_rebook_cooldown_remaining(self, *, wallet: str, outcome_id: int, coin: str,
                                       cooldown_sec: float, now: float | None = None) -> float:
        """Persist fast-cancel cooldown semantics across a process restart."""
        try:
            with sqlite3.connect(f"file:{self.journal.db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """SELECT ts, payload_json FROM strategy_events
                       WHERE event_type=?
                         AND json_extract(payload_json, '$.wallet')=?
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=?
                         AND json_extract(payload_json, '$.state')='CANCELLED'
                         AND json_extract(payload_json, '$.reason') LIKE 'entry_fast_risk_cancel:%'
                       ORDER BY id DESC LIMIT 1""",
                    (self.EVENT, wallet, outcome_id, coin),
                ).fetchone()
            if row is None:
                return 0.0
            cancelled_at = datetime.fromisoformat(str(row[0])).timestamp()
            return max(0.0, float(cooldown_sec) - ((now if now is not None else time.time()) - cancelled_at))
        except (sqlite3.Error, TypeError, ValueError):
            return 0.0

    def recover_or_adopt_audited_submit(
        self, *, wallet: str, outcome_id: int, coin: str, open_orders: list[dict[str, Any]],
    ) -> OutcomeEntryLifecycle | None:
        """Recover a lifecycle, or adopt only a matching S0 submit audit.

        The adoption path supports deployment across a process restart.  It
        requires the exact order id, BUY side, coin and decision price from a
        schema-versioned local ORDER_SUBMIT record; arbitrary UI orders fail.
        """
        existing = self.recover(wallet=wallet, outcome_id=outcome_id, coin=coin)
        if existing is not None:
            return existing
        buys = [row for row in open_orders if row.get("coin") == coin and row.get("side") == "B"]
        if len(buys) != 1:
            return None
        order = buys[0]
        order_id = str(order.get("oid") or "")
        if not order_id:
            return None
        try:
            with sqlite3.connect(self.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT payload_json FROM order_events
                    WHERE event_type='ORDER_SUBMIT' AND side='BUY' AND venue_order_id=? AND instrument_id=?
                    ORDER BY id DESC LIMIT 1
                    """, (order_id, coin),
                ).fetchone()
            payload = json.loads(row[0] or "{}") if row else {}
            audit = payload.get("audit") if isinstance(payload, dict) else None
            if not isinstance(audit, dict):
                # Crash-window recovery: exactly one open BUY may be adopted
                # only when an fsync'd, matching pre-submit intent exists.
                with sqlite3.connect(self.journal.db_path) as intent_conn:
                    intent_row = intent_conn.execute(
                        """SELECT payload_json FROM strategy_events
                           WHERE event_type='OUTCOME_ORDER_INTENT'
                             AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                             AND json_extract(payload_json, '$.wallet')=?
                             AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                             AND json_extract(payload_json, '$.coin')=?
                             AND json_extract(payload_json, '$.state')='INTENT_DURABLE'
                           ORDER BY id DESC LIMIT 1""", (wallet, outcome_id, coin),
                    ).fetchone()
                intent = json.loads(intent_row[0] or "{}") if intent_row else {}
                audit = intent.get("audit") if isinstance(intent, dict) else None
                if not isinstance(audit, dict):
                    return None
                expected_shares = Decimal(str(intent.get("shares", "0")))
                if Decimal(str(order.get("sz", "0"))) != expected_shares:
                    return None
                payload = {"outcome_id": intent.get("outcome_id"), "coin": intent.get("coin")}
            if audit.get("entry_policy_schema_version") != 1:
                return None
            if audit.get("entry_policy_kind") not in {
                "s0_oi_spot_mark_confirmation", "s0_spot_mark_tier_b", "s0_trend_continuation",
            }:
                return None
            if int(payload.get("outcome_id")) != outcome_id or str(payload.get("coin")) != coin:
                return None
            price = Decimal(str(audit["entry_bid_at_decision"]))
            order_price = Decimal(str(order.get("limitPx", order.get("px", "0"))))
            if not Decimal("0") < price < Decimal("1") or order_price != price:
                return None
        except (KeyError, TypeError, ValueError, ArithmeticError, sqlite3.Error, json.JSONDecodeError):
            return None
        lifecycle = OutcomeEntryLifecycle(wallet, outcome_id, coin, order_id, price, 0, "BUY_RESTING", time.time())
        self.record(lifecycle, reason="adopted_exact_audited_s0_submit_or_pre_submit_intent_after_restart")
        return lifecycle
