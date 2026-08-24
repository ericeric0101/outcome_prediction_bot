"""Official-SDK settlement verification and guarded paired-share merge flow."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_sdk_sidecar import OutcomeSdkSidecarClient


@dataclass(frozen=True)
class OutcomeSettlement:
    market_id: int
    settled: bool
    settle_fraction: Decimal | None
    details: str | None
    raw: dict[str, Any] | None


class OutcomeSettlementAdapter:
    """Never infer settlement from BTC price or local strategy state."""

    def __init__(self, sidecar: OutcomeSdkSidecarClient | None = None) -> None:
        self.sidecar = sidecar or OutcomeSdkSidecarClient()

    def fetch(self, market: OutcomeMarketSpec) -> OutcomeSettlement:
        raw = self.sidecar.request("fetch_settled_outcome", payload={"marketId": str(market.outcome_id)})
        if raw is None:
            return OutcomeSettlement(market.outcome_id, False, None, None, None)
        fraction = raw.get("settleFraction")
        return OutcomeSettlement(market.outcome_id, True, Decimal(str(fraction)) if fraction is not None else None, raw.get("details"), raw)

    def merge_paired_shares(self, *, market: OutcomeMarketSpec, amount: Decimal) -> dict[str, Any]:
        """Merge paired Yes+No shares only after official settlement confirmation.

        This is intentionally not called ``redeem``: standalone one-sided
        binary shares have no documented generic SDK redeem action.
        """
        if not self.fetch(market).settled:
            raise RuntimeError("refusing merge: official SDK has not confirmed settlement")
        return self.sidecar.request("merge_outcome", payload={"marketId": str(market.outcome_id), "amount": str(amount)}, allow_execution=True)
