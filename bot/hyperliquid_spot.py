"""Hyperliquid native spot market identities and guarded testnet execution.

This is deliberately independent from the HIP-4 Outcome order lifecycle.  No
mainnet mutation is supported by this module; the only mutation adapter refuses
to initialize unless both Hyperliquid testnet and an explicit operator flag
are present.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping
from urllib.parse import urlparse

from bot.adapters.outcome_auth import OutcomeAuth, generate_cloid


@dataclass(frozen=True)
class SpotMarket:
    pair: str
    coin: str
    index: int
    asset_id: int
    base_token: str
    base_token_id: int
    quote_token: str
    quote_token_id: int
    sz_decimals: int


def resolve_btc_usdc_spot(meta: Mapping[str, Any]) -> SpotMarket:
    """Resolve BTC/USDC from venue metadata; do not hardcode mainnet IDs."""
    tokens = meta.get("tokens")
    universe = meta.get("universe")
    if not isinstance(tokens, list) or not isinstance(universe, list):
        raise ValueError("spotMeta must contain tokens and universe arrays")
    by_index = {int(t["index"]): t for t in tokens if isinstance(t, Mapping) and "index" in t}
    matches: list[SpotMarket] = []
    for pair in universe:
        if not isinstance(pair, Mapping):
            continue
        indices = pair.get("tokens")
        if not isinstance(indices, list) or len(indices) != 2:
            continue
        base, quote = by_index.get(int(indices[0])), by_index.get(int(indices[1]))
        if not base or not quote or str(quote.get("name", "")).upper() != "USDC":
            continue
        base_name = str(base.get("name", "")).upper()
        # HyperCore currently names the app's BTC spot token UBTC. Accept BTC
        # only when metadata explicitly identifies it as the base token.
        if base_name not in {"BTC", "UBTC"}:
            continue
        index = int(pair.get("index", -1))
        if index < 0:
            continue
        sz_decimals = int(base.get("szDecimals", -1))
        if not 0 <= sz_decimals <= 8:
            raise ValueError("BTC spot metadata has invalid szDecimals")
        matches.append(SpotMarket(
            pair=str(pair.get("name") or f"{base_name}/USDC"), coin=f"@{index}",
            index=index, asset_id=10_000 + index, base_token=base_name,
            base_token_id=int(indices[0]), quote_token="USDC", quote_token_id=int(indices[1]),
            sz_decimals=sz_decimals,
        ))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one BTC/USDC spot market, found {len(matches)}")
    return matches[0]


def _round_significant(value: Decimal, digits: int = 5) -> Decimal:
    if value <= 0:
        raise ValueError("price must be positive")
    if value == value.to_integral_value():
        return value
    exponent = value.adjusted() - digits + 1
    quantum = Decimal(1).scaleb(exponent)
    return value.quantize(quantum, rounding=ROUND_DOWN)


def align_spot_price(price: Decimal, *, sz_decimals: int) -> Decimal:
    """Apply Hyperliquid spot's 5-significant-figure / 8-szDecimals rules."""
    if not 0 <= sz_decimals <= 8:
        raise ValueError("invalid spot szDecimals")
    max_dp = 8 - sz_decimals
    by_decimals = price.quantize(Decimal(1).scaleb(-max_dp), rounding=ROUND_DOWN)
    return _round_significant(by_decimals)


def align_spot_size(size: Decimal, *, sz_decimals: int) -> Decimal:
    if size <= 0 or not 0 <= sz_decimals <= 8:
        raise ValueError("invalid spot size or szDecimals")
    return size.quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_DOWN)


