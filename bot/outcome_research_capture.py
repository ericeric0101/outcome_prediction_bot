"""Durable P2/P3 research capture owned by the live Outcome launcher.

It is read-only against the venue: the launcher remains the only wallet order
writer, while this component records the exact books/fills it observes.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_event_bridge import OutcomeFillEvent
from bot.outcome_p2_quality import P2_SCHEMA_VERSION, build_p2_capture_quality
from bot.outcome_p3_pipeline import OutcomeP3Pipeline
from bot.outcome_parity import OutcomeParityAnalyzer
from monitoring.trade_journal_db import TradeJournalDB


@dataclass(frozen=True)
class OutcomeResearchCaptureResult:
    captured: bool
    accepted: bool | None = None
    p3_markouts_written: int = 0


class OutcomeResearchCapture:
    """Rate-limited P2 snapshot and actual-fill markout recorder for live mode."""

    def __init__(self, *, client: Any, wallet_address: str, journal: TradeJournalDB, run_id: str | None = None,
                 interval_sec: float | None = None, heartbeat_sec: float | None = None, gap_alert_sec: float | None = None) -> None:
        self.client, self.wallet_address, self.journal = client, wallet_address, journal
        self.run_id = run_id or f"outcome-research-{uuid.uuid4().hex[:10]}"
        self.interval_ms = int(1000 * (interval_sec if interval_sec is not None else float(os.getenv("OUTCOME_RESEARCH_CAPTURE_INTERVAL_SEC", "5"))))
        self.heartbeat_ms = int(1000 * (heartbeat_sec if heartbeat_sec is not None else float(os.getenv("OUTCOME_RESEARCH_HEARTBEAT_SEC", "60"))))
        self.gap_alert_ms = int(1000 * (gap_alert_sec if gap_alert_sec is not None else float(os.getenv("OUTCOME_RESEARCH_GAP_ALERT_SEC", "30"))))
        if self.interval_ms < 1000 or self.heartbeat_ms < self.interval_ms or self.gap_alert_ms < self.interval_ms:
            raise ValueError("Outcome research capture interval/heartbeat/gap values are invalid")
        self._last_capture_ms = 0
        self._last_heartbeat_ms = 0
        self._p3 = OutcomeP3Pipeline(journal, self.run_id)
        self._parity = OutcomeParityAnalyzer()

    def _heartbeat(self, *, now_ms: int, market: OutcomeMarketSpec, accepted: bool, p3_written: int) -> None:
        if now_ms - self._last_heartbeat_ms < self.heartbeat_ms:
            return
        self.journal.log_strategy_event(self.run_id, "OUTCOME_RESEARCH_CAPTURE_HEARTBEAT", {
            "venue": "hyperliquid_outcome", "read_only": True, "market_id": market.outcome_id,
            "period": market.period, "last_capture_timestamp_ms": now_ms,
            "p2_capture_accepted": accepted, "p3_markouts_written": p3_written,
            "interval_ms": self.interval_ms, "gap_alert_ms": self.gap_alert_ms,
        })
        self._last_heartbeat_ms = now_ms

    def capture_if_due(self, *, market: OutcomeMarketSpec, yes_book: Mapping[str, Any], no_book: Mapping[str, Any],
                       yes_local_received_at_ms: int, no_local_received_at_ms: int, capture_complete_at_ms: int) -> OutcomeResearchCaptureResult:
        if self._last_capture_ms and capture_complete_at_ms - self._last_capture_ms < self.interval_ms:
            return OutcomeResearchCaptureResult(False)
        if self._last_capture_ms and capture_complete_at_ms - self._last_capture_ms > self.gap_alert_ms:
            self.journal.log_strategy_event(self.run_id, "OUTCOME_RESEARCH_CAPTURE_GAP_ALERT", {
                "venue": "hyperliquid_outcome", "read_only": True, "market_id": market.outcome_id,
                "period": market.period, "previous_capture_timestamp_ms": self._last_capture_ms,
                "current_capture_timestamp_ms": capture_complete_at_ms,
                "gap_ms": capture_complete_at_ms - self._last_capture_ms, "threshold_ms": self.gap_alert_ms,
                "action": "gap_recorded_no_interpolation",
            })
        quality = build_p2_capture_quality(
            yes_book=yes_book, no_book=no_book, yes_local_received_at_ms=yes_local_received_at_ms,
            no_local_received_at_ms=no_local_received_at_ms, capture_complete_at_ms=capture_complete_at_ms,
        )
        try:
            user_fees = self.client.get_user_fees_sync(self.wallet_address)
            maker_fee = Decimal(str(user_fees["userSpotAddRate"]))
            taker_fee = Decimal(str(user_fees["userSpotCrossRate"]))
            fee_evidence: dict[str, Any] = {
                "source": "hyperliquid_userFees", "open_fee_rate": "0",
                "user_spot_cross_rate": str(taker_fee), "user_spot_add_rate": str(maker_fee),
                "status": "observed_settlement_conversion_unverified",
            }
        except Exception as exc:
            maker_fee = taker_fee = None
            fee_evidence = {"source": "hyperliquid_userFees", "status": "unavailable", "error_type": type(exc).__name__}
        parity = OutcomeParityAnalyzer(maker_close_fee_rate=maker_fee, taker_close_fee_rate=taker_fee).analyze(market, yes_book, no_book)
        self.journal.log_strategy_event(self.run_id, "OUTCOME_P2_PARITY_SNAPSHOT", {
            "venue": "hyperliquid_outcome", "period": market.period, "p2_schema_version": P2_SCHEMA_VERSION,
            "snapshot_timestamp_ms": capture_complete_at_ms, "outcome_id": market.outcome_id,
            "yes_coin": market.yes_coin, "no_coin": market.no_coin, "yes_l2": dict(yes_book), "no_l2": dict(no_book),
            "capture_quality": quality, "fee_evidence": fee_evidence, **parity.as_dict(),
        })
        for raw in self.client.get_user_fills_sync(self.wallet_address):
            try:
                fill = OutcomeFillEvent.from_user_fill(raw)
            except ValueError:
                continue
            observed_period = market.period if fill.outcome_id == market.outcome_id else "unknown"
            self._p3.record_actual_fill(fill, period=observed_period)
        quotes = self._p3.quotes_from_journal(outcome_id=market.outcome_id, period=market.period)
        yes_levels = yes_book.get("levels", [[], []])
        bids = yes_levels[0] if isinstance(yes_levels, list) and yes_levels else []
        asks = yes_levels[1] if isinstance(yes_levels, list) and len(yes_levels) > 1 else []
        bid = Decimal(str(bids[0]["px"])) if bids else None
        ask = Decimal(str(asks[0]["px"])) if asks else None
        p3_written = self._p3.observe_quotes(
            outcome_id=market.outcome_id, period=market.period, quotes=quotes,
            time_left_sec=market.time_to_expiry_sec(), spread=(ask - bid if ask is not None and bid is not None else None),
            depth=Decimal(str(bids[0]["sz"])) if bids else None, volatility_regime="unknown",
        )
        self._last_capture_ms = capture_complete_at_ms
        accepted = quality.get("status") == "accepted"
        self._heartbeat(now_ms=capture_complete_at_ms, market=market, accepted=accepted, p3_written=p3_written)
        return OutcomeResearchCaptureResult(True, accepted, p3_written)
