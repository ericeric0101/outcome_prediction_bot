from bot.lifecycle.outcome_lifecycle import parse_outcome_market_spec


def test_market_spec_preserves_official_side_metadata():
    raw = {
        "outcome": 1145,
        "description": "class:priceBinary|underlying:BTC|expiry:20260823-0600|targetPrice:77431|period:1d",
        "sideSpecs": [{"name": "Above"}, {"name": "Below"}],
        "szDecimals": 2,
    }
    market = parse_outcome_market_spec(raw)
    assert market is not None
    assert market.side_names == ("Above", "Below")
    assert market.side_name(0) == "Above"
    assert market.raw_meta == raw
    assert market.sz_decimals == 2
