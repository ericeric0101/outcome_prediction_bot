"""
Hyperliquid Outcome (HIP-4) Market Discovery, Specification Parsing, and Lifecycle Manager.

Handles:
- Parsing of Outcome market specification strings:
  `class:priceBinary|underlying:BTC|expiry:20260823-1015|targetPrice:78213|period:15m`
- Extraction of strike target price, expiry epoch, side asset IDs
- Active / upcoming 15m BTC market discovery from outcomeMeta
- State machine: WAITING -> ACTIVE -> REDUCE_ONLY -> SETTLING -> SETTLED
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from bot.adapters.outcome_auth import outcome_asset_id
from bot.enums import MarketPhase


@dataclass(frozen=True)
class OutcomeMarketSpec:
    """
    Parsed specification for a Hyperliquid Outcome (HIP-4) Market.
    """
    outcome_id: int
    coin_name: str           # e.g. "@516"
    yes_coin: str            # e.g. "#5160"
    no_coin: str             # e.g. "#5161"
    yes_asset_id: int        # e.g. 100005160
    no_asset_id: int         # e.g. 100005161
    market_class: str        # e.g. "priceBinary"
    underlying: str          # e.g. "BTC"
    expiry_str: str          # e.g. "20260823-1015"
    expiry_timestamp: int    # Unix epoch timestamp in seconds
    start_timestamp: int     # Unix epoch timestamp in seconds
    target_price: Decimal    # Strike price from spec
    period: str              # e.g. "15m"
    raw_spec: str
    sz_decimals: int = 0
    max_leverage: int = 1
    side_names: tuple[str, str] = ("Yes", "No")
    raw_meta: Optional[Dict[str, Any]] = None

    @property
    def strike(self) -> Decimal:
        """Alias for target_price."""
        return self.target_price

    def time_to_expiry_sec(self, current_timestamp: Optional[int] = None) -> float:
        now = current_timestamp if current_timestamp is not None else int(time.time())
        return float(self.expiry_timestamp - now)

    def is_expired(self, current_timestamp: Optional[int] = None) -> bool:
        return self.time_to_expiry_sec(current_timestamp) <= 0.0

    def side_name(self, side_index: int) -> str:
        if side_index not in (0, 1):
            raise ValueError("side_index must be 0 or 1")
        return self.side_names[side_index]


def parse_expiry_string_to_timestamp(expiry_str: str) -> int:
    """
    Parses 'YYYYMMDD-HHMM' or 'YYYYMMDD_HHMM' UTC string to unix timestamp.
    Example: '20260823-1015' -> 2026-08-23 10:15:00 UTC
    """
    cleaned = expiry_str.strip().replace("_", "-")
    dt = datetime.strptime(cleaned, "%Y%m%d-%H%M").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def parse_outcome_market_spec(universe_item: Dict[str, Any]) -> Optional[OutcomeMarketSpec]:
    """
    Parse an outcome universe item from outcomeMeta into OutcomeMarketSpec.
    Supports both outcomeMeta format:
      {'outcome': 1145, 'name': 'Recurring', 'description': 'class:priceBinary|underlying:BTC|expiry:20260823-0600|targetPrice:77431|period:1d', ...}
    and universe format:
      {'name': '@516', 'description': 'class:priceBinary|...'}
    """
    outcome_id = None
    if "outcome" in universe_item:
        try:
            outcome_id = int(universe_item["outcome"])
        except (ValueError, TypeError):
            pass
    elif "outcomeId" in universe_item:
        try:
            outcome_id = int(universe_item["outcomeId"])
        except (ValueError, TypeError):
            pass

    if outcome_id is None:
        name = str(universe_item.get("name", "")).strip()
        if not name:
            return None
        outcome_id_str = name.lstrip("@#")
        try:
            outcome_id = int(outcome_id_str)
        except ValueError:
            return None

    # Get description / spec text
    spec_text = (
        universe_item.get("description")
        or universe_item.get("spec")
        or universe_item.get("title")
        or ""
    ).strip()

    if not spec_text:
        return None

    # Parse pipe-delimited key:value pairs
    parts = spec_text.split("|")
    kv_map: Dict[str, str] = {}
    for part in parts:
        if ":" in part:
            k, v = part.split(":", 1)
            kv_map[k.strip()] = v.strip()

    market_class = kv_map.get("class", "")
    underlying = kv_map.get("underlying", "").upper()
    expiry_str = kv_map.get("expiry", "")
    target_price_str = kv_map.get("targetPrice", "")
    period = kv_map.get("period", "").lower()

    if not (underlying and expiry_str and target_price_str):
        return None

    try:
        expiry_ts = parse_expiry_string_to_timestamp(expiry_str)
    except Exception as e:
        logger.debug(f"Failed to parse expiry {expiry_str}: {e}")
        return None

    try:
        target_price = Decimal(target_price_str)
    except Exception as e:
        logger.debug(f"Failed to parse targetPrice {target_price_str}: {e}")
        return None

    # Period duration in seconds
    period_seconds = 900  # default 15m
    if period.endswith("m"):
        try:
            period_seconds = int(period[:-1]) * 60
        except ValueError:
            pass
    elif period.endswith("h"):
        try:
            period_seconds = int(period[:-1]) * 3600
        except ValueError:
            pass
    elif period.endswith("d") or period == "daily" or period == "1d":
        try:
            days = int(period[:-1]) if period.endswith("d") else 1
            period_seconds = days * 86400
        except ValueError:
            period_seconds = 86400

    start_ts = expiry_ts - period_seconds
    # HIP-4 outcomeMeta does not expose spot/perp-style size decimals.  Live
    # Outcome orders use whole shares; do not inherit a fictional 0.1 step.
    sz_decimals = 0
    max_leverage = int(universe_item.get("maxLeverage", 1))
    side_specs = universe_item.get("sideSpecs") or []
    parsed_side_names = tuple(
        str(item.get("name", "")).strip() for item in side_specs if isinstance(item, dict)
    )
    side_names: tuple[str, str] = (
        (parsed_side_names[0], parsed_side_names[1])
        if len(parsed_side_names) == 2 and all(parsed_side_names)
        else ("Yes", "No")
    )

    coin_name = f"@{outcome_id}"
    yes_coin = f"#{outcome_id}0"
    no_coin = f"#{outcome_id}1"
    yes_asset_id = outcome_asset_id(outcome_id, 0)
    no_asset_id = outcome_asset_id(outcome_id, 1)

    return OutcomeMarketSpec(
        outcome_id=outcome_id,
        coin_name=coin_name,
        yes_coin=yes_coin,
        no_coin=no_coin,
        yes_asset_id=yes_asset_id,
        no_asset_id=no_asset_id,
        market_class=market_class,
        underlying=underlying,
        expiry_str=expiry_str,
        expiry_timestamp=expiry_ts,
        start_timestamp=start_ts,
        target_price=target_price,
        period=period,
        raw_spec=spec_text,
        sz_decimals=sz_decimals,
        max_leverage=max_leverage,
        side_names=side_names,
        raw_meta=dict(universe_item),
    )


def discover_btc_markets(
    outcome_meta: Dict[str, Any],
    current_timestamp: Optional[int] = None,
    allowed_periods: Tuple[str, ...] = ("15m", "1d", "24h", "1h", "daily"),
    preferred_period: Optional[str] = None,
) -> List[OutcomeMarketSpec]:
    """
    Discover all BTC markets (15m, 1d, 24h, 1h, daily) from outcomeMeta payload, sorted by expiry timestamp.
    """
    now = current_timestamp if current_timestamp is not None else int(time.time())
    items = outcome_meta.get("outcomes") or outcome_meta.get("universe") or []
    markets: List[OutcomeMarketSpec] = []

    for item in items:
        spec = parse_outcome_market_spec(item)
        if spec is None:
            continue
        if spec.underlying == "BTC":
            if allowed_periods and spec.period not in allowed_periods:
                continue
            markets.append(spec)

    # If preferred_period is specified and available, filter by preferred
    if preferred_period:
        pref_markets = [m for m in markets if m.period == preferred_period]
        if pref_markets:
            markets = pref_markets

    # Sort by start_timestamp then expiry_timestamp
    markets.sort(key=lambda m: (m.start_timestamp, m.expiry_timestamp))
    return markets


def discover_btc_15m_markets(
    outcome_meta: Dict[str, Any],
    current_timestamp: Optional[int] = None,
) -> List[OutcomeMarketSpec]:
    """
    Discover active BTC markets.
    Prefers 15m markets when available, otherwise falls back to active 1d/daily BTC markets.
    """
    all_btc = discover_btc_markets(outcome_meta, current_timestamp=current_timestamp)
    # Prefer 15m if present
    m15 = [m for m in all_btc if m.period == "15m"]
    if m15:
        return m15
    # Otherwise return available recurring BTC markets (e.g. 1d)
    return all_btc


def select_active_or_next_btc_market(
    markets: List[OutcomeMarketSpec],
    current_timestamp: Optional[int] = None,
) -> Tuple[Optional[OutcomeMarketSpec], Optional[str]]:
    """
    Select the current active market (now between start and expiry), or next upcoming market.
    Returns: (selected_market, status_tag)
    status_tag: None (current active), 'future' (upcoming), or 'none' (none found)
    """
    if not markets:
        return None, "none"

    now = current_timestamp if current_timestamp is not None else int(time.time())

    # 1. Currently active: start_timestamp <= now < expiry_timestamp
    # If multiple, choose the one closest to expiry that still has time
    active_markets = [
        m for m in markets
        if m.start_timestamp <= now < m.expiry_timestamp
    ]
    if active_markets:
        active_markets.sort(key=lambda m: m.expiry_timestamp)
        return active_markets[0], None

    # 2. Near-future markets starting within 15 minutes
    future_markets = [
        m for m in markets
        if m.start_timestamp > now
    ]
    if future_markets:
        future_markets.sort(key=lambda m: m.start_timestamp)
        return future_markets[0], "future"

    # 3. If only recently expired markets exist within 60s grace, return the latest
    recent_markets = [
        m for m in markets
        if now - m.expiry_timestamp <= 60
    ]
    if recent_markets:
        recent_markets.sort(key=lambda m: m.expiry_timestamp, reverse=True)
        return recent_markets[0], "settling"

    return None, "none"


def evaluate_outcome_market_phase(
    market: Optional[OutcomeMarketSpec],
    current_timestamp: Optional[int] = None,
    reduce_only_seconds: float = 300.0,
    settling_grace_seconds: float = 60.0,
) -> MarketPhase:
    """
    State machine for Outcome 15m market:
    - WAITING: current_time < market.start_timestamp
    - ACTIVE: market.start_timestamp <= current_time < (expiry - reduce_only_seconds)
    - REDUCE_ONLY: (expiry - reduce_only_seconds) <= current_time < expiry
    - SETTLING: expiry <= current_time < (expiry + settling_grace_seconds)
    - SETTLED: current_time >= (expiry + settling_grace_seconds)
    """
    if market is None:
        return MarketPhase.WAITING

    now = current_timestamp if current_timestamp is not None else int(time.time())
    time_left = market.time_to_expiry_sec(now)

    if now < market.start_timestamp:
        return MarketPhase.WAITING

    if time_left > reduce_only_seconds:
        return MarketPhase.ACTIVE

    if time_left > 0:
        return MarketPhase.REDUCE_ONLY

    if -time_left < settling_grace_seconds:
        return MarketPhase.SETTLING

    return MarketPhase.WAITING
