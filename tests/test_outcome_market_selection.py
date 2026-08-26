from bot.lifecycle.outcome_lifecycle import parse_period_preferences, select_configured_btc_market
from bot.outcome_daily_scope import resolve_daily_outcome_scope
import pytest


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


def test_parse_period_preferences_defaults_to_daily_scope():
    assert parse_period_preferences(None) == ("1d",)
    assert parse_period_preferences("15m, 1d") == ("15m", "1d")


def test_daily_outcome_scope_rejects_other_periods_and_fallback():
    assert resolve_daily_outcome_scope({"OUTCOME_MARKET_PERIODS": "1d", "OUTCOME_MARKET_ALLOW_FALLBACK": "0"}) == (("1d",), False)
    with pytest.raises(ValueError, match="1d-only"):
        resolve_daily_outcome_scope({"OUTCOME_MARKET_PERIODS": "15m,1d", "OUTCOME_MARKET_ALLOW_FALLBACK": "0"})
    with pytest.raises(ValueError, match="ALLOW_FALLBACK=0"):
        resolve_daily_outcome_scope({"OUTCOME_MARKET_PERIODS": "1d", "OUTCOME_MARKET_ALLOW_FALLBACK": "1"})
