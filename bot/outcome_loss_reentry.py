"""Durable post-loss re-entry guard based only on official fill evidence."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from monitoring.trade_journal_db import TradeJournalDB


@dataclass(frozen=True)
class OutcomeLossReentryDecision:
    allowed: bool
    reason: str
    is_limited_reentry: bool = False
    prior_exit_price: float | None = None
    cooldown_remaining_sec: float | None = None


class OutcomeLossReentryGate:
    EVENT = "OUTCOME_LOSS_EXIT_CONFIRMED"
    REENTRY_EVENT = "OUTCOME_LOSS_REENTRY_SUBMITTED"
    # Fixed product policy, intentionally not an operator-facing env knob.
    # This gives the market time to establish a fresh thesis after a passive
    # loss exit, while not sacrificing an entire 1d contract to one loss.
    COOLDOWN_SEC = 15 * 60

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id

    @staticmethod
    def _legacy_event_net_pnl(
        conn: sqlite3.Connection,
        *,
        outcome_id: int,
        event_ts: str,
        payload: dict[str, object],
    ) -> Decimal | None:
        """Recompute a v1 event only when its immutable lot is complete.

        ``None`` means the legacy record cannot be safely disproved and must
        retain fail-closed behaviour.  A number is authoritative enough to
        reject only a demonstrably non-losing v1 event.
        """
        coin = str(payload.get("coin") or "")
        order_id = str(payload.get("order_id") or "")
        if not coin or not order_id:
            return None
        sell = conn.execute(
            """SELECT id, price, qty, commission_usdc FROM order_events
               WHERE event_type='ORDER_FILLED' AND instrument_id=? AND venue_order_id=? AND side='SELL'
                 AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                 AND json_extract(payload_json, '$.actual_fill')=1
               ORDER BY id DESC LIMIT 1""",
            (coin, order_id),
        ).fetchone()
        if sell is None:
            return None
        entry = conn.execute(
            """SELECT payload_json FROM strategy_events
               WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED'
                 AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                 AND json_extract(payload_json, '$.coin')=? AND ts<=?
               ORDER BY id DESC LIMIT 1""",
            (outcome_id, coin, event_ts),
        ).fetchone()
        try:
            entry_payload = json.loads(entry[0] or "{}") if entry else {}
            entry_order_id = str(entry_payload.get("order_id") or "") if isinstance(entry_payload, dict) else ""
            if not entry_order_id:
                return None
            buys = conn.execute(
                """SELECT price, qty, commission_usdc FROM order_events
                   WHERE event_type='ORDER_FILLED' AND side='BUY' AND instrument_id=? AND venue_order_id=?
                     AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                     AND json_extract(payload_json, '$.actual_fill')=1 AND id<?
                   ORDER BY id ASC""",
                (coin, entry_order_id, int(sell[0])),
            ).fetchall()
            if not buys:
                return None
            sell_price, sell_qty, sell_fee = (Decimal(str(sell[1])), Decimal(str(sell[2])), Decimal(str(sell[3] or 0)))
            buy_qty = sum((Decimal(str(item[1])) for item in buys), Decimal("0"))
            if sell_qty <= 0 or buy_qty != sell_qty:
                return None
            buy_cost = sum(
                (Decimal(str(item[0])) * Decimal(str(item[1])) + Decimal(str(item[2] or 0)) for item in buys),
                Decimal("0"),
            )
            return sell_price * sell_qty - sell_fee - buy_cost
        except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _latest_verified_loss_event(
        self, conn: sqlite3.Connection, *, outcome_id: int
    ) -> tuple[int, str, str] | None:
        """Return the latest loss event, skipping proven-profitable v1 rows."""
        rows = conn.execute(
            """SELECT id, ts, payload_json FROM strategy_events WHERE event_type=?
               AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
               ORDER BY id DESC""",
            (self.EVENT, outcome_id),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row[2] or "{}")
                if not isinstance(payload, dict):
                    return row
                if "net_realized_pnl_usdc" in payload:
                    # v2 events are generated only after the same complete-lot
                    # check.  Treat malformed numeric evidence as fail-closed.
                    if Decimal(str(payload["net_realized_pnl_usdc"])) >= 0:
                        continue
                    return row
                legacy_pnl = self._legacy_event_net_pnl(
                    conn, outcome_id=outcome_id, event_ts=str(row[1]), payload=payload,
                )
                if legacy_pnl is not None and legacy_pnl >= 0:
                    continue
                return row
            except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
                return row
        return None

    def record_confirmed_loss_exit(self, *, outcome_id: int, period: str, coin: str, order_id: str) -> bool:
        """Record only a verified, fee-inclusive losing round trip.

        A reversal observation is a risk signal, not realized-loss evidence.
        In particular, a profit target may fill after the lifecycle was briefly
        tagged ``REVERSAL_CONFIRMED``.  Recording that closure as a loss would
        incorrectly consume the market's limited re-entry token.
        """
        try:
            with sqlite3.connect(self.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id, ts, payload_json, price, qty, commission_usdc FROM order_events
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
            if not isinstance(payload, dict):
                return False
            # Resolve the exact bot-owned entry order that preceded this SELL.
            # A bare coin-level lookup could join a close to an older same-side
            # round trip after restart, which is no safer than the old
            # reversal-state inference.
            with sqlite3.connect(self.journal.db_path) as conn:
                entry_row = conn.execute(
                    """SELECT payload_json FROM strategy_events
                       WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED'
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=?
                         AND ts<=?
                       ORDER BY id DESC LIMIT 1""",
                    (outcome_id, coin, row[1]),
                ).fetchone()
            entry_payload = json.loads(entry_row[0] or "{}") if entry_row else {}
            entry_order_id = str(entry_payload.get("order_id") or "") if isinstance(entry_payload, dict) else ""
            if not entry_order_id:
                return False
            with sqlite3.connect(self.journal.db_path) as conn:
                buy_rows = conn.execute(
                    """SELECT price, qty, commission_usdc FROM order_events
                       WHERE event_type='ORDER_FILLED' AND side='BUY'
                         AND instrument_id=? AND venue_order_id=?
                         AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
                         AND json_extract(payload_json, '$.actual_fill')=1
                         AND id<?
                       ORDER BY id ASC""",
                    (coin, entry_order_id, int(row[0])),
                ).fetchall()
            if not buy_rows:
                return False
            try:
                sell_price = Decimal(str(row[3]))
                sell_qty = Decimal(str(row[4]))
                sell_fee = Decimal(str(row[5] or 0))
                buy_qty = sum((Decimal(str(item[1])) for item in buy_rows), Decimal("0"))
                buy_cost = sum(
                    (Decimal(str(item[0])) * Decimal(str(item[1])) + Decimal(str(item[2] or 0)) for item in buy_rows),
                    Decimal("0"),
                )
                net_pnl = sell_price * sell_qty - sell_fee - buy_cost
            except (InvalidOperation, TypeError, ValueError):
                return False
            # Only a fully matched official lot can spend the loss re-entry
            # budget.  Partial history or a non-loss close remains neutral.
            if sell_qty <= 0 or buy_qty != sell_qty or net_pnl >= 0:
                return False
            self.journal.log_strategy_event(self.run_id, self.EVENT, {
                "venue": "hyperliquid_outcome", "outcome_id": outcome_id,
                "period": period, "coin": coin, "order_id": order_id,
                "official_fill_order_event_id": int(row[0]), "official_fill_ts": row[1],
                "fill_provenance": payload.get("fill_provenance"),
                "loss_exit_price": float(sell_price), "entry_order_id": entry_order_id,
                "entry_cost_usdc": str(buy_cost), "net_exit_proceeds_usdc": str(sell_price * sell_qty - sell_fee),
                "net_realized_pnl_usdc": str(net_pnl),
                "reentry_policy": "one_limited_reentry_after_verified_fee_inclusive_loss_v2",
            })
            return True
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError, InvalidOperation):
            return False

    def evaluate(
        self,
        *,
        outcome_id: int,
        coin: str,
        candidate_bid: float,
        now: datetime | None = None,
    ) -> OutcomeLossReentryDecision:
        """Allow one post-loss entry only after a fresh, recovered setup.

        The caller has already established a current directional signal and a
        viable fee-after target.  Here we enforce the durable constraints:
        one re-entry per market, a fixed cooldown, and reclaim of the actual
        loss-exit price when returning to the same Outcome side.
        """
        try:
            with sqlite3.connect(f"file:{Path(self.journal.db_path).resolve()}?mode=ro", uri=True) as conn:
                row = self._latest_verified_loss_event(conn, outcome_id=outcome_id)
                consumed = conn.execute(
                    """SELECT 1 FROM strategy_events WHERE event_type=?
                       AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                       AND CAST(json_extract(payload_json, '$.loss_exit_event_id') AS INTEGER)=? LIMIT 1""",
                    (self.REENTRY_EVENT, outcome_id, int(row[0])) if row is not None else (self.REENTRY_EVENT, outcome_id, -1),
                ).fetchone()
            if row is None:
                return OutcomeLossReentryDecision(True, "no_confirmed_loss_exit")
            if consumed is not None:
                return OutcomeLossReentryDecision(False, "loss_reentry_already_used_until_market_rollover")
            payload = json.loads(row[2] or "{}")
            loss_coin = str(payload.get("coin") or "")
            exit_price = payload.get("loss_exit_price")
            if exit_price is None:
                # S2-3 events created before this policy did not duplicate
                # the immutable official fill price.  Recover it from that
                # referenced order event rather than lock a live market just
                # because the audit schema evolved.
                with sqlite3.connect(f"file:{Path(self.journal.db_path).resolve()}?mode=ro", uri=True) as conn:
                    legacy_fill = conn.execute(
                        """SELECT price FROM order_events
                           WHERE event_type='ORDER_FILLED' AND venue_order_id=?
                             AND instrument_id=? AND side='SELL'
                           ORDER BY id DESC LIMIT 1""",
                        (str(payload.get("order_id") or ""), loss_coin),
                    ).fetchone()
                exit_price = legacy_fill[0] if legacy_fill is not None else None
            if exit_price is None:
                return OutcomeLossReentryDecision(False, "loss_reentry_missing_exit_price")
            exit_price_float = float(exit_price)
            loss_at = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
            current = now or datetime.now(timezone.utc)
            if loss_at.tzinfo is None:
                loss_at = loss_at.replace(tzinfo=timezone.utc)
            elapsed_sec = max(0.0, (current - loss_at).total_seconds())
            if elapsed_sec < self.COOLDOWN_SEC:
                return OutcomeLossReentryDecision(
                    False,
                    "loss_reentry_cooldown_active",
                    is_limited_reentry=True,
                    prior_exit_price=exit_price_float,
                    cooldown_remaining_sec=self.COOLDOWN_SEC - elapsed_sec,
                )
            if coin == loss_coin and candidate_bid < exit_price_float:
                return OutcomeLossReentryDecision(
                    False,
                    "loss_reentry_same_side_exit_price_not_reclaimed",
                    is_limited_reentry=True,
                    prior_exit_price=exit_price_float,
                )
            return OutcomeLossReentryDecision(
                True,
                "loss_reentry_limited_recovery_authorized",
                is_limited_reentry=True,
                prior_exit_price=exit_price_float,
            )
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            return OutcomeLossReentryDecision(False, "loss_reentry_journal_unavailable")

    def record_reentry_submitted(
        self,
        *,
        outcome_id: int,
        period: str,
        coin: str,
        order_id: str,
        bid: float,
        target_price: float,
        entry_reason: str,
    ) -> bool:
        """Durably consume the sole re-entry token after exchange acceptance."""
        try:
            with sqlite3.connect(self.journal.db_path) as conn:
                loss = self._latest_verified_loss_event(conn, outcome_id=outcome_id)
                consumed = conn.execute(
                    """SELECT 1 FROM strategy_events WHERE event_type=?
                       AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                       AND CAST(json_extract(payload_json, '$.loss_exit_event_id') AS INTEGER)=? LIMIT 1""",
                    (self.REENTRY_EVENT, outcome_id, int(loss[0])) if loss is not None else (self.REENTRY_EVENT, outcome_id, -1),
                ).fetchone()
            if loss is None or consumed is not None:
                return False
            self.journal.log_strategy_event(self.run_id, self.REENTRY_EVENT, {
                "venue": "hyperliquid_outcome", "outcome_id": outcome_id,
                "period": period, "coin": coin, "order_id": order_id,
                "loss_exit_event_id": int(loss[0]),
                "entry_bid": bid, "target_price_preview": target_price,
                "entry_reason": entry_reason,
                "reentry_policy": "one_limited_reentry_after_cooldown_and_reclaim_v1",
            })
            return True
        except (sqlite3.Error, ValueError, TypeError):
            return False
