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
    # A cross-validated WS snapshot is sufficient for steady-state monitoring,
    # but a mutation boundary deliberately pays for fresh REST truth.  If the
    # order disappeared between the decision and this read, do not issue a
    # blind cancel or infer whether it filled.
    force_reconcile = getattr(account, "force_open_orders_reconciliation_sync", None)
    if callable(force_reconcile):
        try:
            before = force_reconcile(wallet)
        except Exception as exc:
            return CancelConfirmResult(False, f"cancel_preflight_unavailable:{type(exc).__name__}")
        if not any(str(row.get("oid")) == str(order_id) for row in before):
            return CancelConfirmResult(False, "old_order_not_open_before_cancel")
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
