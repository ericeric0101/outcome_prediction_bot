from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_stream_health import OutcomeStreamHealth


def market(): return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "15m", "")


def test_stream_health_requires_connection_rest_resync_and_both_books():
    health = OutcomeStreamHealth(max_book_age_sec=3)
    assert health.check(market(), now=10).reason == "ws_disconnected"
    health.on_lifecycle("connected")
    assert health.check(market(), now=10).reason == "ws_rest_resync_required"
    health.mark_rest_resynced()
    health.on_l2_book("#11530", 10)
    assert health.check(market(), now=10).reason == "ws_book_missing"
    health.on_l2_book("#11531", 10)
    assert health.check(market(), now=12).ready
    assert health.check(market(), now=14).reason == "ws_book_stale"


def test_stream_health_fails_closed_after_disconnect_or_market_rollover():
    health = OutcomeStreamHealth()
    health.configure_market(market())
    health.on_lifecycle("connected")
    health.mark_rest_resynced()
    health.on_l2_book("#11530", 1); health.on_l2_book("#11531", 1)
    health.on_lifecycle("disconnected")
    assert health.check(market(), now=1).reason == "ws_disconnected"
