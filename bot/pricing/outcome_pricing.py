"""
Hyperliquid Outcome (HIP-4) Pricing, Spot Mark Feed, and Sizing Economics.

Provides:
- Native HyperCore BTC Mark Price caching and freshness verification
- Outcome token (#outcomeId0 / #outcomeId1) L2 Book depth and mid tracking
- Hyperliquid fee tiered model with referral discount support
- Min-Notional (10 USDC) sizing calculation and validation
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from execution.rebate_model import QuoteEconomics


DEFAULT_MIN_NOTIONAL_USDC = Decimal("10.0")
DEFAULT_HL_MAKER_FEE_RATE = Decimal("0.00015")  # 1.5 bps
DEFAULT_HL_TAKER_FEE_RATE = Decimal("0.00035")  # 3.5 bps
DEFAULT_REFERRAL_DISCOUNT = Decimal("0.04")     # 4% discount


@dataclass(frozen=True)
class OutcomeBookTop:
    bid_price: Optional[Decimal]
    bid_size: Optional[Decimal]
    ask_price: Optional[Decimal]
    ask_size: Optional[Decimal]
    mid_price: Optional[Decimal]
    timestamp: float

    @property
    def spread(self) -> Optional[Decimal]:
        if self.bid_price is not None and self.ask_price is not None:
            return self.ask_price - self.bid_price
        return None


class OutcomePricingState:
    """
    In-memory state tracker for Hyperliquid Outcome pricing.
    """

    def __init__(self, stale_timeout_sec: float = 10.0) -> None:
        self.stale_timeout_sec = stale_timeout_sec
        self.btc_mark_price: Optional[Decimal] = None
        self.btc_mark_timestamp: float = 0.0
        self.outcome_mids: Dict[str, Tuple[Decimal, float]] = {}
        self.outcome_books: Dict[str, OutcomeBookTop] = {}

    def update_btc_mark_price(self, price: float | Decimal | str, timestamp: Optional[float] = None) -> None:
        self.btc_mark_price = Decimal(str(price))
        self.btc_mark_timestamp = timestamp if timestamp is not None else time.time()

    def get_btc_mark_price(self, max_age_sec: Optional[float] = None) -> Optional[Decimal]:
        if self.btc_mark_price is None:
            return None
        max_age = max_age_sec if max_age_sec is not None else self.stale_timeout_sec
        if (time.time() - self.btc_mark_timestamp) > max_age:
            logger.warning(f"BTC Mark Price is stale ({time.time() - self.btc_mark_timestamp:.2f}s > {max_age}s)")
            return None
        return self.btc_mark_price

    def update_outcome_mid(self, coin: str, mid: float | Decimal | str, timestamp: Optional[float] = None) -> None:
        ts = timestamp if timestamp is not None else time.time()
        self.outcome_mids[coin] = (Decimal(str(mid)), ts)

    def get_outcome_mid(self, coin: str, max_age_sec: Optional[float] = None) -> Optional[Decimal]:
        entry = self.outcome_mids.get(coin)
        if entry is None:
            return None
        mid, ts = entry
        max_age = max_age_sec if max_age_sec is not None else self.stale_timeout_sec
        if (time.time() - ts) > max_age:
            return None
        return mid

    def update_l2_book(self, coin: str, l2_data: Dict[str, Any], timestamp: Optional[float] = None) -> None:
        """
        Process Hyperliquid L2 book payload:
        l2_data = {"coin": "...", "levels": [[bids...], [asks...]]}
        """
        levels = l2_data.get("levels", [[], []])
        bids = levels[0] if len(levels) > 0 else []
        asks = levels[1] if len(levels) > 1 else []

        best_bid_px = Decimal(str(bids[0]["px"])) if bids else None
        best_bid_sz = Decimal(str(bids[0]["sz"])) if bids else None
        best_ask_px = Decimal(str(asks[0]["px"])) if asks else None
        best_ask_sz = Decimal(str(asks[0]["sz"])) if asks else None

        mid_px: Optional[Decimal] = None
        if best_bid_px is not None and best_ask_px is not None:
            mid_px = (best_bid_px + best_ask_px) / Decimal("2")
        elif best_bid_px is not None:
            mid_px = best_bid_px
        elif best_ask_px is not None:
            mid_px = best_ask_px

        ts = timestamp if timestamp is not None else time.time()
        self.outcome_books[coin] = OutcomeBookTop(
            bid_price=best_bid_px,
            bid_size=best_bid_sz,
            ask_price=best_ask_px,
            ask_size=best_ask_sz,
            mid_price=mid_px,
            timestamp=ts,
        )
        if mid_px is not None:
            self.outcome_mids[coin] = (mid_px, ts)

    def get_book_top(self, coin: str, max_age_sec: Optional[float] = None) -> Optional[OutcomeBookTop]:
        book = self.outcome_books.get(coin)
        if book is None:
            return None
        max_age = max_age_sec if max_age_sec is not None else self.stale_timeout_sec
        if (time.time() - book.timestamp) > max_age:
            return None
        return book

    def get_best_bid_ask(self, coin: str, max_age_sec: Optional[float] = None) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        book = self.get_book_top(coin, max_age_sec=max_age_sec)
        if book is None:
            return None, None
        return book.bid_price, book.ask_price


def compute_min_shares_for_notional(
    price: Decimal | float | str,
    min_notional_usdc: Decimal = DEFAULT_MIN_NOTIONAL_USDC,
    sz_decimals: int = 1,
) -> Decimal:
    """
    Compute the minimum number of shares required to meet the min notional constraint.
    shares * price >= min_notional_usdc => shares >= min_notional_usdc / price.
    """
    p = Decimal(str(price))
    if p <= 0:
        return Decimal("1.0")
    raw_shares = min_notional_usdc / p
    # Round UP to instrument size decimals
    fmt = "0." + "0" * sz_decimals if sz_decimals > 0 else "1"
    step = Decimal("0." + "0" * (sz_decimals - 1) + "1") if sz_decimals > 0 else Decimal("1")
    rounded = raw_shares.quantize(Decimal(fmt), rounding=ROUND_UP)
    # Double check inequality
    while rounded * p < min_notional_usdc:
        rounded += step
    return rounded


def estimate_outcome_economics(
    quote_size_usdc: Decimal,
    probability: Decimal,
    half_spread: Decimal,
    min_notional_usdc: Decimal = DEFAULT_MIN_NOTIONAL_USDC,
    maker_fee_rate: Decimal = DEFAULT_HL_MAKER_FEE_RATE,
    referral_discount: Decimal = DEFAULT_REFERRAL_DISCOUNT,
    adverse_selection_buffer: Decimal = Decimal("0"),
) -> QuoteEconomics:
    """
    Calculate Hyperliquid Outcome quote economics taking into account fees, spread capture,
    and 10 USDC min notional.
    """
    p = max(Decimal("0.0001"), min(Decimal("0.9999"), probability))
    effective_maker_fee_rate = maker_fee_rate * (Decimal("1") - referral_discount)

    # Determine shares: ensure notional is at least min_notional_usdc
    target_notional = max(quote_size_usdc, min_notional_usdc)
    shares = compute_min_shares_for_notional(p, target_notional, sz_decimals=1)

    # Expected spread capture
    spread_capture = shares * p * half_spread

    # Fee charged
    fee_equivalent = shares * p * effective_maker_fee_rate

    # Expected net
    expected_net = spread_capture - fee_equivalent - adverse_selection_buffer

    return QuoteEconomics(
        shares=shares,
        probability=p,
        fee_equivalent_usdc=fee_equivalent,
        expected_rebate_usdc=Decimal("0"),  # Hyperliquid maker fee is already discounted
        expected_spread_capture_usdc=spread_capture,
        expected_net_usdc=expected_net,
    )
