from bot.lifecycle.outcome_lifecycle import parse_period_preferences, select_configured_btc_market


def _meta(*periods):
    return {"outcomes": [
        {"outcome": 100 + i, "description": f"class:priceBinary|underlying:BTC|expiry:20260824-1400|targetPrice:70000|period:{period}", "sideSpecs": [{"name": "Up"}, {"name": "Down"}]}
        for i, period in enumerate(periods)
    ]}


def test_configured_market_selection_falls_back_when_15m_is_absent():
    market, status, period, fallback = select_configured_btc_market(
        _meta("1d"), period_preferences=("15m", "1d"), current_timestamp=1787580000,
    )
    assert market is not None and market.period == "1d"
    assert period == "1d" and fallback
    assert status in {None, "future", "settling"}


def test_exact_period_mode_does_not_silently_use_a_different_market():
    market, status, period, fallback = select_configured_btc_market(
        _meta("1d"), period_preferences=("15m",), allow_fallback=False, current_timestamp=1787580000,
    )
    assert (market, status, period, fallback) == (None, "none", None, False)


def test_parse_period_preferences_has_safe_multi_period_default():
    assert parse_period_preferences(None)[0] == "15m"
    assert parse_period_preferences("15m, 1d") == ("15m", "1d")
