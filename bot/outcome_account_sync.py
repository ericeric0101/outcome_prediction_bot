"""Read-only Hyperliquid Outcome account synchronization.

This is intentionally a read-only Outcome adapter.  It normalizes the three
official account endpoints and does not sign or submit an exchange action.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional
from bot.outcome_event_bridge import (
    OutcomeFillEvent,
    parse_outcome_balance_coin,
    parse_outcome_coin,
)


def _decimal(payload: Mapping[str, Any], key: str, default: Decimal = Decimal("0")) -> Decimal:
    value = payload.get(key)
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception as exc:  # Decimal raises several implementation-specific errors.
        raise ValueError(f"Outcome account payload has invalid {key!r}: {payload!r}") from exc


@dataclass(frozen=True)
class OutcomeBalance:
    """One HIP-4 token balance from ``spotClearinghouseState``."""

    outcome_id: int
    side_index: int
    coin: str
    total_qty: Decimal
    held_qty: Decimal
    available_qty: Decimal
    entry_notional: Decimal
    avg_entry_price: Decimal
    raw: Mapping[str, Any]

    @property
    def outcome_side(self) -> str:
        return "UP" if self.side_index == 0 else "DOWN"

    @classmethod
    def from_spot_balance(cls, payload: Mapping[str, Any]) -> "OutcomeBalance":
        coin = str(payload.get("coin", ""))
        outcome_id, side_index = parse_outcome_balance_coin(coin)
        total = _decimal(payload, "total")
        held = _decimal(payload, "hold")
        if total < 0 or held < 0 or held > total:
            raise ValueError(f"Invalid Outcome balance quantities: {payload!r}")
        entry_notional = _decimal(payload, "entryNtl")
        return cls(
            outcome_id=outcome_id,
            side_index=side_index,
            coin=coin,
            total_qty=total,
            held_qty=held,
            available_qty=total - held,
            entry_notional=entry_notional,
            # Hyperliquid exposes aggregate entry notional in this endpoint;
            # do not manufacture a cost basis when the position is empty.
            avg_entry_price=(entry_notional / total) if total > 0 else Decimal("0"),
            raw=payload,
        )


@dataclass(frozen=True)
class OutcomeOpenOrder:
    """Normalized read-only view of an active HIP-4 order."""

    outcome_id: int
    side_index: int
    coin: str
    order_id: Optional[str]
    client_order_id: Optional[str]
    side: str
    price: Optional[Decimal]
    quantity: Optional[Decimal]
    raw: Mapping[str, Any]

    @classmethod
    def from_frontend_order(cls, payload: Mapping[str, Any]) -> "OutcomeOpenOrder":
        coin = str(payload.get("coin", ""))
        outcome_id, side_index = parse_outcome_coin(coin)
        raw_side = str(payload.get("side", "")).upper()
        side = "BUY" if raw_side in {"B", "BUY"} else "SELL" if raw_side in {"A", "SELL"} else raw_side
        price_value = payload.get("limitPx", payload.get("px"))
        size_value = payload.get("sz", payload.get("origSz"))
        return cls(
            outcome_id=outcome_id,
            side_index=side_index,
            coin=coin,
            order_id=str(payload["oid"]) if payload.get("oid") is not None else None,
            client_order_id=str(payload["cloid"]) if payload.get("cloid") else None,
            side=side,
            price=Decimal(str(price_value)) if price_value is not None else None,
            quantity=Decimal(str(size_value)) if size_value is not None else None,
            raw=payload,
        )


@dataclass(frozen=True)
class OutcomeAccountSnapshot:
    """A coherent, read-only account view returned by the three HIP-4 queries."""

    balances: tuple[OutcomeBalance, ...]
    open_orders: tuple[OutcomeOpenOrder, ...]
    fills: tuple[OutcomeFillEvent, ...]
    ignored_settlement_fills: tuple[Mapping[str, Any], ...]

    def balance_for(self, outcome_id: int, side_index: int) -> Optional[OutcomeBalance]:
        return next(
            (b for b in self.balances if b.outcome_id == outcome_id and b.side_index == side_index),
            None,
        )

class OutcomeAccountSynchronizer:
    """Fetch and translate HIP-4 account endpoints without exchange access."""

    def __init__(self, client: Any, wallet_address: str) -> None:
        address = str(wallet_address or "").strip()
        if not (address.startswith("0x") and len(address) == 42):
            raise ValueError("wallet_address must be a 20-byte 0x-prefixed address")
        self.client = client
        self.wallet_address = address.lower()

    def fetch_snapshot(self) -> OutcomeAccountSnapshot:
        """Read balances, open orders, and fills.  No exchange request is made."""
        clearinghouse = self.client.get_spot_clearinghouse_state_sync(self.wallet_address)
        raw_orders = self.client.get_open_orders_sync(self.wallet_address)
        raw_fills = self.client.get_user_fills_sync(self.wallet_address)
        balances = tuple(
            OutcomeBalance.from_spot_balance(item)
            for item in clearinghouse.get("balances", [])
            if str(item.get("coin", "")).startswith("+")
        )
        orders = tuple(
            OutcomeOpenOrder.from_frontend_order(item)
            for item in raw_orders
            if str(item.get("coin", "")).startswith("#")
        )
        fills: list[OutcomeFillEvent] = []
        settlements: list[Mapping[str, Any]] = []
        for item in raw_fills:
            if not str(item.get("coin", "")).startswith("#"):
                continue
            if str(item.get("dir", "")).strip().lower() == "settlement":
                settlements.append(item)
                continue
            fills.append(OutcomeFillEvent.from_user_fill(item))
        return OutcomeAccountSnapshot(
            balances=balances,
            open_orders=orders,
            fills=tuple(fills),
            # A settlement fill is retained as evidence only.  A winning side
            # still requires an authoritative Outcome settlement source.
            ignored_settlement_fills=tuple(settlements),
        )
