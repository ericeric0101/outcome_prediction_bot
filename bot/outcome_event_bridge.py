"""Translate Hyperliquid Outcome payloads into the existing strategy journal.

This module is deliberately independent of Nautilus and Polymarket. It is the
first migration seam: strategy/risk code continues to consume the established
order and settlement event schema while venue-specific JSON remains here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

from bot.adapters.outcome_auth import outcome_asset_id
from monitoring.trade_journal_db import TradeJournalDB


def _decimal(payload: Mapping[str, Any], key: str) -> Decimal:
    try:
        return Decimal(str(payload[key]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Outcome fill has no valid {key!r}: {payload!r}") from exc


def parse_outcome_coin(coin: str) -> tuple[int, int]:
    """Return (outcome_id, side_index) from an HIP-4 market coin such as #11450."""
    text = str(coin or "").strip()
    if not text.startswith("#") or not text[1:].isdigit():
        raise ValueError(f"Not an HIP-4 outcome coin: {coin!r}")
    return _parse_outcome_encoding(text[1:], coin)


def parse_outcome_balance_coin(coin: str) -> tuple[int, int]:
    """Parse an outcome balance coin (``+11450``) from spot clearinghouse state.

    Hyperliquid represents tradable outcome books as ``#<encoding>`` and settled
    / held outcome tokens in spot balances as ``+<encoding>``.  Keeping these
    distinct prevents a balance payload from accidentally being used as an order
    book identifier.
    """
    text = str(coin or "").strip()
    if not text.startswith("+") or not text[1:].isdigit():
        raise ValueError(f"Not an HIP-4 outcome balance coin: {coin!r}")
    return _parse_outcome_encoding(text[1:], coin)


def _parse_outcome_encoding(encoded_text: str, original: str) -> tuple[int, int]:
    encoded = int(encoded_text)
    side_index = encoded % 10
    if side_index not in (0, 1):
        raise ValueError(f"Outcome coin has invalid side index: {original!r}")
    return encoded // 10, side_index


def _normalize_fill_side(payload: Mapping[str, Any]) -> str:
    """Map Hyperliquid's B/A fields to the strategy's BUY/SELL schema."""
    raw = str(payload.get("side", "")).strip().upper()
    if raw in {"B", "BUY"}:
        return "BUY"
    if raw in {"A", "SELL"}:
        return "SELL"
    direction = str(payload.get("dir", "")).strip().upper()
    if direction.startswith("BUY") or direction.startswith("OPEN LONG"):
        return "BUY"
    if direction.startswith("SELL") or direction.startswith("CLOSE LONG"):
        return "SELL"
    raise ValueError(f"Outcome fill has unknown side: {payload!r}")


@dataclass(frozen=True)
class OutcomeFillEvent:
    """A venue-neutral representation of one HIP-4 user fill."""

    outcome_id: int
    side_index: int
    coin: str
    client_order_id: Optional[str]
    venue_order_id: Optional[str]
    trade_id: str
    side: str
    price: Decimal
    quantity: Decimal
    fee_usdc: Decimal
    fee_token: str
    timestamp_ms: int
    is_maker: bool
    raw: Mapping[str, Any]

    @property
    def outcome_side(self) -> str:
        return "UP" if self.side_index == 0 else "DOWN"

    @classmethod
    def from_user_fill(cls, payload: Mapping[str, Any]) -> "OutcomeFillEvent":
        coin = str(payload.get("coin", ""))
        outcome_id, side_index = parse_outcome_coin(coin)
        direction = str(payload.get("dir", "")).strip().lower()
        if direction == "settlement":
            raise ValueError("Settlement payloads must use OutcomeSettlementEvent")
        timestamp = payload.get("time")
        if not isinstance(timestamp, int):
            raise ValueError(f"Outcome fill has no valid timestamp: {payload!r}")
        trade_id = str(payload.get("tid") or payload.get("hash") or "")
        if not trade_id:
            raise ValueError(f"Outcome fill has no trade identifier: {payload!r}")
        return cls(
            outcome_id=outcome_id,
            side_index=side_index,
            coin=coin,
            client_order_id=(str(payload["cloid"]) if payload.get("cloid") else None),
            venue_order_id=(str(payload["oid"]) if payload.get("oid") is not None else None),
            trade_id=trade_id,
            side=_normalize_fill_side(payload),
            price=_decimal(payload, "px"),
            quantity=_decimal(payload, "sz"),
            fee_usdc=_decimal(payload, "fee") if payload.get("fee") is not None else Decimal("0"),
            fee_token=str(payload.get("feeToken", "")),
            timestamp_ms=timestamp,
            is_maker=not bool(payload.get("crossed", False)),
            raw=payload,
        )


@dataclass(frozen=True)
class OutcomeSettlementEvent:
    """Verified settlement fact; never infer it from the current BTC mark."""

    outcome_id: int
    winning_side_index: int
    settlement_price: Optional[Decimal]
    source: str

    def __post_init__(self) -> None:
        if self.winning_side_index not in (0, 1):
            raise ValueError("winning_side_index must be 0 (UP/YES) or 1 (DOWN/NO)")

    @property
    def outcome_side(self) -> str:
        return "UP" if self.winning_side_index == 0 else "DOWN"


class OutcomeJournalBridge:
    """Persist Outcome events using the existing strategy analytics schema."""

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal = journal
        self.run_id = run_id

    def record_fill(
        self,
        fill: OutcomeFillEvent,
        *,
        market_key: str,
        extra_payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "venue": "hyperliquid_outcome",
            "market_key": market_key,
            "outcome_id": fill.outcome_id,
            "outcome_side": fill.outcome_side,
            "side_index": fill.side_index,
            "coin": fill.coin,
            "asset_id": outcome_asset_id(fill.outcome_id, fill.side_index),
            "trade_id": fill.trade_id,
            "timestamp_ms": fill.timestamp_ms,
            "liquidity_class": "maker" if fill.is_maker else "taker",
            "fee_token": fill.fee_token,
        }
        if extra_payload:
            payload.update(dict(extra_payload))
        self.journal.log_order_event(
            self.run_id,
            "ORDER_FILLED",
            client_order_id=fill.client_order_id,
            venue_order_id=fill.venue_order_id,
            side=fill.side,
            price=float(fill.price),
            qty=float(fill.quantity),
            status="FILLED",
            instrument_id=fill.coin,
            commission_usdc=float(fill.fee_usdc),
            payload=payload,
        )

    def record_settlement(
        self,
        settlement: OutcomeSettlementEvent,
        *,
        market_key: str,
        extra_payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "venue": "hyperliquid_outcome",
            "market_key": market_key,
            "outcome_id": settlement.outcome_id,
            "outcome": settlement.outcome_side,
            "winning_side_index": settlement.winning_side_index,
            "settlement_source": settlement.source,
        }
        if settlement.settlement_price is not None:
            payload["settlement_price"] = settlement.settlement_price
        if extra_payload:
            payload.update(dict(extra_payload))
        self.journal.log_strategy_event(self.run_id, "MARKET_SETTLEMENT", payload)
