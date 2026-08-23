"""
Hyperliquid Outcome (HIP-4) Execution Adapter.

Manages:
- Maker BUY order submission with GTC Post-Only (ALO), hysteresis checking, cloid tracking.
- Minimum notional (10 USDC) validation and share rounding.
- Take-Profit (TP) tail-protect orders.
- Invalidation recovery ladder (Stage 1: Passive GTC Recovery SELL; Stage 2: IOC Marketable SELL).
- Native automatic settlement outcome reconciliation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from loguru import logger

from bot.adapters.outcome_auth import (
    align_outcome_price,
    align_outcome_size,
    generate_cloid,
    outcome_asset_id,
)
from bot.adapters.outcome_client import OutcomeClient
from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.pricing.outcome_pricing import (
    DEFAULT_MIN_NOTIONAL_USDC,
    compute_min_shares_for_notional,
)


@dataclass
class ActiveOutcomeOrder:
    outcome_id: int
    side_index: int            # 0 = UP / YES, 1 = DOWN / NO
    is_buy: bool
    price: Decimal
    size: Decimal
    order_type: str            # "ALO", "GTC", "IOC"
    cloid: str
    venue_oid: Optional[int]
    created_at: float
    is_exit: bool = False
    is_recovery: bool = False
    is_urgent: bool = False


class OutcomeExecutionAdapter:
    """
    Execution Adapter for Hyperliquid Outcome (HIP-4).
    """

    def __init__(
        self,
        client: OutcomeClient,
        min_notional_usdc: Decimal = DEFAULT_MIN_NOTIONAL_USDC,
        sz_decimals: int = 0,
    ) -> None:
        self.client = client
        self.min_notional_usdc = min_notional_usdc
        self.sz_decimals = int(sz_decimals)
        self._active_orders: Dict[str, ActiveOutcomeOrder] = {}  # cloid -> ActiveOutcomeOrder

    @property
    def active_orders(self) -> Dict[str, ActiveOutcomeOrder]:
        return self._active_orders

    def get_order_by_cloid(self, cloid: str) -> Optional[ActiveOutcomeOrder]:
        return self._active_orders.get(cloid)

    async def submit_maker_buy(
        self,
        outcome_id: int,
        side_index: int,
        price: Union[float, Decimal, str],
        size: Union[float, Decimal, str],
        cloid: Optional[str] = None,
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Submit a Maker BUY GTC Post-Only order on Outcome.
        Ensures min notional (10 USDC) is satisfied.
        """
        p = Decimal(str(price))
        s = Decimal(align_outcome_size(size, sz_decimals=self.sz_decimals))

        # Check / enforce min notional
        if p * s < self.min_notional_usdc:
            s = compute_min_shares_for_notional(p, self.min_notional_usdc, sz_decimals=self.sz_decimals)
            logger.info(f"Adjusted BUY size to {s} to satisfy min notional {self.min_notional_usdc} USDC at price {p}")

        order_cloid = cloid or generate_cloid()

        resp = await self.client.submit_order(
            outcome_id=outcome_id,
            side_index=side_index,
            is_buy=True,
            price=p,
            size=s,
            order_type="ALO",  # Add Liquidity Only (Post-Only)
            reduce_only=False,
            cloid=order_cloid,
            vault_address=vault_address,
            sz_decimals=self.sz_decimals,
        )

        if not resp.get("success"):
            return {"success": False, "error": resp.get("error"), "response": resp, "cloid": order_cloid}
        venue_oid = resp.get("order_id")
        statuses = (
            resp.get("result", {})
            .get("response", {})
            .get("data", {})
            .get("statuses", [])
        )
        if statuses:
            resting = statuses[0].get("resting")
            if resting:
                venue_oid = resting.get("oid")

        active_order = ActiveOutcomeOrder(
            outcome_id=outcome_id,
            side_index=side_index,
            is_buy=True,
            price=p,
            size=s,
            order_type="ALO",
            cloid=order_cloid,
            venue_oid=venue_oid,
            created_at=time.time(),
        )
        if resp.get("status") == "resting":
            self._active_orders[order_cloid] = active_order

        return {
            "success": True,
            "cloid": order_cloid,
            "venue_oid": venue_oid,
            "outcome_id": outcome_id,
            "side_index": side_index,
            "price": p,
            "size": s,
            "response": resp,
        }

    async def submit_take_profit(
        self,
        outcome_id: int,
        side_index: int,
        size: Union[float, Decimal, str],
        tp_price: Union[float, Decimal, str] = Decimal("0.97"),
        cloid: Optional[str] = None,
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Submit a passive Take-Profit (TP) GTC SELL limit order.
        """
        p = Decimal(str(tp_price))
        s = Decimal(align_outcome_size(size, sz_decimals=self.sz_decimals))
        order_cloid = cloid or generate_cloid()

        resp = await self.client.submit_order(
            outcome_id=outcome_id,
            side_index=side_index,
            is_buy=False,
            price=p,
            size=s,
            order_type="Gtc",
            reduce_only=True,
            cloid=order_cloid,
            vault_address=vault_address,
            sz_decimals=self.sz_decimals,
        )

        if not resp.get("success"):
            return {"success": False, "error": resp.get("error"), "response": resp, "cloid": order_cloid}
        venue_oid = resp.get("order_id")
        statuses = (
            resp.get("result", {})
            .get("response", {})
            .get("data", {})
            .get("statuses", [])
        )
        if statuses:
            resting = statuses[0].get("resting")
            if resting:
                venue_oid = resting.get("oid")

        active_order = ActiveOutcomeOrder(
            outcome_id=outcome_id,
            side_index=side_index,
            is_buy=False,
            price=p,
            size=s,
            order_type="Gtc",
            cloid=order_cloid,
            venue_oid=venue_oid,
            created_at=time.time(),
            is_exit=True,
        )
        if resp.get("status") == "resting":
            self._active_orders[order_cloid] = active_order

        return {
            "success": True,
            "cloid": order_cloid,
            "venue_oid": venue_oid,
            "outcome_id": outcome_id,
            "side_index": side_index,
            "price": p,
            "size": s,
            "response": resp,
        }

    async def submit_recovery_exit(
        self,
        outcome_id: int,
        side_index: int,
        size: Union[float, Decimal, str],
        exit_price: Union[float, Decimal, str],
        is_ioc: bool = False,
        cloid: Optional[str] = None,
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Submit Invalidation Recovery Exit SELL order.
        - Stage 1: Passive GTC Recovery SELL (is_ioc=False)
        - Stage 2: IOC Marketable SELL (is_ioc=True)
        """
        p = Decimal(str(exit_price))
        s = Decimal(align_outcome_size(size, sz_decimals=self.sz_decimals))
        order_cloid = cloid or generate_cloid()
        tif = "Ioc" if is_ioc else "Gtc"

        resp = await self.client.submit_order(
            outcome_id=outcome_id,
            side_index=side_index,
            is_buy=False,
            price=p,
            size=s,
            order_type=tif,
            reduce_only=True,
            cloid=order_cloid,
            vault_address=vault_address,
            sz_decimals=self.sz_decimals,
        )

        if not resp.get("success"):
            return {"success": False, "error": resp.get("error"), "response": resp, "cloid": order_cloid}
        venue_oid = resp.get("order_id")
        statuses = (
            resp.get("result", {})
            .get("response", {})
            .get("data", {})
            .get("statuses", [])
        )
        if statuses:
            resting = statuses[0].get("resting")
            if resting:
                venue_oid = resting.get("oid")

        active_order = ActiveOutcomeOrder(
            outcome_id=outcome_id,
            side_index=side_index,
            is_buy=False,
            price=p,
            size=s,
            order_type=tif,
            cloid=order_cloid,
            venue_oid=venue_oid,
            created_at=time.time(),
            is_exit=True,
            is_recovery=True,
            is_urgent=is_ioc,
        )
        if resp.get("status") == "resting":
            self._active_orders[order_cloid] = active_order

        return {
            "success": True,
            "cloid": order_cloid,
            "venue_oid": venue_oid,
            "outcome_id": outcome_id,
            "side_index": side_index,
            "price": p,
            "size": s,
            "is_ioc": is_ioc,
            "response": resp,
        }

    async def cancel_order(
        self,
        outcome_id: int,
        side_index: int,
        cloid: Optional[str] = None,
        venue_oid: Optional[int] = None,
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancel an open order by cloid or venue oid."""
        if cloid:
            res = await self.client.cancel_by_cloid(
                outcome_id=outcome_id,
                side_index=side_index,
                cloid=cloid,
                vault_address=vault_address,
            )
            self._active_orders.pop(cloid, None)
            return res
        elif venue_oid is not None:
            res = await self.client.cancel_order(
                outcome_id=outcome_id,
                side_index=side_index,
                order_id=venue_oid,
                vault_address=vault_address,
            )
            # Remove from local map
            for c, o in list(self._active_orders.items()):
                if o.venue_oid == venue_oid:
                    self._active_orders.pop(c, None)
            return res
        else:
            raise ValueError("Either cloid or venue_oid must be provided to cancel_order")

    def compute_settlement(
        self,
        market_spec: OutcomeMarketSpec,
        settlement_mark_price: Decimal | float,
        inventory_shares: Decimal | float,
        inventory_cost_usdc: Decimal | float,
        held_side_index: int = 0,  # 0 = UP / YES, 1 = DOWN / NO
    ) -> Dict[str, Any]:
        raise RuntimeError(
            "Outcome settlement inference is disabled: pass official resolution evidence through the settlement bridge instead."
        )
