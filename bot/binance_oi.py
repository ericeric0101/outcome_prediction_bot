"""Read-only Binance USDⓈ-M BTC open-interest collection for Outcome 1d research.

The module deliberately contains no authenticated Binance client or order
method.  It stores event-time provenance so later X3 joins cannot turn a REST
backfill into artificial low-latency alpha.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

import httpx

from monitoring.trade_journal_db import TradeJournalDB


BINANCE_USDM_BASE_URL = "https://fapi.binance.com"
BINANCE_USDM_SOURCE = "binance_usdm_public"
BINANCE_OI_SCHEMA_VERSION = 1
CURRENT_OI_ENDPOINT = "/fapi/v1/openInterest"
HISTORICAL_OI_ENDPOINT = "/futures/data/openInterestHist"
PREMIUM_INDEX_ENDPOINT = "/fapi/v1/premiumIndex"
AGG_TRADES_ENDPOINT = "/fapi/v1/aggTrades"


def _decimal_text(value: Any, *, field: str, positive: bool = False) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Binance {field} is not a decimal: {value!r}") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise ValueError(f"Binance {field} must be {'positive ' if positive else ''}finite: {value!r}")
    return format(parsed, "f")


def _integer(value: Any, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Binance {field} is not an integer: {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"Binance {field} must be positive: {value!r}")
    return parsed


def payload_hash(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class BinanceOiObservation:
    """One current or historical OI point with explicit time provenance."""

    symbol: str
    exchange_timestamp_ms: int
    local_received_at_ms: int
    request_latency_ms: float
    open_interest: str
    endpoint: str
    raw_payload: Mapping[str, Any]
    backfilled: bool
    open_interest_value: Optional[str] = None
    mark_price: Optional[str] = None
    index_price: Optional[str] = None
    taker_buy_notional: Optional[str] = None
    taker_sell_notional: Optional[str] = None
    taker_imbalance: Optional[float] = None
    context: Mapping[str, Any] | None = None

    @property
    def raw_payload_hash(self) -> str:
        return payload_hash(self.raw_payload)

    @classmethod
    def from_current_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        local_received_at_ms: int,
        request_latency_ms: float,
        context: Mapping[str, Any] | None = None,
    ) -> "BinanceOiObservation":
        symbol = str(payload.get("symbol", "")).upper()
        if symbol != "BTCUSDT":
            raise ValueError(f"expected Binance BTCUSDT OI, received {symbol or '<missing>'}")
        return cls(
            symbol=symbol,
            exchange_timestamp_ms=_integer(payload.get("time"), field="openInterest.time"),
            local_received_at_ms=int(local_received_at_ms),
            request_latency_ms=float(request_latency_ms),
            open_interest=_decimal_text(payload.get("openInterest"), field="openInterest", positive=True),
            endpoint=CURRENT_OI_ENDPOINT,
            raw_payload=dict(payload),
            backfilled=False,
            context=dict(context or {}),
        )

    @classmethod
    def from_historical_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        local_received_at_ms: int,
        request_latency_ms: float,
    ) -> "BinanceOiObservation":
        symbol = str(payload.get("symbol", "")).upper()
        if symbol != "BTCUSDT":
            raise ValueError(f"expected Binance BTCUSDT historical OI, received {symbol or '<missing>'}")
        value = payload.get("sumOpenInterestValue")
        return cls(
            symbol=symbol,
            exchange_timestamp_ms=_integer(payload.get("timestamp"), field="openInterestHist.timestamp"),
            local_received_at_ms=int(local_received_at_ms),
            request_latency_ms=float(request_latency_ms),
            open_interest=_decimal_text(payload.get("sumOpenInterest"), field="sumOpenInterest", positive=True),
            open_interest_value=(
                _decimal_text(value, field="sumOpenInterestValue", positive=True)
                if value is not None else None
            ),
            endpoint=HISTORICAL_OI_ENDPOINT,
            raw_payload=dict(payload),
            backfilled=True,
            context={"period": "5m", "schema_version": BINANCE_OI_SCHEMA_VERSION},
        )


class BinanceUsdMFuturesPublicClient:
    """Small retrying public-only REST client; no key is accepted or needed."""

    def __init__(self, *, base_url: str = BINANCE_USDM_BASE_URL, timeout_sec: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self._client: Optional[httpx.Client] = None

    def _http(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(self.timeout_sec, connect=5.0))
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()
        self._client = None

    def get_json(self, endpoint: str, *, params: Mapping[str, Any], max_retries: int = 3) -> tuple[Any, float, int]:
        last_error: Optional[BaseException] = None
        for attempt in range(max_retries):
            started = time.monotonic()
            try:
                response = self._http().get(endpoint, params=dict(params))
                response.raise_for_status()
                return response.json(), (time.monotonic() - started) * 1000.0, int(time.time() * 1000)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < max_retries:
                    time.sleep(min(4.0, float(2 ** attempt)))
        assert last_error is not None
        raise RuntimeError(f"Binance public request failed for {endpoint}: {last_error}") from last_error

    def current_open_interest(self, *, symbol: str = "BTCUSDT") -> tuple[Mapping[str, Any], float, int]:
        payload, latency, received = self.get_json(CURRENT_OI_ENDPOINT, params={"symbol": symbol})
        if not isinstance(payload, Mapping):
            raise ValueError("Binance current OI payload must be an object")
        return payload, latency, received

    def historical_open_interest(self, *, symbol: str = "BTCUSDT", period: str = "5m", limit: int = 500) -> tuple[tuple[Mapping[str, Any], ...], float, int]:
        payload, latency, received = self.get_json(HISTORICAL_OI_ENDPOINT, params={"symbol": symbol, "period": period, "limit": limit})
        if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
            raise ValueError("Binance historical OI payload must be an array of objects")
        return tuple(payload), latency, received

    def premium_index(self, *, symbol: str = "BTCUSDT") -> tuple[Mapping[str, Any], float, int]:
        payload, latency, received = self.get_json(PREMIUM_INDEX_ENDPOINT, params={"symbol": symbol})
        if not isinstance(payload, Mapping):
            raise ValueError("Binance premiumIndex payload must be an object")
        return payload, latency, received

    def aggregate_trades(self, *, symbol: str = "BTCUSDT", limit: int = 100) -> tuple[tuple[Mapping[str, Any], ...], float, int]:
        payload, latency, received = self.get_json(AGG_TRADES_ENDPOINT, params={"symbol": symbol, "limit": limit})
        if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
            raise ValueError("Binance aggTrades payload must be an array of objects")
        return tuple(payload), latency, received


def build_current_context(
    premium_index: Mapping[str, Any],
    aggregate_trades: tuple[Mapping[str, Any], ...],
    *,
    premium_request_latency_ms: float,
    trades_request_latency_ms: float,
) -> dict[str, Any]:
    """Extract only context required to interpret an OI move; retain raw payloads."""
    symbol = str(premium_index.get("symbol", "")).upper()
    if symbol != "BTCUSDT":
        raise ValueError(f"expected BTCUSDT premiumIndex, received {symbol or '<missing>'}")
    mark_price = _decimal_text(premium_index.get("markPrice"), field="markPrice", positive=True)
    index_price = _decimal_text(premium_index.get("indexPrice"), field="indexPrice", positive=True)
    buy_notional = Decimal("0")
    sell_notional = Decimal("0")
    valid_trades = 0
    for trade in aggregate_trades:
        try:
            notional = Decimal(str(trade["p"])) * Decimal(str(trade["q"]))
            if notional <= 0:
                continue
            # m=true means buyer was maker, therefore the aggressor sold.
            if bool(trade.get("m")):
                sell_notional += notional
            else:
                buy_notional += notional
            valid_trades += 1
        except (KeyError, InvalidOperation, TypeError, ValueError):
            continue
    total = buy_notional + sell_notional
    imbalance = float((buy_notional - sell_notional) / total) if total > 0 else None
    return {
        "schema_version": BINANCE_OI_SCHEMA_VERSION,
        "premium_index": dict(premium_index),
        "aggregate_trades": [dict(item) for item in aggregate_trades],
        "mark_price": mark_price,
        "index_price": index_price,
        "taker_buy_notional": format(buy_notional, "f"),
        "taker_sell_notional": format(sell_notional, "f"),
        "taker_imbalance": imbalance,
        "valid_aggregate_trade_count": valid_trades,
        "premium_request_latency_ms": premium_request_latency_ms,
        "trades_request_latency_ms": trades_request_latency_ms,
    }


class BinanceOiCollector:
    """Formal read-only collector with deduped current and historical observations."""

    def __init__(self, *, journal: TradeJournalDB, run_id: str, client: BinanceUsdMFuturesPublicClient, symbol: str = "BTCUSDT") -> None:
        self.journal = journal
        self.run_id = run_id
        self.client = client
        self.symbol = symbol.upper()

    def _write(self, observation: BinanceOiObservation) -> bool:
        context = dict(observation.context or {})
        return self.journal.record_binance_oi_observation(
            run_id=self.run_id,
            source=BINANCE_USDM_SOURCE,
            endpoint=observation.endpoint,
            symbol=observation.symbol,
            exchange_timestamp_ms=observation.exchange_timestamp_ms,
            local_received_at_ms=observation.local_received_at_ms,
            request_latency_ms=observation.request_latency_ms,
            open_interest=observation.open_interest,
            open_interest_value=observation.open_interest_value,
            mark_price=str(context.get("mark_price")) if context.get("mark_price") is not None else observation.mark_price,
            index_price=str(context.get("index_price")) if context.get("index_price") is not None else observation.index_price,
            taker_buy_notional=str(context.get("taker_buy_notional")) if context.get("taker_buy_notional") is not None else observation.taker_buy_notional,
            taker_sell_notional=str(context.get("taker_sell_notional")) if context.get("taker_sell_notional") is not None else observation.taker_sell_notional,
            taker_imbalance=context.get("taker_imbalance", observation.taker_imbalance),
            backfilled=observation.backfilled,
            raw_payload_hash=observation.raw_payload_hash,
            raw_payload=dict(observation.raw_payload),
            context=context,
        )

    def backfill_5m(self, *, limit: int = 500) -> int:
        if not 1 <= limit <= 500:
            raise ValueError("Binance OI historical limit must be in [1, 500]")
        payloads, latency, received = self.client.historical_open_interest(symbol=self.symbol, period="5m", limit=limit)
        written = 0
        for payload in payloads:
            observation = BinanceOiObservation.from_historical_payload(payload, local_received_at_ms=received, request_latency_ms=latency)
            written += int(self._write(observation))
        self.journal.log_strategy_event(self.run_id, "BINANCE_OI_BACKFILL", {
            "source": BINANCE_USDM_SOURCE, "symbol": self.symbol, "period": "5m",
            "requested_limit": limit, "returned": len(payloads), "written": written,
            "backfilled": True, "read_only": True,
        })
        return written

    def collect_current(self) -> bool:
        oi_payload, oi_latency, oi_received = self.client.current_open_interest(symbol=self.symbol)
        premium_payload, premium_latency, _ = self.client.premium_index(symbol=self.symbol)
        trades_payload, trades_latency, _ = self.client.aggregate_trades(symbol=self.symbol)
        context = build_current_context(
            premium_payload, trades_payload,
            premium_request_latency_ms=premium_latency, trades_request_latency_ms=trades_latency,
        )
        observation = BinanceOiObservation.from_current_payload(
            oi_payload, local_received_at_ms=oi_received, request_latency_ms=oi_latency, context=context,
        )
        written = self._write(observation)
        self.journal.log_strategy_event(self.run_id, "BINANCE_OI_CURRENT", {
            "source": BINANCE_USDM_SOURCE, "symbol": self.symbol,
            "exchange_timestamp_ms": observation.exchange_timestamp_ms,
            "local_received_at_ms": observation.local_received_at_ms,
            "request_latency_ms": observation.request_latency_ms,
            "written": written, "read_only": True,
            "context_status": "complete",
        })
        return written
