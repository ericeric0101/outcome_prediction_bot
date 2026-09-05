"""Durable, bounded P3 actual-fill markout collection.

The raw P2 journal is the audit authority, but it is intentionally large. A
5-second collector must never replay that history on every tick: a compact,
indexed quote window and short-lived pending-fill registry preserve exact
horizon evidence without turning the collection loop into an O(database-size)
operation.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import sqlite3
from typing import Any, Mapping, Optional

from bot.outcome_event_bridge import OutcomeFillEvent, OutcomeJournalBridge
from bot.outcome_markout import (
    P3_MARKOUT_HORIZONS_SEC,
    P3_MARKOUT_SCHEMA_VERSION,
    P3_MARKOUT_TOLERANCE_MS,
    OutcomeQuote,
    markouts_for_fill,
)
from monitoring.trade_journal_db import TradeJournalDB


# An account poll may discover a fill after its 5/10-second targets elapsed.
# Retain a small exact-quote window so those targets remain labelable; this is
# deliberately not a replay of the multi-gigabyte raw journal.
P3_QUOTE_RETENTION_MS = 300_000
P3_PENDING_EXPIRY_MS = max(P3_MARKOUT_HORIZONS_SEC) * 1000 + P3_MARKOUT_TOLERANCE_MS
P3_FILL_CONTEXT_SCHEMA_VERSION = 1


def _decimal(value: object) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


class OutcomeP3Pipeline:
    """Records official fills and exact executable markouts in bounded work."""

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id
        self.bridge = OutcomeJournalBridge(journal, run_id)

    def _fill_context(self, fill: OutcomeFillEvent, *, period: str) -> dict[str, Any]:
        quote = self.journal.outcome_p3_quote_before(
            outcome_id=fill.outcome_id, period=period, coin=fill.coin, timestamp_ms=fill.timestamp_ms,
        )
        if quote is None:
            return {
                "fill_context_schema_version": P3_FILL_CONTEXT_SCHEMA_VERSION,
                "fill_context_status": "missing_asof_quote",
                "fill_context_timestamp_ms": None,
                "fill_context_snapshot_event_id": None,
                "time_left_sec": None,
                "spread": None,
                "depth": None,
                "volatility_regime": "unknown",
            }
        snapshot_event_id, timestamp_ms, _bid, _ask, context = quote
        return {
            "fill_context_schema_version": P3_FILL_CONTEXT_SCHEMA_VERSION,
            "fill_context_status": "asof_or_before_fill",
            "fill_context_timestamp_ms": timestamp_ms,
            "fill_context_snapshot_event_id": snapshot_event_id,
            "time_left_sec": context.get("time_left_sec"),
            "spread": context.get("spread"),
            "depth": context.get("depth"),
            "volatility_regime": context.get("volatility_regime", "unknown"),
        }

    def record_actual_fill(
        self,
        fill: OutcomeFillEvent,
        *,
        period: str | None,
        observed_at_ms: int | None = None,
    ) -> bool:
        """Persist exchange evidence once and queue only a still-observable fill."""
        normalized_period = period or "unknown"
        recorded = self.bridge.record_fill(fill, market_key=f"outcome:{fill.outcome_id}", extra_payload={
            "actual_fill": True,
            "period": normalized_period,
            "fill_provenance": "hyperliquid_userFills",
            "research_only": True,
        })
        if not fill.is_maker or normalized_period == "unknown":
            return recorded
        expires_at_ms = fill.timestamp_ms + P3_PENDING_EXPIRY_MS
        if observed_at_ms is not None and int(observed_at_ms) > expires_at_ms:
            return recorded
        self.journal.record_outcome_p3_pending_fill(fill={
            "fill_id": fill.trade_id,
            "outcome_id": fill.outcome_id,
            "period": normalized_period,
            "side_index": fill.side_index,
            "coin": fill.coin,
            "client_order_id": fill.client_order_id,
            "venue_order_id": fill.venue_order_id,
            "side": fill.side,
            "fill_price": str(fill.price),
            "quantity": str(fill.quantity),
            "fee_usdc": str(fill.fee_usdc),
            "fee_token": fill.fee_token,
            "fill_timestamp_ms": fill.timestamp_ms,
            "fill_context": self._fill_context(fill, period=normalized_period),
            "expires_at_ms": expires_at_ms,
        })
        # A poll can arrive after a target quote is already in the bounded
        # window. Process it immediately instead of waiting for another tick.
        self._write_available_markouts(outcome_id=fill.outcome_id, period=normalized_period)
        return recorded

    def record_quote_snapshot(
        self,
        *,
        snapshot_event_id: int,
        outcome_id: int,
        period: str,
        snapshot_timestamp_ms: int,
        quotes: tuple[OutcomeQuote, ...],
        quote_contexts: Mapping[str, Mapping[str, Any]],
    ) -> int:
        """Index one accepted P2 snapshot and resolve only active pending fills."""
        for quote in quotes:
            if quote.timestamp_ms != snapshot_timestamp_ms:
                raise ValueError("P3 quote timestamp must equal its source snapshot timestamp")
            context = dict(quote_contexts.get(quote.coin) or {})
            if "time_left_sec" not in context:
                raise ValueError("P3 quote context requires time_left_sec")
            self.journal.record_outcome_p3_quote(
                snapshot_event_id=snapshot_event_id,
                outcome_id=outcome_id,
                period=period,
                coin=quote.coin,
                snapshot_timestamp_ms=snapshot_timestamp_ms,
                best_bid=quote.bid,
                best_ask=quote.ask,
                context=context,
            )
        written = self._write_available_markouts(outcome_id=outcome_id, period=period)
        self.journal.prune_outcome_p3_window(
            before_quote_timestamp_ms=snapshot_timestamp_ms - P3_QUOTE_RETENTION_MS,
            before_pending_expiry_ms=snapshot_timestamp_ms,
        )
        return written

    def _has_markout(self, trade_id: str, horizon_sec: int) -> bool:
        with sqlite3.connect(self.journal.db_path) as conn:
            row = conn.execute(
                """SELECT 1 FROM order_events WHERE event_type='FILL_MARKOUT'
                   AND json_extract(payload_json, '$.fill_id')=?
                   AND CAST(json_extract(payload_json, '$.horizon_sec') AS INTEGER)=?
                   AND CAST(json_extract(payload_json, '$.p3_markout_schema_version') AS INTEGER)=? LIMIT 1""",
                (trade_id, horizon_sec, P3_MARKOUT_SCHEMA_VERSION),
            ).fetchone()
        return row is not None

    @staticmethod
    def _pending_fill(row: Mapping[str, Any]) -> OutcomeFillEvent:
        return OutcomeFillEvent(
            outcome_id=int(row["outcome_id"]), side_index=int(row["side_index"]), coin=str(row["coin"]),
            client_order_id=row.get("client_order_id"), venue_order_id=row.get("venue_order_id"),
            trade_id=str(row["fill_id"]), side=str(row["side"]), price=Decimal(str(row["fill_price"])),
            quantity=Decimal(str(row["quantity"])), fee_usdc=Decimal(str(row["fee_usdc"])),
            fee_token=str(row["fee_token"]), timestamp_ms=int(row["fill_timestamp_ms"]), is_maker=True, raw={},
        )

    def _write_available_markouts(self, *, outcome_id: int, period: str) -> int:
        written = 0
        for row in self.journal.outcome_p3_pending_fills(outcome_id=outcome_id, period=period):
            fill = self._pending_fill(row)
            context = row.get("fill_context") or {}
            for horizon_sec in P3_MARKOUT_HORIZONS_SEC:
                if self._has_markout(fill.trade_id, horizon_sec):
                    continue
                target_ms = fill.timestamp_ms + horizon_sec * 1000
                quote_row = self.journal.outcome_p3_quote_near(
                    outcome_id=outcome_id,
                    period=period,
                    coin=fill.coin,
                    target_timestamp_ms=target_ms,
                    tolerance_ms=P3_MARKOUT_TOLERANCE_MS,
                )
                if quote_row is None:
                    continue
                snapshot_event_id, quote_timestamp_ms, bid, ask = quote_row
                quote = OutcomeQuote(fill.coin, quote_timestamp_ms, _decimal(bid), _decimal(ask), snapshot_event_id)
                observation = markouts_for_fill(fill, (quote,), horizons_sec=(horizon_sec,))[0]
                if observation.status != "observed" or observation.markout_per_share is None:
                    continue
                bucket = self._bucket(
                    context.get("time_left_sec"), fill.side, _decimal(context.get("spread")),
                    _decimal(context.get("depth")), str(context.get("volatility_regime") or "unknown"),
                )
                self.journal.log_order_event(
                    self.run_id, "FILL_MARKOUT", client_order_id=fill.client_order_id, venue_order_id=fill.venue_order_id,
                    side=fill.side, price=float(observation.executable_mark), qty=float(fill.quantity), status="OBSERVED",
                    instrument_id=fill.coin, commission_usdc=float(fill.fee_usdc),
                    payload={
                        "venue": "hyperliquid_outcome", "actual_fill": True, "fill_id": fill.trade_id,
                        "outcome_id": outcome_id, "period": period, "coin": fill.coin,
                        "horizon_sec": observation.horizon_sec, "markout_mid": float(observation.executable_mark),
                        "signed_markout_ps": float(observation.markout_per_share),
                        "p3_markout_schema_version": P3_MARKOUT_SCHEMA_VERSION,
                        "target_horizon_ms": observation.horizon_sec * 1000,
                        "horizon_tolerance_ms": P3_MARKOUT_TOLERANCE_MS,
                        "quote_timestamp_ms": observation.quote_timestamp_ms,
                        "actual_elapsed_ms": observation.actual_elapsed_ms,
                        "target_lag_ms": observation.target_lag_ms,
                        "snapshot_event_id": observation.snapshot_event_id,
                        "fee_per_share": float(fill.fee_usdc / fill.quantity) if fill.quantity else 0.0,
                        "entry_regime_bucket": bucket,
                        "fill_context_schema_version": context.get("fill_context_schema_version"),
                        "fill_context_status": context.get("fill_context_status"),
                        "fill_context_timestamp_ms": context.get("fill_context_timestamp_ms"),
                        "fill_context_snapshot_event_id": context.get("fill_context_snapshot_event_id"),
                        "time_left_sec": context.get("time_left_sec"), "spread": context.get("spread"),
                        "depth": context.get("depth"), "volatility_regime": context.get("volatility_regime"),
                        "executable_quote": True, "counterfactual": False,
                    },
                )
                written += 1
        return written

    @staticmethod
    def _bucket(
        time_left_sec: object,
        side: str,
        spread: Decimal | None,
        depth: Decimal | None,
        volatility_regime: str,
    ) -> str:
        try:
            seconds = float(time_left_sec)
        except (TypeError, ValueError):
            seconds = -1.0
        time_bucket = "unknown" if seconds < 0 else "lt_300" if seconds < 300 else "300_600" if seconds < 600 else "600_plus"
        spread_bucket = "unknown" if spread is None else "tight" if spread <= Decimal("0.01") else "wide"
        depth_bucket = "unknown" if depth is None else "deep" if depth >= Decimal("100") else "thin"
        return f"{time_bucket}|{side.lower()}|{spread_bucket}|{depth_bucket}|{volatility_regime}"
