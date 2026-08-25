"""P3 research pipeline: durable actual-fill provenance and executable markouts."""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterable

from bot.outcome_event_bridge import OutcomeFillEvent, OutcomeJournalBridge
from bot.outcome_markout import OutcomeQuote, markouts_for_fill
from bot.outcome_p2_quality import is_eligible_p2_snapshot
from monitoring.trade_journal_db import TradeJournalDB


class OutcomeP3Pipeline:
    """Records only exchange-confirmed fills; synthetic candidates are excluded."""

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id
        self.bridge = OutcomeJournalBridge(journal, run_id)

    def _has_fill(self, trade_id: str) -> bool:
        with sqlite3.connect(self.journal.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM order_events WHERE event_type='ORDER_FILLED' AND json_extract(payload_json, '$.trade_id')=? LIMIT 1",
                (trade_id,),
            ).fetchone()
        return row is not None

    def record_actual_fill(self, fill: OutcomeFillEvent, *, period: str | None) -> bool:
        if self._has_fill(fill.trade_id):
            return False
        return self.bridge.record_fill(fill, market_key=f"outcome:{fill.outcome_id}", extra_payload={
            "actual_fill": True,
            "period": period or "unknown",
            "fill_provenance": "hyperliquid_userFills",
            "research_only": True,
        })

    def _actual_fills(self, *, outcome_id: int, period: str) -> list[OutcomeFillEvent]:
        with sqlite3.connect(self.journal.db_path) as conn:
            rows = conn.execute(
                "SELECT payload_json, client_order_id, venue_order_id, side, price, qty, commission_usdc FROM order_events WHERE event_type='ORDER_FILLED'"
            ).fetchall()
        output = []
        for payload_raw, cloid, oid, side, price, qty, fee in rows:
            try:
                payload = json.loads(payload_raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("actual_fill") is not True or payload.get("period") != period or int(payload.get("outcome_id", -1)) != outcome_id:
                continue
            timestamp = payload.get("timestamp_ms")
            trade_id = payload.get("trade_id")
            coin = payload.get("coin")
            if not isinstance(timestamp, int) or not trade_id or not coin or price is None or qty is None or side not in {"BUY", "SELL"}:
                continue
            output.append(OutcomeFillEvent(
                outcome_id=outcome_id, side_index=int(payload["side_index"]), coin=str(coin),
                client_order_id=cloid, venue_order_id=oid, trade_id=str(trade_id), side=str(side),
                price=Decimal(str(price)), quantity=Decimal(str(qty)), fee_usdc=Decimal(str(fee or 0)),
                fee_token=str(payload.get("fee_token", "")), timestamp_ms=timestamp,
                is_maker=payload.get("liquidity_class") == "maker", raw=payload,
            ))
        return output

    def _has_markout(self, trade_id: str, horizon_sec: int) -> bool:
        with sqlite3.connect(self.journal.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM order_events WHERE event_type='FILL_MARKOUT' AND json_extract(payload_json, '$.fill_id')=? AND CAST(json_extract(payload_json, '$.horizon_sec') AS INTEGER)=? LIMIT 1",
                (trade_id, horizon_sec),
            ).fetchone()
        return row is not None

    def quotes_from_journal(self, *, outcome_id: int, period: str) -> tuple[OutcomeQuote, ...]:
        """Rehydrate executable BBO history from durable P2 snapshots."""
        with sqlite3.connect(self.journal.db_path) as conn:
            rows = conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_P2_PARITY_SNAPSHOT'").fetchall()
        quotes: list[OutcomeQuote] = []
        for (raw,) in rows:
            try:
                snapshot = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(snapshot, dict) or not is_eligible_p2_snapshot(snapshot):
                continue
            if snapshot.get("period") != period or int(snapshot.get("outcome_id", -1)) != outcome_id:
                continue
            timestamp = snapshot.get("snapshot_timestamp_ms")
            if not isinstance(timestamp, int):
                continue
            for coin_key, book_key in (("yes_coin", "yes_l2"), ("no_coin", "no_l2")):
                book = snapshot.get(book_key) or {}
                levels = book.get("levels", [[], []]) if isinstance(book, dict) else [[], []]
                bids, asks = levels[0] if levels else [], levels[1] if len(levels) > 1 else []
                bid = Decimal(str(bids[0]["px"])) if bids else None
                ask = Decimal(str(asks[0]["px"])) if asks else None
                coin = snapshot.get(coin_key)
                if isinstance(coin, str):
                    quotes.append(OutcomeQuote(coin, timestamp, bid, ask))
        return tuple(quotes)

    def observe_quotes(self, *, outcome_id: int, period: str, quotes: Iterable[OutcomeQuote], time_left_sec: float, spread: Decimal | None, depth: Decimal | None, volatility_regime: str) -> int:
        written = 0
        for fill in self._actual_fills(outcome_id=outcome_id, period=period):
            if not fill.is_maker:
                continue
            for observation in markouts_for_fill(fill, quotes):
                if observation.status != "observed" or self._has_markout(fill.trade_id, observation.horizon_sec):
                    continue
                assert observation.markout_per_share is not None
                bucket = self._bucket(time_left_sec, fill.side, spread, depth, volatility_regime)
                self.journal.log_order_event(
                    self.run_id, "FILL_MARKOUT", client_order_id=fill.client_order_id, venue_order_id=fill.venue_order_id,
                    side=fill.side, price=float(observation.executable_mark), qty=float(fill.quantity), status="OBSERVED",
                    instrument_id=fill.coin, commission_usdc=float(fill.fee_usdc),
                    payload={
                        "venue": "hyperliquid_outcome", "actual_fill": True, "fill_id": fill.trade_id,
                        "outcome_id": outcome_id, "period": period, "coin": fill.coin,
                        "horizon_sec": observation.horizon_sec, "markout_mid": float(observation.executable_mark),
                        "signed_markout_ps": float(observation.markout_per_share),
                        "fee_per_share": float(fill.fee_usdc / fill.quantity) if fill.quantity else 0.0,
                        "entry_regime_bucket": bucket, "time_left_sec": time_left_sec,
                        "spread": float(spread) if spread is not None else None,
                        "depth": float(depth) if depth is not None else None,
                        "volatility_regime": volatility_regime,
                        "executable_quote": True, "counterfactual": False,
                    },
                )
                written += 1
        return written

    @staticmethod
    def _bucket(time_left_sec: float, side: str, spread: Decimal | None, depth: Decimal | None, volatility_regime: str) -> str:
        time_bucket = "lt_300" if time_left_sec < 300 else "300_600" if time_left_sec < 600 else "600_plus"
        spread_bucket = "unknown" if spread is None else "tight" if spread <= Decimal("0.01") else "wide"
        depth_bucket = "unknown" if depth is None else "deep" if depth >= Decimal("100") else "thin"
        return f"{time_bucket}|{side.lower()}|{spread_bucket}|{depth_bucket}|{volatility_regime}"
