"""The sole cancel-and-confirm mutation primitive for Outcome orders.

An SDK transport acknowledgement is not evidence that an order disappeared
from the venue.  Every execution lane that reduces risk must therefore use
this helper and treat any unreadable or still-open order as reconciliation
required rather than as cancelled.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec


class CancelAccountReader(Protocol):
    def get_open_orders_sync(self, user: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class CancelConfirmResult:
    confirmed: bool
    reason: str


def cancel_and_confirm(
    *, account: CancelAccountReader, gateway: Any, wallet: str,
    market: OutcomeMarketSpec, side_index: int, order_id: str,
) -> CancelConfirmResult:
    """Cancel one owned order, then require fresh venue absence.

    The caller retains lifecycle-specific ownership and inventory checks.  A
    shared primitive keeps the transport acknowledgement gap from drifting
    between entry replacement, exit replacement, emergency, rollover, and
    graceful shutdown.
    """
    try:
        gateway.cancel_owned_order(market=market, side_index=side_index, order_id=str(order_id))
    except Exception as exc:
        return CancelConfirmResult(False, f"cancel_request_failed:{type(exc).__name__}")
    invalidate = getattr(account, "invalidate", None)
    if callable(invalidate):
        invalidate()
    try:
        still_open = any(str(row.get("oid")) == str(order_id) for row in account.get_open_orders_sync(wallet))
    except Exception as exc:
        return CancelConfirmResult(False, f"cancel_confirmation_unavailable:{type(exc).__name__}")
    return CancelConfirmResult(not still_open, "cancel_confirmed" if not still_open else "old_order_still_open_after_cancel")
