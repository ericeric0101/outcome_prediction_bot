from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_stream_health import OutcomeStreamHealth


def market(): return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "15m", "")


def test_stream_health_requires_connection_rest_resync_and_both_books():
    health = OutcomeStreamHealth()
    assert health.check(market(), now=10).reason == "ws_disconnected"
    health.on_lifecycle("connected")
    assert health.check(market(), now=10).reason == "ws_rest_resync_required"
    health.mark_rest_resynced()
    health.on_l2_book("#11530", 10)
    assert health.check(market(), now=10).reason == "ws_book_missing"
    health.on_l2_book("#11531", 10)
    # Outcome quiet-book updates are normally spaced about 5--6 seconds; the
    # default must not classify that normal cadence as a disconnect.
    assert health.check(market(), now=14).ready
    assert health.check(market(), now=26).reason == "ws_book_stale"


def test_stream_health_fails_closed_after_disconnect_or_market_rollover():
    health = OutcomeStreamHealth()
    health.configure_market(market())
    health.on_lifecycle("connected")
    health.mark_rest_resynced()
    health.on_l2_book("#11530", 1); health.on_l2_book("#11531", 1)
    health.on_lifecycle("disconnected")
    assert health.check(market(), now=1).reason == "ws_disconnected"


def test_stream_health_exposes_only_a_healthy_ws_bbo_keep_hint():
    health = OutcomeStreamHealth()
    health.configure_market(market())
    health.on_lifecycle("connected")
    health.mark_rest_resynced()
    health.on_l2_book("#11530", payload={
        "levels": [[{"px": "0.60", "sz": "10"}, {"px": "0.59", "sz": "20"}], [{"px": "0.61", "sz": "5"}]],
    })
    health.on_l2_book("#11531", payload={
        "levels": [[{"px": "0.39", "sz": "11"}], [{"px": "0.40", "sz": "5"}]],
    })
    assert health.fresh_bbo(market(), "#11530") == (Decimal("0.60"), Decimal("0.61"))
    assert health.fresh_book_top(market(), "#11530")["top3_bid_depth"] == Decimal("30")
    health.on_lifecycle("disconnected")
    assert health.fresh_bbo(market(), "#11530") is None
