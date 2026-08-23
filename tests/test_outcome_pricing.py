"""Unit tests for bot/pricing/outcome_pricing.py."""

from decimal import Decimal
import time
import pytest

from bot.pricing.outcome_pricing import (
    DEFAULT_MIN_NOTIONAL_USDC,
    OutcomeBookTop,
    OutcomePricingState,
    compute_min_shares_for_notional,
    estimate_outcome_economics,
)


def test_outcome_pricing_state_btc_mark():
    state = OutcomePricingState(stale_timeout_sec=5.0)
    assert state.get_btc_mark_price() is None

    state.update_btc_mark_price(78250.5, timestamp=time.time())
    mark = state.get_btc_mark_price()
    assert mark == Decimal("78250.5")

    # Stale test
    state.update_btc_mark_price(78250.5, timestamp=time.time() - 10.0)
    assert state.get_btc_mark_price() is None


def test_outcome_pricing_state_l2_book():
    state = OutcomePricingState(stale_timeout_sec=5.0)
    l2_data = {
        "coin": "#5160",
        "levels": [
            [{"px": "0.45", "sz": "100.0", "n": 1}],
            [{"px": "0.47", "sz": "120.0", "n": 2}],
        ],
    }
    state.update_l2_book("#5160", l2_data)
    book = state.get_book_top("#5160")
    assert book is not None
    assert book.bid_price == Decimal("0.45")
    assert book.ask_price == Decimal("0.47")
    assert book.mid_price == Decimal("0.46")
    assert book.spread == Decimal("0.02")
    assert state.get_outcome_mid("#5160") == Decimal("0.46")


def test_compute_min_shares_for_notional():
    # Price = 0.50, min notional = 10 USDC -> shares = 20.0 (20 * 0.5 = 10.0 >= 10.0)
    shares = compute_min_shares_for_notional(Decimal("0.50"), Decimal("10.0"), sz_decimals=1)
    assert shares == Decimal("20.0")
    assert shares * Decimal("0.50") >= Decimal("10.0")

    # Price = 0.45, min notional = 10 USDC -> shares >= 22.222... -> 22.3 (22.3 * 0.45 = 10.035 >= 10.0)
    shares_45 = compute_min_shares_for_notional(Decimal("0.45"), Decimal("10.0"), sz_decimals=1)
    assert shares_45 >= Decimal("22.2")
    assert shares_45 * Decimal("0.45") >= Decimal("10.0")

    # Price = 0.10, min notional = 10 USDC -> shares = 100.0
    shares_10 = compute_min_shares_for_notional(Decimal("0.10"), Decimal("10.0"), sz_decimals=1)
    assert shares_10 == Decimal("100.0")
    assert shares_10 * Decimal("0.10") >= Decimal("10.0")


def test_estimate_outcome_economics():
    econ = estimate_outcome_economics(
        quote_size_usdc=Decimal("10.0"),
        probability=Decimal("0.50"),
        half_spread=Decimal("0.01"),
        min_notional_usdc=Decimal("10.0"),
    )
    assert econ.shares == Decimal("20.0")
    assert econ.probability == Decimal("0.50")
    assert econ.expected_spread_capture_usdc > Decimal("0")
    assert econ.fee_equivalent_usdc == Decimal("0")
    assert econ.expected_net_usdc > Decimal("0")

    charged = estimate_outcome_economics(
        quote_size_usdc=Decimal("10.0"), probability=Decimal("0.50"), half_spread=Decimal("0.01"),
        maker_fee_rate=Decimal("0.00015"), referral_discount=Decimal("0"),
    )
    assert charged.fee_equivalent_usdc > Decimal("0")