class HyperliquidSpotClient:
    """Small spot-only REST adapter; it never calls Outcome order methods."""

    def __init__(self, auth: OutcomeAuth, *, execution_enabled: bool = False,
                 timeout_sec: float = 4.0, transport: Any | None = None) -> None:
        self.auth = auth
        self.execution_enabled = bool(execution_enabled)
        if self.execution_enabled and not auth.is_testnet:
            raise RuntimeError("BTC spot mutations are testnet-only; mainnet execution is unsupported")
        if self.execution_enabled and urlparse(str(auth.base_url)).hostname != "api.hyperliquid-testnet.xyz":
            raise RuntimeError("BTC spot execution must use the official Hyperliquid testnet API host")
        if self.execution_enabled:
            auth.require_agent_authorized()
        import httpx
        self._client = httpx.Client(
            base_url=auth.base_url, timeout=httpx.Timeout(timeout_sec, connect=2.0),
            headers={"Content-Type": "application/json"}, transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def post_info(self, payload: Mapping[str, Any]) -> Any:
        response = self._client.post("/info", json=dict(payload))
        response.raise_for_status()
        return response.json()

    def spot_meta(self) -> Mapping[str, Any]:
        return self.post_info({"type": "spotMeta"})

    def l2_book(self, coin: str) -> Mapping[str, Any]:
        return self.post_info({"type": "l2Book", "coin": coin})

    def spot_state(self, wallet: str | None = None) -> Mapping[str, Any]:
        return self.post_info({"type": "spotClearinghouseState", "user": (wallet or self.auth.wallet_address).lower()})

    def open_orders(self, wallet: str | None = None) -> list[Mapping[str, Any]]:
        return self.post_info({"type": "frontendOpenOrders", "user": (wallet or self.auth.wallet_address).lower(), "dex": "ALL_DEXS"})

    def _exchange(self, action: Mapping[str, Any]) -> Any:
        if not self.execution_enabled or not self.auth.is_testnet:
            raise RuntimeError("spot exchange mutation blocked: explicit testnet execution is not enabled")
        payload = self.auth.sign_l1_action(dict(action))
        response = self._client.post("/exchange", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, Mapping) or result.get("status") != "ok":
            raise RuntimeError("Hyperliquid spot exchange returned a non-ok response")
        response_data = result.get("response")
        data = response_data.get("data", {}) if isinstance(response_data, Mapping) else {}
        statuses = data.get("statuses", []) if isinstance(data, Mapping) else []
        if any(isinstance(row, Mapping) and "error" in row for row in statuses):
            raise RuntimeError("Hyperliquid spot exchange rejected the order action")
        return result


@dataclass(frozen=True)
class SpotRiskLimits:
    max_order_notional_usdc: Decimal = Decimal("10")
    max_position_notional_usdc: Decimal = Decimal("10")
    max_open_orders: int = 1


class BTCSpotExecutionService:
    """Explicit, bounded ALO-only testnet service; not wired to live signals."""

    def __init__(self, *, client: HyperliquidSpotClient, journal: Any,
                 run_id: str = "btc-spot-testnet", limits: SpotRiskLimits = SpotRiskLimits(),
                 clock_ms: Any = lambda: int(time.time() * 1000)) -> None:
        if not client.auth.is_testnet or not client.execution_enabled:
            raise RuntimeError("BTCSpotExecutionService requires explicit testnet execution")
        if journal is None:
            raise RuntimeError("BTC spot mutation requires the existing durable journal")
        self.client, self.journal, self.run_id = client, journal, run_id
        self.limits, self.clock_ms = limits, clock_ms
        self._mutation_lock = threading.Lock()
        self._ambiguous = self._has_unresolved_durable_intent()

    def _has_unresolved_durable_intent(self) -> bool:
        """Rebuild a fail-closed mutation fence from durable spot intents."""
        wallet = str(getattr(self.client.auth, "wallet_address", "")).lower()
        journal_check = getattr(self.journal, "has_unresolved_spot_testnet_intent", None)
        if not wallet or not callable(journal_check):
            # Production TradeJournalDB and OutcomeAuth always provide these;
            # if a custom adapter omits either, do not grant mutation authority.
            return True
        try:
            return bool(journal_check(wallet))
        except Exception:
            return True

    @staticmethod
    def _balances(state: Mapping[str, Any]) -> dict[str, tuple[Decimal, Decimal]]:
        result: dict[str, tuple[Decimal, Decimal]] = {}
        for row in state.get("balances", []):
            if isinstance(row, Mapping) and row.get("coin"):
                total = Decimal(str(row.get("total", "0")))
                hold = Decimal(str(row.get("hold", "0")))
                result[str(row["coin"]).upper()] = (total, max(Decimal("0"), total - hold))
        return result

    def submit_alo(self, *, market: SpotMarket, is_buy: bool, price: Decimal, size: Decimal) -> Any:
        """Submit one post-only limit order after fresh balance/order checks.

        Any transport exception permanently fences this service instance. A
        caller must reconcile through a fresh process/account-truth workflow;
        the method never retries an ambiguous mutation.
        """
        with self._mutation_lock:
            if self._ambiguous:
                raise RuntimeError("spot mutation fenced after ambiguous exchange response")
            px = align_spot_price(Decimal(price), sz_decimals=market.sz_decimals)
            qty = align_spot_size(Decimal(size), sz_decimals=market.sz_decimals)
            notional = px * qty
            if qty <= 0 or notional <= 0 or notional > self.limits.max_order_notional_usdc:
                raise ValueError("order exceeds the bounded spot order-notional limit")
            state = self.client.spot_state()
            balances = self._balances(state)
            open_orders = [o for o in self.client.open_orders() if str(o.get("coin", "")) == market.coin]
            if len(open_orders) >= self.limits.max_open_orders:
                raise RuntimeError("spot open-order limit reached; reconcile before another mutation")
            base_total, base_available = balances.get(market.base_token, (Decimal("0"), Decimal("0")))
            if is_buy:
                _quote_total, quote_available = balances.get("USDC", (Decimal("0"), Decimal("0")))
                if quote_available < notional:
                    raise RuntimeError("insufficient official USDC spot balance")
            elif qty > base_available:
                raise RuntimeError("sell size exceeds official available BTC spot balance")
            book = self.client.l2_book(market.coin)
            levels = book.get("levels", [[], []])
            bids = levels[0] if isinstance(levels, list) and levels else []
            asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else []
            if not bids or not asks:
                raise RuntimeError("fresh BTC spot book unavailable")
            try:
                book_age_ms = int(time.time() * 1000) - int(book.get("time"))
            except (TypeError, ValueError):
                raise RuntimeError("spot book has no authoritative timestamp")
            if book_age_ms < -1_000 or book_age_ms > 5_000:
                raise RuntimeError("spot book is stale or has an invalid future timestamp")
            best_bid, best_ask = Decimal(str(bids[0]["px"])), Decimal(str(asks[0]["px"]))
            if best_bid <= 0 or best_ask < best_bid or (is_buy and px >= best_ask) or (not is_buy and px <= best_bid):
                raise RuntimeError("price is crossed/stale; only non-marketable ALO prices are allowed")
            if is_buy and (base_total + qty) * max(px, best_ask) > self.limits.max_position_notional_usdc:
                raise RuntimeError("spot position-notional limit would be exceeded")
            cloid = generate_cloid()
            action = {"type": "order", "orders": [{
                "a": market.asset_id, "b": bool(is_buy), "p": format(px.normalize(), "f"),
                "s": format(qty.normalize(), "f"), "r": False, "c": cloid,
                "t": {"limit": {"tif": "Alo"}},
            }], "grouping": "na"}
            intent_id = self.journal.log_durable_strategy_event(self.run_id, "BTC_SPOT_TESTNET_ORDER_INTENT", {
                "wallet": str(self.client.auth.wallet_address).lower(),
                "market_pair": market.pair, "coin": market.coin, "asset_id": market.asset_id,
                "side": "BUY" if is_buy else "SELL", "price": str(px), "size": str(qty),
                "notional_usdc": str(notional), "cloid": cloid, "tif": "Alo",
                "testnet": True, "live_authority": False,
            })
            if intent_id is None:
                raise RuntimeError("durable BTC spot order intent unavailable; mutation blocked")
            try:
                result = self.client._exchange(action)
                # The exchange response is not final identity evidence. Adopt
                # only one fresh account-truth row carrying our exact cloid.
                matches = [o for o in self.client.open_orders() if str(o.get("coin", "")) == market.coin
                           and str(o.get("cloid", "")).lower() == cloid.lower()]
                if len(matches) != 1:
                    raise RuntimeError(f"submitted spot order not uniquely visible in account truth ({len(matches)} matches)")
                oid = int(matches[0]["oid"])
                ack_id = self.journal.log_durable_strategy_event(self.run_id, "BTC_SPOT_TESTNET_ORDER_CONFIRMED", {
                    "wallet": str(self.client.auth.wallet_address).lower(),
                    "intent_event_id": intent_id, "market_pair": market.pair, "coin": market.coin,
                    "asset_id": market.asset_id, "cloid": cloid, "venue_order_id": oid,
                    "account_truth_match_count": 1, "testnet": True, "live_authority": False,
                })
                if ack_id is None:
                    raise RuntimeError("durable BTC spot acknowledgement unavailable")
            except Exception:
                self._ambiguous = True
                raise
            return {"exchange_response": result, "venue_order_id": oid, "cloid": cloid}

    def cancel_owned_order(self, *, market: SpotMarket, order_id: int) -> Any:
        """Cancel a unique matching spot order and confirm absence by account truth."""
        with self._mutation_lock:
            if self._ambiguous:
                raise RuntimeError("spot mutation fenced after ambiguous exchange response")
            open_orders = self.client.open_orders()
            matches = [o for o in open_orders if str(o.get("coin", "")) == market.coin
                       and str(o.get("oid", "")) == str(order_id)]
            if len(matches) != 1:
                raise RuntimeError(f"spot cancel requires one exact account-truth match; got {len(matches)}")
            intent_id = self.journal.log_durable_strategy_event(self.run_id, "BTC_SPOT_TESTNET_CANCEL_INTENT", {
                "wallet": str(self.client.auth.wallet_address).lower(),
                "market_pair": market.pair, "coin": market.coin, "asset_id": market.asset_id,
                "venue_order_id": int(order_id), "cloid": matches[0].get("cloid"),
                "testnet": True, "live_authority": False,
            })
            if intent_id is None:
                raise RuntimeError("durable BTC spot cancel intent unavailable; mutation blocked")
            try:
                result = self.client._exchange({
                    "type": "cancel", "cancels": [{"a": market.asset_id, "o": int(order_id)}],
                })
                remaining = [o for o in self.client.open_orders() if str(o.get("oid", "")) == str(order_id)]
                if remaining:
                    self._ambiguous = True
                    raise RuntimeError("spot cancel not confirmed absent in fresh account truth")
                final_balances = self._balances(self.client.spot_state())
                ack_id = self.journal.log_durable_strategy_event(self.run_id, "BTC_SPOT_TESTNET_CANCEL_CONFIRMED", {
                    "wallet": str(self.client.auth.wallet_address).lower(),
                    "intent_event_id": intent_id, "market_pair": market.pair, "coin": market.coin,
                    "asset_id": market.asset_id, "venue_order_id": int(order_id),
                    "testnet": True, "account_truth_absent": True,
                    "final_base_total": str(final_balances.get(market.base_token, (Decimal("0"), Decimal("0")))[0]),
                    "final_quote_total": str(final_balances.get("USDC", (Decimal("0"), Decimal("0")))[0]),
                    "live_authority": False,
                })
                if ack_id is None:
                    self._ambiguous = True
                    raise RuntimeError("durable BTC spot cancel confirmation unavailable")
                return result
            except Exception:
                self._ambiguous = True
                raise


def build_btc_spot_testnet_service(auth: OutcomeAuth, *, journal: Any,
                                   environ: Mapping[str, str] | None = None) -> BTCSpotExecutionService:
    """Construct the bounded spot service only from explicit testnet config."""
    import os
    env = environ if environ is not None else os.environ
    enabled = str(env.get("BTC_SPOT_TESTNET_EXECUTION_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        raise RuntimeError("BTC_SPOT_TESTNET_EXECUTION_ENABLED is not explicitly enabled")
    if not auth.is_testnet:
        raise RuntimeError("refusing BTC spot service: HL_TESTNET must be enabled")
    limits = SpotRiskLimits(
        max_order_notional_usdc=Decimal(str(env.get("BTC_SPOT_MAX_ORDER_NOTIONAL_USDC", "10"))),
        max_position_notional_usdc=Decimal(str(env.get("BTC_SPOT_MAX_POSITION_NOTIONAL_USDC", "10"))),
        max_open_orders=1,
    )
    if limits.max_order_notional_usdc <= 0 or limits.max_position_notional_usdc <= 0:
        raise ValueError("BTC spot notional limits must be positive")
    client = HyperliquidSpotClient(auth, execution_enabled=True)
    return BTCSpotExecutionService(client=client, journal=journal, limits=limits)
