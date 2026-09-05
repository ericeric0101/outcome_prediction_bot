"""Durable P2/P3 research capture owned by the live Outcome launcher.

It is read-only against the venue: the launcher remains the only wallet order
writer, while this component records the exact books/fills it observes.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_event_bridge import OutcomeFillEvent
from bot.outcome_markout import OutcomeQuote
from bot.outcome_p2_quality import P2_SCHEMA_VERSION, build_p2_capture_quality
from bot.outcome_market_authority import publish_outcome_market_authority
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
                 interval_sec: float | None = None, heartbeat_sec: float | None = None, gap_alert_sec: float | None = None,
                 account_sync_interval_sec: float = 15.0, fee_refresh_sec: float = 300.0,
                 account_sync_async: bool = True) -> None:
        self.client, self.wallet_address, self.journal = client, wallet_address, journal
        self.run_id = run_id or f"outcome-research-{uuid.uuid4().hex[:10]}"
        self.interval_ms = int(1000 * (interval_sec if interval_sec is not None else float(os.getenv("OUTCOME_RESEARCH_CAPTURE_INTERVAL_SEC", "5"))))
        self.heartbeat_ms = int(1000 * (heartbeat_sec if heartbeat_sec is not None else float(os.getenv("OUTCOME_RESEARCH_HEARTBEAT_SEC", "60"))))
        self.gap_alert_ms = int(1000 * (gap_alert_sec if gap_alert_sec is not None else float(os.getenv("OUTCOME_RESEARCH_GAP_ALERT_SEC", "30"))))
        self.account_sync_interval_ms = int(account_sync_interval_sec * 1000)
        self.fee_refresh_ms = int(fee_refresh_sec * 1000)
        self.account_sync_async = bool(account_sync_async)
        if (self.interval_ms < 1000 or self.heartbeat_ms < self.interval_ms or self.gap_alert_ms < self.interval_ms
                or self.account_sync_interval_ms < self.interval_ms or self.fee_refresh_ms < self.interval_ms):
            raise ValueError("Outcome research capture interval/heartbeat/gap values are invalid")
        self._last_capture_ms = 0
        self._last_heartbeat_ms = 0
        self._p3 = OutcomeP3Pipeline(journal, self.run_id)
        self._parity = OutcomeParityAnalyzer()
        # Account endpoints are materially slower than public books.  They
        # therefore run on a bounded, read-only background path: P2 snapshots
        # never inherit a ``userFills`` delay or pretend cached fees are new.
        self._account_lock = threading.Lock()
        self._account_sync_in_flight = False
        self._account_sync_started_ms: int | None = None
        self._last_fill_sync_ms = 0
        self._last_fee_sync_ms = 0
        self._maker_fee: Decimal | None = None
        self._taker_fee: Decimal | None = None
        self._fee_evidence: dict[str, Any] = {
            "source": "hyperliquid_userFees", "status": "pending_first_observation",
        }

    def _account_snapshot(self, *, now_ms: int) -> tuple[Decimal | None, Decimal | None, dict[str, Any]]:
        with self._account_lock:
            evidence = dict(self._fee_evidence)
            observed = self._last_fee_sync_ms or None
            evidence.update({
                "observed_at_ms": observed,
                "age_ms": (now_ms - observed) if observed is not None else None,
                "account_sync_in_flight": self._account_sync_in_flight,
                "last_fill_sync_ms": self._last_fill_sync_ms or None,
            })
            return self._maker_fee, self._taker_fee, evidence

    def _sync_account_state(self, *, market: OutcomeMarketSpec) -> None:
        """Refresh fees/fills outside the market-data loop; never submits orders."""
        now_ms = int(time.time() * 1000)
        fee_result: tuple[Decimal, Decimal] | None = None
        fee_error: str | None = None
        fill_error: str | None = None
        try:
            with self._account_lock:
                refresh_fee = not self._last_fee_sync_ms or now_ms - self._last_fee_sync_ms >= self.fee_refresh_ms
            if refresh_fee:
                user_fees = self.client.get_user_fees_sync(self.wallet_address)
                fee_result = (
                    Decimal(str(user_fees["userSpotAddRate"])),
                    Decimal(str(user_fees["userSpotCrossRate"])),
                )
        except Exception as exc:
            fee_error = type(exc).__name__

        try:
            fills = self.client.get_user_fills_sync(self.wallet_address)
            for raw in fills:
                try:
                    fill = OutcomeFillEvent.from_user_fill(raw)
                except ValueError:
                    continue
                observed_period = market.period if fill.outcome_id == market.outcome_id else "unknown"
                self._p3.record_actual_fill(fill, period=observed_period, observed_at_ms=now_ms)
        except Exception as exc:
            fill_error = type(exc).__name__

        completed_ms = int(time.time() * 1000)
        with self._account_lock:
            if fee_result is not None:
                self._maker_fee, self._taker_fee = fee_result
                self._last_fee_sync_ms = completed_ms
                self._fee_evidence = {
                    "source": "hyperliquid_userFees", "open_fee_rate": "0",
                    "user_spot_cross_rate": str(fee_result[1]), "user_spot_add_rate": str(fee_result[0]),
                    "status": "observed_settlement_conversion_unverified",
                }
            elif fee_error is not None:
                self._fee_evidence = {
                    **self._fee_evidence,
                    "status": "stale_after_refresh_error" if self._last_fee_sync_ms else "unavailable",
                    "error_type": fee_error,
                }
            if fill_error is None:
                self._last_fill_sync_ms = completed_ms
            self._account_sync_in_flight = False
            self._account_sync_started_ms = None

    def _start_account_sync_if_due(self, *, market: OutcomeMarketSpec, now_ms: int) -> None:
        with self._account_lock:
            if self._account_sync_in_flight:
                return
            due = not self._last_fill_sync_ms or now_ms - self._last_fill_sync_ms >= self.account_sync_interval_ms
            if not due:
                return
            self._account_sync_in_flight = True
            self._account_sync_started_ms = now_ms
        if not self.account_sync_async:
            self._sync_account_state(market=market)
            return
        threading.Thread(
            target=self._sync_account_state, kwargs={"market": market}, daemon=True,
            name="outcome-research-account-sync",
        ).start()

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
        publish_outcome_market_authority(market)
        self._start_account_sync_if_due(market=market, now_ms=capture_complete_at_ms)
        if self._last_capture_ms and capture_complete_at_ms - self._last_capture_ms < self.interval_ms:
            return OutcomeResearchCaptureResult(False)
        # A gap means *three requested samples* are missing, not merely that a
        # single snapshot took longer than the target interval.  The old
        # comparison turned every expensive capture into an alert.
        gap_threshold_ms = max(self.gap_alert_ms, self.interval_ms * 3)
        if self._last_capture_ms and capture_complete_at_ms - self._last_capture_ms > gap_threshold_ms:
            self.journal.log_strategy_event(self.run_id, "OUTCOME_RESEARCH_CAPTURE_GAP_ALERT", {
                "venue": "hyperliquid_outcome", "read_only": True, "market_id": market.outcome_id,
                "period": market.period, "previous_capture_timestamp_ms": self._last_capture_ms,
                "current_capture_timestamp_ms": capture_complete_at_ms,
                "gap_ms": capture_complete_at_ms - self._last_capture_ms, "threshold_ms": gap_threshold_ms,
                "expected_interval_ms": self.interval_ms,
                "action": "gap_recorded_no_interpolation",
            })
        quality = build_p2_capture_quality(
            yes_book=yes_book, no_book=no_book, yes_local_received_at_ms=yes_local_received_at_ms,
            no_local_received_at_ms=no_local_received_at_ms, capture_complete_at_ms=capture_complete_at_ms,
        )
        maker_fee, taker_fee, fee_evidence = self._account_snapshot(now_ms=capture_complete_at_ms)
        parity = OutcomeParityAnalyzer(maker_close_fee_rate=maker_fee, taker_close_fee_rate=taker_fee).analyze(market, yes_book, no_book)
        snapshot_event_id = self.journal.log_strategy_event(self.run_id, "OUTCOME_P2_PARITY_SNAPSHOT", {
            "venue": "hyperliquid_outcome", "period": market.period, "p2_schema_version": P2_SCHEMA_VERSION,
            "snapshot_timestamp_ms": capture_complete_at_ms, "outcome_id": market.outcome_id,
            # These are decision-time facts, not retrospective metadata.  X4
            # needs them to test whether the same feature behaves differently
            # early versus late in a daily contract.
            "expiry": market.expiry_str,
            "time_left_sec": market.time_to_expiry_sec(current_timestamp=capture_complete_at_ms // 1000),
            "strike": str(market.strike),
            "yes_coin": market.yes_coin, "no_coin": market.no_coin, "yes_l2": dict(yes_book), "no_l2": dict(no_book),
            "capture_quality": quality, "fee_evidence": fee_evidence, **parity.as_dict(),
        })
        p3_written = 0
        if quality.get("status") == "accepted" and snapshot_event_id is not None:
            def _top(book: Mapping[str, Any]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
                levels = book.get("levels", [[], []])
                bids = levels[0] if isinstance(levels, list) and levels else []
                asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else []
                bid = Decimal(str(bids[0]["px"])) if bids else None
                ask = Decimal(str(asks[0]["px"])) if asks else None
                depth = Decimal(str(bids[0]["sz"])) if bids else None
                return bid, ask, depth

            yes_bid, yes_ask, yes_depth = _top(yes_book)
            no_bid, no_ask, no_depth = _top(no_book)
            time_left_sec = market.time_to_expiry_sec(current_timestamp=capture_complete_at_ms // 1000)
            p3_written = self._p3.record_quote_snapshot(
                snapshot_event_id=snapshot_event_id,
                outcome_id=market.outcome_id,
                period=market.period,
                snapshot_timestamp_ms=capture_complete_at_ms,
                quotes=(
                    OutcomeQuote(market.yes_coin, capture_complete_at_ms, yes_bid, yes_ask, snapshot_event_id),
                    OutcomeQuote(market.no_coin, capture_complete_at_ms, no_bid, no_ask, snapshot_event_id),
                ),
                quote_contexts={
                    market.yes_coin: {
                        "time_left_sec": time_left_sec,
                        "spread": str(yes_ask - yes_bid) if yes_ask is not None and yes_bid is not None else None,
                        "depth": str(yes_depth) if yes_depth is not None else None,
                        "volatility_regime": "unknown",
                    },
                    market.no_coin: {
                        "time_left_sec": time_left_sec,
                        "spread": str(no_ask - no_bid) if no_ask is not None and no_bid is not None else None,
                        "depth": str(no_depth) if no_depth is not None else None,
                        "volatility_regime": "unknown",
                    },
                },
            )
        self._last_capture_ms = capture_complete_at_ms
        accepted = quality.get("status") == "accepted"
        self._heartbeat(now_ms=capture_complete_at_ms, market=market, accepted=accepted, p3_written=p3_written)
        return OutcomeResearchCaptureResult(True, accepted, p3_written)
