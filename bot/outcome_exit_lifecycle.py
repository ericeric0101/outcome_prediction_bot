"""Durable ownership evidence for one Outcome passive exit lifecycle.

The exchange remains the source of truth for inventory and open orders.  This
store only proves which order IDs this runtime is allowed to manage after a
restart; an unrecorded order is never treated as owned.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime
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
    updated_at_ts: float | None = None


class OutcomeExitLifecycleStore:
    EVENT = "OUTCOME_EXIT_LIFECYCLE"
    AMBIGUOUS_SUBMIT_EVENT = "OUTCOME_EXIT_ORDER_AMBIGUOUS_SUBMIT"
    AMBIGUITY_RESOLVED_EVENT = "OUTCOME_EXIT_ORDER_AMBIGUITY_RESOLVED"
    INTENT_FINALIZED_EVENT = "OUTCOME_EXIT_ORDER_INTENT_FINALIZED"
    MAX_EMERGENCY_ATTEMPTS = 2
    AMBIGUITY_VISIBILITY_FENCE_SEC = 15.0

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id

    def record(
        self, lifecycle: OutcomeExitLifecycle, *, reason: str,
        extra: dict[str, Any] | None = None, durable: bool = False,
    ) -> int | None:
        payload = {
            "venue": "hyperliquid_outcome", "wallet": lifecycle.wallet,
            "outcome_id": lifecycle.outcome_id, "coin": lifecycle.coin,
            "order_id": lifecycle.order_id, "inventory": str(lifecycle.inventory),
            "target_price": str(lifecycle.target_price), "replacement_count": lifecycle.replacement_count,
            "state": lifecycle.state, "reason": reason, **(extra or {}),
        }
        if durable:
            return self.journal.log_durable_strategy_event(self.run_id, self.EVENT, payload)
        return self.journal.log_strategy_event(self.run_id, self.EVENT, payload)

    def record_replacement_submit(
        self, lifecycle: OutcomeExitLifecycle, *, old_order_id: str | None, trigger_bbo: dict[str, Any],
        loss_threshold: Decimal | None, current_signal: dict[str, Any], plan_reason: str,
        execution_timing: dict[str, Any] | None = None, intent_id: str | None = None,
        execution_type: str = "exit_cancel_confirm_rebook_alo",
    ) -> None:
        """Make every rebook joinable to its later exchange fill.

        ``OUTCOME_EXIT_LIFECYCLE`` proves ownership state.  This separate
        canonical ``ORDER_SUBMIT`` row supplies the venue order id used by the
        fill reconciler and preserves the decision-time (not post-cancel) BBO.
        """
        self.journal.log_order_event(
            self.run_id, "ORDER_SUBMIT", venue_order_id=lifecycle.order_id,
            side="SELL", price=float(lifecycle.target_price), qty=float(lifecycle.inventory),
            status="ALO_REPLACEMENT_SUBMITTED", reason=plan_reason, instrument_id=lifecycle.coin,
            payload={
                "venue": "hyperliquid_outcome", "outcome_id": lifecycle.outcome_id,
                "coin": lifecycle.coin, "execution_type": execution_type,
                "old_order_id": old_order_id, "replacement_price": str(lifecycle.target_price),
                "replacement_count": lifecycle.replacement_count,
                "trigger_bbo": trigger_bbo,
                "loss_threshold": str(loss_threshold) if loss_threshold is not None else None,
                "current_signal": current_signal,
                "execution_timing": execution_timing or {},
                "exit_intent_id": intent_id,
            },
        )

    def record_submit_intent(
        self, *, wallet: str, outcome_id: int, coin: str, order_kind: str,
        price: Decimal, shares: Decimal, old_order_id: str | None,
        replacement_count: int, intended_state: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[str, int] | None:
        """Commit exact SELL intent before a venue mutation.

        The UUID prevents same-millisecond collisions and the journal method
        uses ``synchronous=FULL``.  A caller must fail closed if this returns
        ``None``.
        """
        intent_id = f"exit:{outcome_id}:{coin}:{uuid.uuid4().hex}"
        event_id = self.journal.log_durable_order_intent(self.run_id, {
            "venue": "hyperliquid_outcome", "wallet": wallet,
            "intent_id": intent_id, "state": "INTENT_DURABLE",
            "outcome_id": int(outcome_id), "coin": coin, "side": "SELL",
            "order_kind": order_kind, "price": str(price), "shares": str(shares),
            "old_order_id": old_order_id, "replacement_count": int(replacement_count),
            "intended_state": intended_state, "context": context or {},
        })
        return (intent_id, int(event_id)) if event_id is not None else None

    def record_ambiguous_submit(
        self, *, wallet: str, outcome_id: int, coin: str, intent_id: str,
        intent_event_id: int, order_kind: str, price: Decimal, shares: Decimal,
        old_order_id: str | None, replacement_count: int, intended_state: str,
        sidecar_request_id: str, command: str, detail: str,
    ) -> int | None:
        return self.journal.log_durable_strategy_event(self.run_id, self.AMBIGUOUS_SUBMIT_EVENT, {
            "venue": "hyperliquid_outcome", "wallet": wallet,
            "outcome_id": int(outcome_id), "coin": coin, "side": "SELL",
            "intent_id": intent_id, "intent_event_id": int(intent_event_id),
            "order_kind": order_kind, "price": str(price), "shares": str(shares),
            "old_order_id": old_order_id, "replacement_count": int(replacement_count),
            "intended_state": intended_state, "sidecar_request_id": sidecar_request_id,
            "command": command, "detail": detail, "state": "RECONCILIATION_REQUIRED",
        })

    def pending_ambiguous_submit(
        self, *, wallet: str, outcome_id: int, coin: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            coin_clause = " AND json_extract(payload_json, '$.coin')=?" if coin is not None else ""
            params: list[Any] = [wallet, int(outcome_id)]
            if coin is not None:
                params.append(coin)
            with sqlite3.connect(f"file:{self.journal.db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    f"""SELECT id, ts, payload_json FROM strategy_events
                        WHERE event_type='OUTCOME_ORDER_INTENT'
                          AND json_extract(payload_json, '$.wallet')=?
                          AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                          AND json_extract(payload_json, '$.side')='SELL'
                          {coin_clause}
                        ORDER BY id DESC LIMIT 1""", params,
                ).fetchone()
                if row is None:
                    return None
                payload = json.loads(row[2] or "{}")
                if not isinstance(payload, dict):
                    return None
                resolved = conn.execute(
                    """SELECT 1 FROM strategy_events WHERE event_type IN (?, ?) AND id>?
                       AND json_extract(payload_json, '$.intent_id')=? LIMIT 1""",
                    (self.AMBIGUITY_RESOLVED_EVENT, self.INTENT_FINALIZED_EVENT,
                     int(row[0]), str(payload.get("intent_id") or "")),
                ).fetchone()
                ambiguity = conn.execute(
                    """SELECT id, ts, payload_json FROM strategy_events WHERE event_type=? AND id>?
                       AND json_extract(payload_json, '$.intent_id')=? ORDER BY id DESC LIMIT 1""",
                    (self.AMBIGUOUS_SUBMIT_EVENT, int(row[0]), str(payload.get("intent_id") or "")),
                ).fetchone()
            if resolved:
                return None
            if ambiguity is not None:
                ambiguity_payload = json.loads(ambiguity[2] or "{}")
                if isinstance(ambiguity_payload, dict):
                    payload.update(ambiguity_payload)
                payload["ambiguity_event_id"] = int(ambiguity[0])
                payload["ambiguity_recorded_at_ts"] = datetime.fromisoformat(str(ambiguity[1])).timestamp()
            else:
                # Covers a process crash after the durable intent but before
                # an ACK or explicit ambiguity event could be persisted.
                payload["ambiguity_event_id"] = None
                payload["ambiguity_recorded_at_ts"] = datetime.fromisoformat(str(row[1])).timestamp()
                payload["state"] = "UNACKNOWLEDGED_INTENT"
            return payload
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            return {"reason": "ambiguous_exit_journal_unreadable"}

    def resolve_ambiguous_submit(self, *, intent_id: str, order_id: str | None, reason: str) -> bool:
        return self.journal.log_durable_strategy_event(self.run_id, self.AMBIGUITY_RESOLVED_EVENT, {
            "intent_id": intent_id, "order_id": order_id, "reason": reason,
            "state": "RESOLVED_BY_ACCOUNT_TRUTH",
        }) is not None

    def finalize_submit_intent(self, *, intent_id: str, order_id: str | None, reason: str) -> bool:
        return self.journal.log_durable_strategy_event(self.run_id, self.INTENT_FINALIZED_EVENT, {
            "intent_id": intent_id, "order_id": order_id, "reason": reason,
            "state": "FINALIZED",
        }) is not None

    def reconcile_ambiguous_submit(
        self, *, wallet: str, outcome_id: int, coin: str, inventory: Decimal,
        open_orders: list[dict[str, Any]], now: float | None = None,
    ) -> tuple[str, OutcomeExitLifecycle | None]:
        """Adopt an ACK-lost ALO or resolve it only from fresh account truth.

        Matching is intentionally strict.  An unrelated/manual SELL is never
        adopted merely because the market and side match.
        """
        pending = self.pending_ambiguous_submit(wallet=wallet, outcome_id=outcome_id, coin=coin)
        if pending is None:
            return "none", None
        try:
            intent_id = str(pending["intent_id"])
            intended_price = Decimal(str(pending["price"]))
            intended_shares = Decimal(str(pending["shares"]))
            order_kind = str(pending["order_kind"])
            intended_state = str(pending["intended_state"])
            replacement_count = int(pending.get("replacement_count", 0))
            recorded_at = float(pending["ambiguity_recorded_at_ts"])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return "pending", None

        if order_kind != "emergency_ioc":
            matches = []
            for row in open_orders:
                try:
                    size = Decimal(str(row.get("sz", "0")))
                    price = Decimal(str(row.get("limitPx", row.get("px", row.get("price", "0")))))
                except (TypeError, ValueError, ArithmeticError):
                    continue
                if (row.get("coin") == coin and row.get("side") == "A" and price == intended_price
                        and inventory > 0 and size == inventory and size <= intended_shares):
                    matches.append(row)
            if len(matches) == 1:
                order_id = str(matches[0].get("oid"))
                lifecycle = OutcomeExitLifecycle(
                    wallet, outcome_id, coin, order_id, inventory, intended_price,
                    replacement_count, intended_state,
                )
                self.record_replacement_submit(
                    lifecycle, old_order_id=pending.get("old_order_id"),
                    trigger_bbo={}, loss_threshold=None, current_signal={},
                    plan_reason="adopted_ambiguous_exit_from_account_truth", intent_id=intent_id,
                    execution_type=f"adopted_ambiguous_{order_kind}",
                )
                recorded = self.record(
                    lifecycle, reason="adopted_ambiguous_exit_from_account_truth",
                    extra={"exit_intent_id": intent_id}, durable=True,
                )
                if recorded is None:
                    return "pending", None
                if not self.resolve_ambiguous_submit(intent_id=intent_id, order_id=order_id, reason="matching_resting_sell"):
                    return "pending", None
                return "adopted", lifecycle

        # A missing resting order plus lower inventory proves that some SELL
        # execution occurred.  It does not prove exact fill price, so retain a
        # reconciliation state for residual inventory.
        if Decimal("0") <= inventory < intended_shares:
            state = "CLOSED" if inventory <= 0 else "RECONCILE_REQUIRED"
            lifecycle = OutcomeExitLifecycle(
                wallet, outcome_id, coin, str(pending.get("old_order_id") or f"ambiguous:{intent_id}"),
                inventory, intended_price, replacement_count, state,
            )
            recorded = self.record(
                lifecycle, reason="ambiguous_exit_inventory_change_confirmed",
                extra={"exit_intent_id": intent_id}, durable=True,
            )
            if recorded is None:
                return "pending", None
            if not self.resolve_ambiguous_submit(intent_id=intent_id, order_id=None, reason="inventory_decreased"):
                return "pending", None
            return "resolved_inventory_change", lifecycle

        age = (now if now is not None else time.time()) - recorded_at
        if age >= self.AMBIGUITY_VISIBILITY_FENCE_SEC and not any(
            row.get("coin") == coin and row.get("side") == "A" for row in open_orders
        ):
            if self.resolve_ambiguous_submit(intent_id=intent_id, order_id=None, reason="no_order_or_fill_after_visibility_fence"):
                return "resolved_no_execution", None
        return "pending", None

    def recover(self, *, wallet: str, outcome_id: int, coin: str) -> OutcomeExitLifecycle | None:
        try:
            with sqlite3.connect(self.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id, ts, payload_json FROM strategy_events
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
            payload = json.loads(row[2])
            if not isinstance(payload, dict) or payload.get("state") not in {
                "SELL_RESTING", "LOSS_BAND_RESTING", "LOSS_BAND_UNFILLED",
                "REVERSAL_CONFIRMED", "CANCEL_SUBMITTED", "RECONCILE_REQUIRED",
                "EMERGENCY_CANCEL_SUBMITTED", "EMERGENCY_EXIT_SUBMITTED", "EMERGENCY_RESIDUAL",
            }:
                return None
            return OutcomeExitLifecycle(
                wallet=str(payload["wallet"]), outcome_id=int(payload["outcome_id"]), coin=str(payload["coin"]),
                order_id=str(payload["order_id"]), inventory=Decimal(str(payload["inventory"])),
                target_price=Decimal(str(payload["target_price"])), replacement_count=int(payload.get("replacement_count", 0)),
                state=str(payload["state"]), updated_event_id=int(row[0]),
                updated_at_ts=datetime.fromisoformat(str(row[1])).timestamp(),
            )
        except (KeyError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError):
            return None

    def latest_owned_sell_fill_at_ms(
        self, *, wallet: str, outcome_id: int, coin: str,
    ) -> tuple[int, str] | None:
        """Return a completed SELL only when its bot ownership is provable.

        A user-fill row alone is not sufficient: it could be a manual order.
        The matching local ``ORDER_SUBMIT`` is immutable evidence that this
        runtime owned the exact order.  This is an admission fence, never a
        source for inventory or fill-size inference.
        """
        try:
            with sqlite3.connect(f"file:{self.journal.db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """
                    SELECT filled.ts, filled.venue_order_id
                    FROM order_events AS filled
                    WHERE filled.event_type='ORDER_FILLED' AND filled.side='SELL'
                      AND filled.instrument_id=?
                      AND json_extract(filled.payload_json, '$.venue')='hyperliquid_outcome'
                      AND json_extract(filled.payload_json, '$.actual_fill')=1
                      AND EXISTS (
                          SELECT 1 FROM order_events AS submitted
                          WHERE submitted.event_type='ORDER_SUBMIT' AND submitted.side='SELL'
                            AND submitted.venue_order_id=filled.venue_order_id
                            AND submitted.instrument_id=filled.instrument_id
                            AND json_extract(submitted.payload_json, '$.venue')='hyperliquid_outcome'
                            AND json_extract(submitted.payload_json, '$.outcome_id')=?
                      )
                    ORDER BY filled.id DESC LIMIT 1
                    """,
                    (coin, int(outcome_id)),
                ).fetchone()
            if row is None:
                return None
            return (int(datetime.fromisoformat(str(row[0])).timestamp() * 1000), str(row[1]))
        except (sqlite3.Error, TypeError, ValueError):
            # A failed local audit read must not manufacture an authorization.
            return None

    def loss_band_first_seen_ts(self, *, wallet: str, outcome_id: int, coin: str) -> float | None:
        """Return durable first passive-loss evidence, never a guessed timer."""
        try:
            with sqlite3.connect(self.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT ts FROM strategy_events
                    WHERE event_type=?
                      AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                      AND json_extract(payload_json, '$.wallet')=?
                      AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                      AND json_extract(payload_json, '$.coin')=?
                      AND json_extract(payload_json, '$.state') IN ('LOSS_BAND_RESTING', 'LOSS_BAND_UNFILLED')
                    ORDER BY id ASC LIMIT 1
                    """, (self.EVENT, wallet, outcome_id, coin),
                ).fetchone()
            return datetime.fromisoformat(str(row[0])).timestamp() if row else None
        except (TypeError, ValueError, sqlite3.Error):
            return None

    def emergency_attempt_count(self, *, wallet: str, outcome_id: int, coin: str) -> int:
        """Return durable accepted IOC attempts; submit is not equivalent to flat."""
        try:
            with sqlite3.connect(self.journal.db_path) as conn:
                accepted = conn.execute(
                    """
                    SELECT COUNT(*) FROM strategy_events
                    WHERE event_type=?
                      AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                      AND json_extract(payload_json, '$.wallet')=?
                      AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                      AND json_extract(payload_json, '$.coin')=?
                      AND json_extract(payload_json, '$.state')='EMERGENCY_EXIT_SUBMITTED'
                    LIMIT 1
                    """, (self.EVENT, wallet, outcome_id, coin),
                ).fetchone()
                ambiguous = conn.execute(
                    """SELECT COUNT(*) FROM strategy_events
                       WHERE event_type=?
                         AND json_extract(payload_json, '$.wallet')=?
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=?
                         AND json_extract(payload_json, '$.order_kind')='emergency_ioc'""",
                    (self.AMBIGUOUS_SUBMIT_EVENT, wallet, outcome_id, coin),
                ).fetchone()
            # An ambiguous IOC may have reached the venue and therefore
            # consumes the bounded mutation budget conservatively.
            return int(accepted[0] or 0) + int(ambiguous[0] or 0)
        except sqlite3.Error:
            # A journal failure must not reopen an emergency execution budget.
            return self.MAX_EMERGENCY_ATTEMPTS

    def emergency_attempted(self, *, wallet: str, outcome_id: int, coin: str) -> bool:
        """True only after the bounded retry budget is exhausted."""
        return self.emergency_attempt_count(wallet=wallet, outcome_id=outcome_id, coin=coin) >= self.MAX_EMERGENCY_ATTEMPTS

    def has_official_sell_fill(self, *, coin: str, order_id: str) -> bool:
        """Whether the immutable user-fill bridge confirms this exact SELL."""
        try:
            with sqlite3.connect(f"file:{self.journal.db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """SELECT 1 FROM order_events
                       WHERE event_type='ORDER_FILLED' AND side='SELL'
                         AND instrument_id=? AND venue_order_id=?
                         AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                         AND json_extract(payload_json, '$.actual_fill')=1
                       LIMIT 1""",
                    (coin, str(order_id)),
                ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    def reconcile_owned_sell(self, *, wallet: str, outcome_id: int, coin: str, inventory: Decimal,
                              open_orders: list[dict[str, Any]]) -> OutcomeExitLifecycle | None:
        status, adopted = self.reconcile_ambiguous_submit(
            wallet=wallet, outcome_id=outcome_id, coin=coin,
            inventory=inventory, open_orders=open_orders,
        )
        if status == "adopted":
            return adopted
        if status == "pending":
            return None
        lifecycle = self.recover(wallet=wallet, outcome_id=outcome_id, coin=coin)
        if lifecycle is None:
            return None
        candidate_sells = [
            row for row in open_orders
            if row.get("coin") == coin and row.get("side") == "A"
        ]
        matching = [row for row in candidate_sells if str(row.get("oid")) == lifecycle.order_id]
        # A closed lifecycle needs an official fill in addition to two account
        # facts: no remaining inventory and absence of the owned order.  A
        # balance/open-order snapshot can briefly look flat while a maker SELL
        # is in flight; terminalising from that transient view would permit a
        # second BUY before the exit actually completes.
        if inventory <= 0 and not matching:
            if not self.has_official_sell_fill(coin=coin, order_id=lifecycle.order_id):
                self.record(
                    lifecycle, reason="owned_sell_absent_waiting_for_official_fill",
                    extra={"state": "RECONCILE_REQUIRED"}, durable=True,
                )
                return None
            closed = OutcomeExitLifecycle(
                lifecycle.wallet, lifecycle.outcome_id, lifecycle.coin, lifecycle.order_id,
                lifecycle.inventory, lifecycle.target_price, lifecycle.replacement_count, "CLOSED",
            )
            self.record(closed, reason="inventory_flat_and_owned_sell_absent", extra={"prior_state": lifecycle.state})
            return None
        # An owned OID alone is not sufficient when another same-coin SELL is
        # also resting: it is ambiguous whether the other order is manual or
        # a stale replacement.  Do not choose one and continue mutating.
        if (len(candidate_sells) != 1 or len(matching) != 1 or inventory <= 0
                or Decimal(str(matching[0].get("sz", "0"))) < inventory):
            self.record(lifecycle, reason="account_truth_does_not_match_owned_sell", extra={"state": "RECONCILE_REQUIRED"})
            return None
        return lifecycle
