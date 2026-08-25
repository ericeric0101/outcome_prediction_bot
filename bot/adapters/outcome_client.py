"""
Hyperliquid Outcome (HIP-4) Native Python Async/Sync REST and WebSocket Client.

Provides:
- Complete REST `/info` endpoint queries:
  - outcomeMeta
  - allMids
  - l2Book
  - clearinghouseState / userState
  - userOutcome
  - openOrders / frontendOpenOrders
  - userFills
- Complete REST `/exchange` endpoint actions:
  - order (Limit GTC, Post-Only / ALO, IOC, reduce-only, cloid)
  - cancel (by oid)
  - cancelByCloid
  - userOutcome (split, merge)
  - approveAgent
- Async WebSocket connection manager for real-time feeds:
  - allMids
  - l2Book
  - userEvents / orderUpdates / fills
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

import httpx
from loguru import logger
import websockets

from bot.adapters.outcome_auth import (
    OutcomeAuth,
    align_outcome_price,
    align_outcome_size,
    generate_cloid,
    outcome_asset_id,
    parse_outcome_asset_id,
)


class OutcomeClient:
    """
    Hyperliquid Outcome REST and WebSocket Client.
    """

    def __init__(
        self,
        auth: OutcomeAuth,
        timeout_sec: float = 10.0,
    ) -> None:
        self.auth = auth
        self.base_url = auth.base_url
        self.ws_url = auth.ws_url
        self.wallet_address = auth.wallet_address
        self.timeout_sec = timeout_sec
        self._async_client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_running = False
        self._subscriptions: Dict[str, Dict[str, Any]] = {}
        self._callbacks: Dict[str, List[Callable[[Dict[str, Any]], Any]]] = {}
        self._cache: Dict[str, Tuple[float, Any]] = {}

    async def get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_sec, connect=5.0),
                headers={"Content-Type": "application/json"},
            )
        return self._async_client

    def get_sync_client(self) -> httpx.Client:
        if self._sync_client is None or self._sync_client.is_closed:
            self._sync_client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_sec, connect=5.0),
                headers={"Content-Type": "application/json"},
            )
        return self._sync_client

    async def close(self) -> None:
        if self._async_client and not self._async_client.is_closed:
            await self._async_client.aclose()
            self._async_client = None
        if self._sync_client and not self._sync_client.is_closed:
            self._sync_client.close()
            self._sync_client = None
        await self.stop_ws()

    # --------------------------------------------------------------------------
    # REST /info Endpoints with Rate-Limit Resilience & Caching
    # --------------------------------------------------------------------------

    @staticmethod
    def _info_retry_delay(attempt: int) -> float:
        """Bounded exponential backoff for rate limits and transient upstream errors."""
        return min(8.0, float(2 ** (attempt - 1)))

    async def post_info(self, payload: Dict[str, Any], max_retries: int = 3) -> Any:
        """Raw POST request to /info with retry for 429, 5xx, and transport failures."""
        client = await self.get_async_client()
        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.post("/info", json=payload)
                if resp.status_code == 429 or 500 <= resp.status_code <= 599:
                    wait_sec = self._info_retry_delay(attempt)
                    logger.warning(
                        f"Hyperliquid /info transient HTTP {resp.status_code} for {payload.get('type')}; "
                        f"retrying in {wait_sec:.1f}s (attempt {attempt}/{max_retries})"
                    )
                    await asyncio.sleep(wait_sec)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if (e.response.status_code == 429 or 500 <= e.response.status_code <= 599) and attempt < max_retries:
                    wait_sec = self._info_retry_delay(attempt)
                    logger.warning(f"Hyperliquid /info HTTP {e.response.status_code}; retrying in {wait_sec:.1f}s...")
                    await asyncio.sleep(wait_sec)
                    continue
                logger.error(f"OutcomeClient.post_info error for {payload.get('type')}: {e}")
                raise
            except Exception as e:
                if attempt == max_retries:
                    logger.error(f"OutcomeClient.post_info error for {payload.get('type')}: {e}")
                    raise
                await asyncio.sleep(self._info_retry_delay(attempt))

    def post_info_sync(self, payload: Dict[str, Any], max_retries: int = 3) -> Any:
        """Synchronous /info request with retry for 429, 5xx, and transport failures."""
        client = self.get_sync_client()
        for attempt in range(1, max_retries + 1):
            try:
                resp = client.post("/info", json=payload)
                if resp.status_code == 429 or 500 <= resp.status_code <= 599:
                    wait_sec = self._info_retry_delay(attempt)
                    logger.warning(
                        f"Hyperliquid /info transient HTTP {resp.status_code} for {payload.get('type')}; "
                        f"retrying in {wait_sec:.1f}s (attempt {attempt}/{max_retries})"
                    )
                    time.sleep(wait_sec)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if (e.response.status_code == 429 or 500 <= e.response.status_code <= 599) and attempt < max_retries:
                    wait_sec = self._info_retry_delay(attempt)
                    logger.warning(f"Hyperliquid /info HTTP {e.response.status_code}; retrying in {wait_sec:.1f}s...")
                    time.sleep(wait_sec)
                    continue
                logger.error(f"OutcomeClient.post_info_sync error for {payload.get('type')}: {e}")
                raise
            except Exception as e:
                if attempt == max_retries:
                    logger.error(f"OutcomeClient.post_info_sync error for {payload.get('type')}: {e}")
                    raise
                time.sleep(self._info_retry_delay(attempt))

    async def get_outcome_meta(self, ttl_sec: float = 15.0) -> Dict[str, Any]:
        """Fetch outcome markets metadata with short TTL caching."""
        now = time.time()
        cached = self._cache.get("outcomeMeta")
        if cached and (now - cached[0]) < ttl_sec:
            return cached[1]
        data = await self.post_info({"type": "outcomeMeta"})
        self._cache["outcomeMeta"] = (now, data)
        return data

    def get_outcome_meta_sync(self, ttl_sec: float = 15.0) -> Dict[str, Any]:
        now = time.time()
        cached = self._cache.get("outcomeMeta")
        if cached and (now - cached[0]) < ttl_sec:
            return cached[1]
        data = self.post_info_sync({"type": "outcomeMeta"})
        self._cache["outcomeMeta"] = (now, data)
        return data

    async def get_spot_meta(self) -> Dict[str, Any]:
        """Fetch spot markets and token metadata."""
        return await self.post_info({"type": "spotMeta"})

    def get_spot_meta_sync(self) -> Dict[str, Any]:
        return self.post_info_sync({"type": "spotMeta"})

    async def get_meta(self) -> Dict[str, Any]:
        """Fetch perps metadata and assets."""
        return await self.post_info({"type": "meta"})

    def get_meta_sync(self) -> Dict[str, Any]:
        return self.post_info_sync({"type": "meta"})

    async def get_all_mids(self, ttl_sec: float = 2.0) -> Dict[str, str]:
        """Fetch all mid prices across perps, spot, and outcomes with TTL caching."""
        now = time.time()
        cached = self._cache.get("allMids")
        if cached and (now - cached[0]) < ttl_sec:
            return cached[1]
        data = await self.post_info({"type": "allMids"})
        self._cache["allMids"] = (now, data)
        return data

    def get_all_mids_sync(self, ttl_sec: float = 2.0) -> Dict[str, str]:
        now = time.time()
        cached = self._cache.get("allMids")
        if cached and (now - cached[0]) < ttl_sec:
            return cached[1]
        data = self.post_info_sync({"type": "allMids"})
        self._cache["allMids"] = (now, data)
        return data

    def get_user_fees_sync(self, user: str) -> Dict[str, Any]:
        """Fetch the wallet's effective Hyperliquid spot fee schedule read-only."""
        return self.post_info_sync({"type": "userFees", "user": user})

    async def get_l2_book(self, coin: str, ttl_sec: float = 1.0) -> Dict[str, Any]:
        """Fetch L2 order book depth for a coin with TTL caching."""
        now = time.time()
        cache_key = f"l2Book:{coin}"
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < ttl_sec:
            return cached[1]
        data = await self.post_info({"type": "l2Book", "coin": coin})
        self._cache[cache_key] = (now, data)
        return data

    def get_l2_book_sync(self, coin: str, ttl_sec: float = 1.0) -> Dict[str, Any]:
        now = time.time()
        cache_key = f"l2Book:{coin}"
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < ttl_sec:
            return cached[1]
        data = self.post_info_sync({"type": "l2Book", "coin": coin})
        self._cache[cache_key] = (now, data)
        return data

    async def get_clearinghouse_state(self, user: Optional[str] = None) -> Dict[str, Any]:
        """Fetch user clearinghouse state (margin, balances, perps positions)."""
        target_user = (user or self.wallet_address).lower()
        return await self.post_info({"type": "clearinghouseState", "user": target_user})

    async def get_spot_clearinghouse_state(self, user: Optional[str] = None) -> Dict[str, Any]:
        """Fetch user spot clearinghouse state (token balances)."""
        target_user = (user or self.wallet_address).lower()
        return await self.post_info({"type": "spotClearinghouseState", "user": target_user})

    def get_spot_clearinghouse_state_sync(self, user: Optional[str] = None) -> Dict[str, Any]:
        """Synchronously fetch user spot balances, including HIP-4 outcome tokens."""
        target_user = (user or self.wallet_address).lower()
        return self.post_info_sync({"type": "spotClearinghouseState", "user": target_user})

    async def get_user_outcome(self, outcome_id: int, user: Optional[str] = None) -> Dict[str, Any]:
        """Fetch user positions/tokens for a specific outcome market."""
        target_user = (user or self.wallet_address).lower()
        return await self.post_info({
            "type": "userOutcome",
            "user": target_user,
            "outcome": int(outcome_id),
        })

    async def get_open_orders(self, user: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch active orders across every DEX, matching the official SDK."""
        target_user = (user or self.wallet_address).lower()
        return await self.post_info({"type": "frontendOpenOrders", "user": target_user, "dex": "ALL_DEXS"})

    def get_open_orders_sync(self, user: Optional[str] = None) -> List[Dict[str, Any]]:
        """Synchronously fetch all active frontend orders; read-only."""
        target_user = (user or self.wallet_address).lower()
        return self.post_info_sync({"type": "frontendOpenOrders", "user": target_user, "dex": "ALL_DEXS"})

    async def get_user_fills(self, user: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch recent user trade fills."""
        target_user = (user or self.wallet_address).lower()
        return await self.post_info({"type": "userFills", "user": target_user})

    def get_user_fills_sync(self, user: Optional[str] = None) -> List[Dict[str, Any]]:
        """Synchronously fetch recent user fills; this endpoint is read-only."""
        target_user = (user or self.wallet_address).lower()
        return self.post_info_sync({"type": "userFills", "user": target_user})

    # --------------------------------------------------------------------------
    # REST /exchange Endpoints
    # --------------------------------------------------------------------------

    async def post_exchange(self, action: Dict[str, Any], vault_address: Optional[str] = None) -> Any:
        """Sign and submit an L1 exchange action."""
        self.auth.require_agent_authorized()
        signed_payload = self.auth.sign_l1_action(action=action, vault_address=vault_address)
        client = await self.get_async_client()
        try:
            resp = await client.post("/exchange", json=signed_payload)
            resp.raise_for_status()
            data = resp.json()
            return data
        except Exception as e:
            logger.error(f"OutcomeClient.post_exchange error for action {action.get('type')}: {e}")
            raise

    def post_exchange_sync(self, action: Dict[str, Any], vault_address: Optional[str] = None) -> Any:
        """Synchronously sign and submit an L1 exchange action."""
        self.auth.require_agent_authorized()
        signed_payload = self.auth.sign_l1_action(action=action, vault_address=vault_address)
        client = self.get_sync_client()
        try:
            resp = client.post("/exchange", json=signed_payload)
            resp.raise_for_status()
            data = resp.json()
            return data
        except Exception as e:
            logger.error(f"OutcomeClient.post_exchange_sync error for action {action.get('type')}: {e}")
            raise

    async def submit_order(
        self,
        outcome_id: int,
        side_index: int,
        is_buy: bool,
        price: Union[float, Decimal, str],
        size: Union[float, Decimal, str],
        order_type: str = "Gtc",  # "Gtc", "Ioc", "Alo" (Post-Only)
        reduce_only: bool = False,
        cloid: Optional[str] = None,
        vault_address: Optional[str] = None,
        sz_decimals: int = 0,
    ) -> Dict[str, Any]:
        """
        Submit an outcome order to Hyperliquid L1.
        """
        asset_id = outcome_asset_id(outcome_id, side_index)
        aligned_price = align_outcome_price(price)
        aligned_size = align_outcome_size(size, sz_decimals=sz_decimals)
        order_cloid = cloid or generate_cloid()

        normalized_type = order_type.upper()
        if normalized_type in ("ALO", "POST_ONLY", "POSTONLY"):
            tif_str = "Alo"
        elif normalized_type in ("IOC", "FOK", "FAK"):
            tif_str = "Ioc"
        elif normalized_type in ("GTC", "GTD"):
            tif_str = "Gtc"
        else:
            raise ValueError(f"Unsupported Outcome time-in-force: {order_type!r}")

        order_wire = {
            "a": asset_id,
            "b": bool(is_buy),
            "p": aligned_price,
            "s": aligned_size,
            "r": bool(reduce_only),
            "t": {
                "limit": {
                    "tif": tif_str
                }
            },
            "c": order_cloid,
        }

        action = {
            "type": "order",
            "orders": [order_wire],
            "grouping": "na",
        }

        result = await self.post_exchange(action=action, vault_address=vault_address)
        normalized = self.normalize_order_result(result)
        return {
            "result": result,
            **normalized,
            "cloid": order_cloid,
            "asset_id": asset_id,
            "price": aligned_price,
            "size": aligned_size,
            "is_buy": is_buy,
            "side_index": side_index,
            "outcome_id": outcome_id,
        }

    @staticmethod
    def normalize_order_result(result: Any) -> Dict[str, Any]:
        """Expose the SDK-style success/status/error contract for raw API replies."""
        if not isinstance(result, dict):
            return {"success": False, "status": None, "order_id": None, "shares": None, "error": "invalid exchange response"}
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses or not isinstance(statuses[0], dict):
            return {"success": False, "status": None, "order_id": None, "shares": None, "error": str(result.get("error") or "missing order status")}
        status = statuses[0]
        if "error" in status:
            return {"success": False, "status": None, "order_id": None, "shares": None, "error": str(status["error"])}
        resting = status.get("resting")
        filled = status.get("filled")
        detail = resting or filled
        if not isinstance(detail, dict):
            return {"success": False, "status": None, "order_id": None, "shares": None, "error": "unrecognized order status"}
        return {
            "success": True,
            "status": "resting" if resting else "filled",
            "order_id": detail.get("oid"),
            "shares": detail.get("totalSz", detail.get("sz")),
            "error": None,
        }

    async def cancel_order(
        self,
        outcome_id: int,
        side_index: int,
        order_id: int,
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancel an order by venue order ID (oid)."""
        asset_id = outcome_asset_id(outcome_id, side_index)
        action = {
            "type": "cancel",
            "cancels": [
                {
                    "a": asset_id,
                    "o": int(order_id),
                }
            ]
        }
        return await self.post_exchange(action=action, vault_address=vault_address)

    async def cancel_by_cloid(
        self,
        outcome_id: int,
        side_index: int,
        cloid: str,
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancel an order by client order ID (cloid)."""
        asset_id = outcome_asset_id(outcome_id, side_index)
        action = {
            "type": "cancelByCloid",
            "cancels": [
                {
                    "asset": asset_id,
                    "cloid": cloid,
                }
            ]
        }
        return await self.post_exchange(action=action, vault_address=vault_address)

    async def split_outcome(
        self,
        outcome_id: int,
        amount: Union[float, Decimal, str],
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Split 1 USDC into 1 YES + 1 NO outcome tokens.
        """
        amount_str = str(float(amount))
        action = {
            "type": "userOutcome",
            "outcomeId": int(outcome_id),
            "action": "split",
            "amount": amount_str,
        }
        return await self.post_exchange(action=action, vault_address=vault_address)

    async def merge_outcome(
        self,
        outcome_id: int,
        amount: Union[float, Decimal, str],
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Merge 1 YES + 1 NO outcome tokens back into 1 USDC.
        """
        amount_str = str(float(amount))
        action = {
            "type": "userOutcome",
            "outcomeId": int(outcome_id),
            "action": "merge",
            "amount": amount_str,
        }
        return await self.post_exchange(action=action, vault_address=vault_address)

    # --------------------------------------------------------------------------
    # WebSocket Streaming
    # --------------------------------------------------------------------------

    def register_callback(self, channel: str, callback: Callable[[Dict[str, Any]], Any]) -> None:
        if channel not in self._callbacks:
            self._callbacks[channel] = []
        self._callbacks[channel].append(callback)

    def unregister_callback(self, channel: str, callback: Callable[[Dict[str, Any]], Any]) -> None:
        """Remove one listener, so a shadow recorder can safely roll markets."""
        callbacks = self._callbacks.get(channel)
        if not callbacks:
            return
        self._callbacks[channel] = [registered for registered in callbacks if registered != callback]
        if not self._callbacks[channel]:
            del self._callbacks[channel]

    async def start_ws(self) -> None:
        """Start the WebSocket listener loop in the background."""
        if self._ws_running:
            return
        self._ws_running = True
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop_ws(self) -> None:
        """Stop the WebSocket listener loop."""
        self._ws_running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

    async def subscribe_all_mids(self) -> None:
        sub = {"type": "allMids"}
        self._subscriptions["allMids"] = sub
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

    async def unsubscribe_all_mids(self) -> None:
        await self._unsubscribe("allMids")

    async def subscribe_l2_book(self, coin: str) -> None:
        sub = {"type": "l2Book", "coin": coin}
        self._subscriptions[f"l2Book:{coin}"] = sub
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

    async def unsubscribe_l2_book(self, coin: str) -> None:
        await self._unsubscribe(f"l2Book:{coin}")

    async def subscribe_trades(self, coin: str) -> None:
        """Subscribe to public outcome trades for one tradeable side coin."""
        sub = {"type": "trades", "coin": coin}
        self._subscriptions[f"trades:{coin}"] = sub
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

    async def unsubscribe_trades(self, coin: str) -> None:
        await self._unsubscribe(f"trades:{coin}")

    async def _unsubscribe(self, key: str) -> None:
        sub = self._subscriptions.pop(key, None)
        if sub and self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({"method": "unsubscribe", "subscription": sub}))

    def _dispatch_callback(self, channel: str, data: Dict[str, Any]) -> None:
        for cb in self._callbacks.get(channel, []):
            try:
                result = cb(data)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as cb_err:
                logger.error(f"Error in WS callback for {channel}: {cb_err}")

    async def subscribe_user_events(self, user: Optional[str] = None) -> None:
        target_user = (user or self.wallet_address).lower()
        sub = {"type": "userEvents", "user": target_user}
        self._subscriptions[f"userEvents:{target_user}"] = sub
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

    async def _ws_loop(self) -> None:
        reconnect_attempt = 0
        while self._ws_running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    reconnect_attempt = 0
                    logger.info(f"OutcomeClient connected to WebSocket: {self.ws_url}")
                    self._dispatch_callback("__lifecycle__", {"event": "connected", "received_at_ms": int(time.time() * 1000)})
                    # Resubscribe to all active topics
                    for sub in self._subscriptions.values():
                        await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

                    while self._ws_running:
                        message = await ws.recv()
                        data = json.loads(message)
                        channel = data.get("channel")
                        if channel:
                            self._dispatch_callback(channel, data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._ws_running:
                    break
                self._dispatch_callback("__lifecycle__", {
                    "event": "disconnected", "received_at_ms": int(time.time() * 1000), "error": str(e),
                })
                reconnect_attempt += 1
                if reconnect_attempt > 10:
                    self._dispatch_callback("__lifecycle__", {
                        "event": "reconnect_exhausted", "received_at_ms": int(time.time() * 1000),
                        "error": str(e),
                    })
                    self._ws_running = False
                    break
                delay_sec = min(30.0, float(2 ** (reconnect_attempt - 1)))
                logger.warning(
                    f"OutcomeClient WebSocket disconnected ({e}), reconnecting in {delay_sec:.0f}s "
                    f"(attempt {reconnect_attempt}/10)..."
                )
                await asyncio.sleep(delay_sec)
