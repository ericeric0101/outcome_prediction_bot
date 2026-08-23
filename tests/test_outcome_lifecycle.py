"""Unit tests for bot/lifecycle/outcome_lifecycle.py."""

from decimal import Decimal
import time
import pytest

from bot.enums import MarketPhase
from bot.lifecycle.outcome_lifecycle import (
    OutcomeMarketSpec,
    discover_btc_15m_markets,
    evaluate_outcome_market_phase,
    parse_expiry_string_to_timestamp,
    parse_outcome_market_spec,
    select_active_or_next_btc_market,
)


def test_parse_expiry_string():
    ts = parse_expiry_string_to_timestamp("20260823-1015")
    assert ts > 1700000000


def test_parse_outcome_market_spec():
    item = {
        "name": "@516",
        "szDecimals": 1,
        "maxLeverage": 1,
        "description": "class:priceBinary|underlying:BTC|expiry:20260823-1015|targetPrice:78213.5|period:15m",
    }
    spec = parse_outcome_market_spec(item)
    assert spec is not None
    assert spec.outcome_id == 516
    assert spec.coin_name == "@516"
    assert spec.yes_coin == "#5160"
    assert spec.no_coin == "#5161"
    assert spec.yes_asset_id == 100005160
    assert spec.no_asset_id == 100005161
    assert spec.underlying == "BTC"
    assert spec.target_price == Decimal("78213.5")
    assert spec.strike == Decimal("78213.5")
    assert spec.period == "15m"
    assert spec.start_timestamp == spec.expiry_timestamp - 900


def test_discover_btc_15m_markets():
    meta = {
        "universe": [
            {
                "name": "@516",
                "description": "class:priceBinary|underlying:BTC|expiry:20260823-1015|targetPrice:78213|period:15m",
            },
            {
                "name": "@517",
                "description": "class:priceBinary|underlying:BTC|expiry:20260823-1030|targetPrice:78300|period:15m",
            },
            {
                "name": "@518",
                "description": "class:priceBinary|underlying:ETH|expiry:20260823-1015|targetPrice:2500|period:15m",
            },
            {
                "name": "@519",
                "description": "class:priceBinary|underlying:BTC|expiry:20260823-1100|targetPrice:78400|period:1h",
            },
        ]
    }
    markets = discover_btc_15m_markets(meta)
    assert len(markets) == 2
    assert markets[0].outcome_id == 516
    assert markets[1].outcome_id == 517


def test_select_active_or_next_btc_market():
    meta = {
        "universe": [
            {
                "name": "@516",
                "description": "class:priceBinary|underlying:BTC|expiry:20260823-1015|targetPrice:78213|period:15m",
            },
            {
                "name": "@517",
                "description": "class:priceBinary|underlying:BTC|expiry:20260823-1030|targetPrice:78300|period:15m",
            },
        ]
    }
    markets = discover_btc_15m_markets(meta)
    expiry_516 = markets[0].expiry_timestamp
    start_516 = markets[0].start_timestamp

    # Time inside market 516
    selected, tag = select_active_or_next_btc_market(markets, current_timestamp=start_516 + 300)
    assert selected is not None
    assert selected.outcome_id == 516
    assert tag is None

    # Time before market 516
    selected_future, tag_future = select_active_or_next_btc_market(markets, current_timestamp=start_516 - 100)
    assert selected_future is not None
    assert selected_future.outcome_id == 516
    assert tag_future == "future"


def test_evaluate_outcome_market_phase():
    item = {
        "name": "@516",
        "description": "class:priceBinary|underlying:BTC|expiry:20260823-1015|targetPrice:78213|period:15m",
    }
    spec = parse_outcome_market_spec(item)
    assert spec is not None
    start = spec.start_timestamp
    expiry = spec.expiry_timestamp

    # WAITING (before start)
    assert evaluate_outcome_market_phase(spec, current_timestamp=start - 10) == MarketPhase.WAITING

    # ACTIVE (between start and 5m before expiry)
    assert evaluate_outcome_market_phase(spec, current_timestamp=start + 100) == MarketPhase.ACTIVE

    # REDUCE_ONLY (last 5m before expiry)
    assert evaluate_outcome_market_phase(spec, current_timestamp=expiry - 200) == MarketPhase.REDUCE_ONLY

    # SETTLING (within 60s after expiry)
    assert evaluate_outcome_market_phase(spec, current_timestamp=expiry + 20) == MarketPhase.SETTLING

    # Post-settling transitions back to WAITING for next market
    assert evaluate_outcome_market_phase(spec, current_timestamp=expiry + 70) == MarketPhase.WAITING
