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
    # The accepted BUY's immutable submit event time.  Quote-management state
    # may be recovered/adopted after a restart, but that must not restart the
    # stale-order clock.
    submitted_at_ts: float | None = None


class OutcomeEntryLifecycleStore:
    EVENT = "OUTCOME_ENTRY_LIFECYCLE"
    AMBIGUOUS_SUBMIT_EVENT = "OUTCOME_ORDER_AMBIGUOUS_SUBMIT"
    AMBIGUITY_RESOLVED_EVENT = "OUTCOME_ORDER_AMBIGUITY_RESOLVED"
    STALE_CANCEL_DECISION_EVENT = "OUTCOME_STALE_ENTRY_CANCEL_DECISION"
    STALE_CANCEL_CONFIRMED_EVENT = "OUTCOME_STALE_ENTRY_CANCEL_CONFIRMED"
    STALE_CANCEL_FILLED_TERMINAL_EVENT = "OUTCOME_STALE_ENTRY_FILL_TERMINAL_RECONCILED"
    DECISION_EXPIRED_EVENT = "OUTCOME_ENTRY_DECISION_EXPIRED"
    REARMED_EVENT = "OUTCOME_ENTRY_REARMED"

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id

    def record(self, lifecycle: OutcomeEntryLifecycle, *, reason: str,
               extra: dict[str, Any] | None = None, durable: bool = False) -> int | None:
        """Persist ownership state and return its journal event id.

        Initial ownership after a venue-acknowledged BUY is safety-critical:
        without it the bot can see a real open BUY but cannot prove it owns
        that order on the next tick.  Callers use ``durable=True`` for that
        acknowledgement boundary; routine observational state transitions may
        remain normal journal writes.
        """
        payload = {
            "venue": "hyperliquid_outcome", "wallet": lifecycle.wallet,
            "outcome_id": lifecycle.outcome_id, "coin": lifecycle.coin,
            "order_id": lifecycle.order_id, "price": str(lifecycle.price),
            "replacement_count": lifecycle.replacement_count, "state": lifecycle.state,
            "reason": reason, **(extra or {}),
        }
        if durable:
            return self.journal.log_durable_strategy_event(self.run_id, self.EVENT, payload)
        return self.journal.log_strategy_event(self.run_id, self.EVENT, payload)

    def record_stale_cancel_decision(
        self, *, lifecycle: OutcomeEntryLifecycle, decision_at_ms: int | None, order_age_sec: float,
    ) -> int | None:
        """Durably bind a reviewed zero-fill cancellation to its S0 decision."""
        return self.journal.log_durable_strategy_event(self.run_id, self.STALE_CANCEL_DECISION_EVENT, {
            "venue": "hyperliquid_outcome", "wallet": lifecycle.wallet,
            "outcome_id": lifecycle.outcome_id, "coin": lifecycle.coin,
            "order_id": lifecycle.order_id, "decision_observed_at_ms": decision_at_ms,
            # The controller performs the authoritative inventory/open-order
            # read immediately after this durable intent.  Do not claim that
            # read has happened before it actually has.
            "order_age_sec": round(order_age_sec, 3), "fill_status": "zero_official_fill_pending_fresh_account_truth",
            "reason": "stale_zero_fill_60s", "state": "CANCEL_REQUESTED",
        })

    def record_stale_cancel_confirmed(
        self, *, lifecycle: OutcomeEntryLifecycle, decision_at_ms: int | None, order_age_sec: float,
        stale_cancel_decision_event_id: int,
    ) -> int | None:
        """Durably expire exactly one old decision after cancel/account truth."""
        payload = {
            "venue": "hyperliquid_outcome", "wallet": lifecycle.wallet,
            "outcome_id": lifecycle.outcome_id, "coin": lifecycle.coin,
            "order_id": lifecycle.order_id, "decision_observed_at_ms": decision_at_ms,
            "stale_cancel_decision_event_id": int(stale_cancel_decision_event_id),
            "order_age_sec": round(order_age_sec, 3), "fill_status": "zero_official_fill_and_zero_inventory",
            "reason": "stale_zero_fill_60s", "state": "CANCEL_CONFIRMED",
        }
        confirmed = self.journal.log_durable_strategy_event(self.run_id, self.STALE_CANCEL_CONFIRMED_EVENT, payload)
        if confirmed is None:
            return None
        expired = self.journal.log_durable_strategy_event(self.run_id, self.DECISION_EXPIRED_EVENT, {
            **payload, "state": "EXPIRED", "cancel_confirmed_event_id": confirmed,
        })
        return expired

    def reconcile_stale_cancel_fill_when_account_flat(
        self, *, wallet: str, outcome_id: int, fresh_current_outcome_flat: bool,
    ) -> bool:
        """Close a stale-cancel episode that raced with an official BUY fill.

        This is deliberately distinct from ``STALE_CANCEL_CONFIRMED``: that
        event means *zero* fill.  Here the immutable official BUY fill proves
        the cancellation raced with execution, while the caller's current
        account-recovery result proves there is now neither inventory nor an
        open order for this Outcome.  The ordinary ineligible-then-fresh-signal
        re-arm contract remains in force after this terminal reconciliation.
        """
        if not fresh_current_outcome_flat:
            return False
        try:
            with sqlite3.connect(f"file:{self.journal.db_path}?mode=ro", uri=True) as conn:
                decision = conn.execute(
                    """SELECT id, payload_json FROM strategy_events WHERE event_type=?
                       AND json_extract(payload_json, '$.wallet')=?
                       AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                       ORDER BY id DESC LIMIT 1""",
                    (self.STALE_CANCEL_DECISION_EVENT, wallet, int(outcome_id)),
                ).fetchone()
                if decision is None:
                    return False
                decision_id, raw_payload = int(decision[0]), str(decision[1] or "{}")
                already_terminal = conn.execute(
                    """SELECT 1 FROM strategy_events WHERE event_type IN (?, ?) AND id>?
                       AND json_extract(payload_json, '$.wallet')=?
                       AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                       AND CAST(json_extract(payload_json, '$.stale_cancel_decision_event_id') AS INTEGER)=?
                       LIMIT 1""",
                    (self.STALE_CANCEL_CONFIRMED_EVENT, self.STALE_CANCEL_FILLED_TERMINAL_EVENT,
                     decision_id, wallet, int(outcome_id), decision_id),
                ).fetchone()
            if already_terminal is not None:
                return False
            payload = json.loads(raw_payload)
            if not isinstance(payload, dict):
                return False
            coin, order_id = str(payload["coin"]), str(payload["order_id"])
            official_fill = self.official_buy_fill(
                outcome_id=int(outcome_id), coin=coin, order_id=order_id,
            )
            if official_fill is None:
                return False
            terminal_payload = {
                "venue": "hyperliquid_outcome", "wallet": wallet,
                "outcome_id": int(outcome_id), "coin": coin, "order_id": order_id,
                "stale_cancel_decision_event_id": decision_id,
                "fill_status": "official_buy_fill_then_fresh_account_flat",
                "official_fill_trade_id": official_fill.get("trade_id"),
                "official_fill_qty": official_fill.get("qty"),
                "account_truth": "fresh_current_outcome_zero_inventory_and_zero_open_orders",
                "reason": "stale_cancel_raced_with_fill_terminally_reconciled",
                "state": "FILLED_TERMINAL_RECONCILED",
            }
            terminal_id = self.journal.log_durable_strategy_event(
                self.run_id, self.STALE_CANCEL_FILLED_TERMINAL_EVENT, terminal_payload,
            )
            if terminal_id is None:
                return False
            expired = self.journal.log_durable_strategy_event(self.run_id, self.DECISION_EXPIRED_EVENT, {
                **terminal_payload, "state": "EXPIRED",
                "terminal_reconciliation_event_id": int(terminal_id),
            })
            return expired is not None
        except (KeyError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError):
            # Missing journal evidence must retain the hard admission fence.
            return False

    def stale_decision_rearm_barrier(
        self, *, wallet: str, outcome_id: int, decision_at_ms: int | None,
        signal_ineligible: bool,
    ) -> tuple[bool, str, int | None]:
        """Require an ineligible observation before an expired decision can reform.

        This state is journal-derived so restart cannot turn a just-cancelled
        still-eligible signal into an immediate replacement order.
        """
        try:
            with sqlite3.connect(f"file:{self.journal.db_path}?mode=ro", uri=True) as conn:
                stale_decision = conn.execute(
                    """SELECT id FROM strategy_events WHERE event_type=?
                       AND json_extract(payload_json, '$.wallet')=?
                       AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                       ORDER BY id DESC LIMIT 1""",
                    (self.STALE_CANCEL_DECISION_EVENT, wallet, int(outcome_id)),
                ).fetchone()
                if stale_decision is not None:
                    stale_decision_id = int(stale_decision[0])
                    confirmed = conn.execute(
                        """SELECT id FROM strategy_events WHERE event_type IN (?, ?) AND id>?
                           AND json_extract(payload_json, '$.wallet')=?
                           AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                           AND CAST(json_extract(payload_json, '$.stale_cancel_decision_event_id') AS INTEGER)=?
                           ORDER BY id DESC LIMIT 1""",
                        (self.STALE_CANCEL_CONFIRMED_EVENT, self.STALE_CANCEL_FILLED_TERMINAL_EVENT,
                         stale_decision_id, wallet, int(outcome_id), stale_decision_id),
                    ).fetchone()
                    if confirmed is None:
                        return False, "stale_cancel_pending_terminal_reconciliation", None
                    confirmed_id = int(confirmed[0])
                    expired = conn.execute(
                        """SELECT id,ts FROM strategy_events
                           WHERE event_type=? AND json_extract(payload_json, '$.wallet')=?
                             AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                             AND (
                                 CAST(json_extract(payload_json, '$.cancel_confirmed_event_id') AS INTEGER)=?
                                 OR CAST(json_extract(payload_json, '$.terminal_reconciliation_event_id') AS INTEGER)=?
                             )
                           ORDER BY id DESC LIMIT 1""",
                        (self.DECISION_EXPIRED_EVENT, wallet, int(outcome_id), confirmed_id, confirmed_id),
                    ).fetchone()
                    if expired is None:
                        return False, "stale_cancel_confirmed_but_decision_expiry_missing", None
                    expired_id, expired_ts = int(expired[0]), str(expired[1])
                    rearmed = conn.execute(
                        """SELECT id,ts FROM strategy_events WHERE event_type=? AND id>?
                           AND json_extract(payload_json, '$.wallet')=?
                             AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                             AND CAST(json_extract(payload_json, '$.expired_decision_event_id') AS INTEGER)=?
                           ORDER BY id DESC LIMIT 1""",
                        (self.REARMED_EVENT, expired_id, wallet, int(outcome_id), expired_id),
                    ).fetchone()
                else:
                    return True, "no_stale_decision_expired", None
            if rearmed is None and signal_ineligible:
                rearm_at_ms = int(time.time() * 1000)
                event_id = self.journal.log_durable_strategy_event(self.run_id, self.REARMED_EVENT, {
                    "venue": "hyperliquid_outcome", "wallet": wallet, "outcome_id": int(outcome_id),
                    "expired_decision_event_id": expired_id, "expired_at": expired_ts,
                    "rearm_state": "ready_after_ineligible_signal", "state": "REARMED",
                })
                return False, "stale_decision_rearmed_waiting_for_fresh_eligible_transition", rearm_at_ms if event_id is not None else None
            if rearmed is None:
                return False, "stale_decision_requires_ineligible_then_fresh_eligible_signal", None
            rearm_at_ms = int(datetime.fromisoformat(str(rearmed[1])).timestamp() * 1000)
            if decision_at_ms is None or decision_at_ms <= rearm_at_ms:
                return False, "fresh_decision_timestamp_required_after_stale_rearm", rearm_at_ms
            return True, "fresh_decision_after_stale_rearm", rearm_at_ms
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            return False, "stale_rearm_journal_unreadable", None

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
                submitted_at_ts=self.submit_timestamp(order_id=str(payload["order_id"]), coin=str(payload["coin"])),
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

    def latest_cancel_intent_reason(
        self, *, wallet: str, outcome_id: int, coin: str, order_id: str,
    ) -> str | None:
        """Return the durable reason for a previously submitted owned cancel.

        This is deliberately narrower than an absent-order lookup.  A later
        flat account snapshot may close an entry lifecycle only when this
        runtime had already durably recorded that it was cancelling this exact
        owned BUY.  Without that intent, an absent order can still be an
        unobserved fill or an account-view race and must remain fail-closed.
        """
        try:
            with sqlite3.connect(f"file:{self.journal.db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """SELECT payload_json FROM strategy_events
                       WHERE event_type=?
                         AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                         AND json_extract(payload_json, '$.wallet')=?
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=?
                         AND json_extract(payload_json, '$.order_id')=?
                         AND json_extract(payload_json, '$.state')='CANCEL_SUBMITTED'
                       ORDER BY id DESC LIMIT 1""",
                    (self.EVENT, wallet, int(outcome_id), coin, str(order_id)),
                ).fetchone()
            if row is None:
                return None
            payload = json.loads(row[0] or "{}")
            reason = payload.get("reason") if isinstance(payload, dict) else None
            return str(reason) if reason else None
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            # A journal-read failure must never authorize a new entry.
            return None

    def pending_ambiguous_submit(self, *, wallet: str, outcome_id: int) -> dict[str, Any] | None:
        """Return an unresolved buy submission whose venue ACK was lost.

        This is intentionally market-wide rather than signal-side specific:
        after a timed-out BUY, a new signal for the opposite coin must not
        bypass the reconciliation fence while the original order may still be
        propagating through the venue's account views.
        """
        try:
            with sqlite3.connect(f"file:{self.journal.db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """SELECT id, payload_json FROM strategy_events
                       WHERE event_type=?
                         AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                         AND json_extract(payload_json, '$.wallet')=?
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                       ORDER BY id DESC LIMIT 1""",
                    (self.AMBIGUOUS_SUBMIT_EVENT, wallet, int(outcome_id)),
                ).fetchone()
                if row is None:
                    return None
                event_id, raw = int(row[0]), row[1]
                payload = json.loads(raw or "{}")
                if not isinstance(payload, dict):
                    return None
                resolved = conn.execute(
                    """SELECT 1 FROM strategy_events
                       WHERE event_type=? AND id > ?
                         AND json_extract(payload_json, '$.intent_id')=?
                       LIMIT 1""",
                    (self.AMBIGUITY_RESOLVED_EVENT, event_id, str(payload.get("intent_id") or "")),
                ).fetchone()
            return None if resolved else dict(payload)
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            # Journal uncertainty is never an authority to submit a new risk.
            return {"reason": "ambiguous_submit_journal_unreadable"}

    def record_ambiguous_submit(
        self, *, wallet: str, outcome_id: int, coin: str, intent_id: str,
        intent_event_id: int, sidecar_request_id: str, command: str, detail: str,
    ) -> int | None:
        return self.journal.log_durable_strategy_event(self.run_id, self.AMBIGUOUS_SUBMIT_EVENT, {
            "venue": "hyperliquid_outcome", "wallet": wallet,
            "outcome_id": int(outcome_id), "coin": coin, "side": "BUY",
            "intent_id": intent_id, "intent_event_id": int(intent_event_id),
            "sidecar_request_id": sidecar_request_id, "command": command,
            "detail": detail, "state": "RECONCILIATION_REQUIRED",
        })

    def resolve_ambiguous_submit(self, *, intent_id: str, order_id: str, reason: str) -> None:
        self.journal.log_strategy_event(self.run_id, self.AMBIGUITY_RESOLVED_EVENT, {
            "intent_id": intent_id, "order_id": order_id, "reason": reason,
            "state": "RESOLVED_BY_ACCOUNT_TRUTH",
        })

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

    def submit_timestamp(self, *, order_id: str, coin: str) -> float | None:
        """Return immutable venue-submit audit time for an owned BUY."""
        try:
            with sqlite3.connect(f"file:{self.journal.db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """SELECT ts FROM order_events
                       WHERE event_type='ORDER_SUBMIT' AND side='BUY'
                         AND venue_order_id=? AND instrument_id=?
                       ORDER BY id ASC LIMIT 1""", (str(order_id), coin),
                ).fetchone()
            return datetime.fromisoformat(str(row[0])).timestamp() if row is not None else None
        except (sqlite3.Error, TypeError, ValueError):
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
        intent_id = ""
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
                intent_id = str(intent.get("intent_id") or "")
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
            # A post-only submit is allowed to use a fresh book after the
            # strategy decision.  ``entry_bid_at_decision`` is therefore not
            # necessarily the price which actually reached the venue.  The
            # immutable ORDER_SUBMIT audit records that actual submitted
            # limit; use it when present, or retain the pre-submit equality
            # requirement for the crash-window intent-only fallback.
            submitted_price = audit.get("entry_submit_bid", audit["entry_bid_at_decision"])
            price = Decimal(str(submitted_price))
            order_price = Decimal(str(order.get("limitPx", order.get("px", "0"))))
            if not Decimal("0") < price < Decimal("1") or order_price != price:
                return None
        except (KeyError, TypeError, ValueError, ArithmeticError, sqlite3.Error, json.JSONDecodeError):
            return None
        lifecycle = OutcomeEntryLifecycle(
            wallet, outcome_id, coin, order_id, price, 0, "BUY_RESTING", time.time(),
            self.submit_timestamp(order_id=order_id, coin=coin),
        )
        self.record(lifecycle, reason="adopted_exact_audited_s0_submit_or_pre_submit_intent_after_restart")
        if intent_id:
            self.resolve_ambiguous_submit(
                intent_id=intent_id, order_id=order_id,
                reason="matching_open_buy_adopted_from_durable_intent",
            )
        return lifecycle
