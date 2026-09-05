from decimal import Decimal

from bot.outcome_event_bridge import OutcomeFillEvent
from bot.outcome_markout import OutcomeQuote, markouts_for_fill


def _fill(side="BUY"):
    return OutcomeFillEvent(
        outcome_id=516, side_index=0, coin="#5160", client_order_id=None, venue_order_id="1",
        trade_id="trade-1", side=side, price=Decimal("0.50"), quantity=Decimal("10"),
        fee_usdc=Decimal("0"), fee_token="USDC", timestamp_ms=1_000, is_maker=True, raw={},
    )


def test_buy_markout_uses_future_executable_bid_not_midpoint():
    quotes = [OutcomeQuote("#5160", 2_000, Decimal("0.54"), Decimal("0.55"))]
    observation = markouts_for_fill(_fill(), quotes, (1,))[0]
    assert observation.status == "observed"
    assert observation.executable_mark == Decimal("0.54")
    assert observation.markout_per_share == Decimal("0.04")


def test_missing_future_quote_is_unknown_not_a_synthetic_loss():
    observation = markouts_for_fill(_fill("SELL"), [], (1,))[0]
    assert observation.status == "missing_horizon_quote"
    assert observation.markout_per_share is None


def test_markout_refuses_a_far_later_quote_instead_of_stretching_horizon():
    quotes = [OutcomeQuote("#5160", 32_000, Decimal("0.54"), Decimal("0.55"))]
    observation = markouts_for_fill(_fill(), quotes, (1,), tolerance_ms=500)[0]
    assert observation.status == "missing_horizon_quote"
    assert observation.markout_per_share is None


def test_markout_records_actual_elapsed_and_nearest_quote_within_tolerance():
    quotes = [
        OutcomeQuote("#5160", 5_400, Decimal("0.54"), Decimal("0.55"), snapshot_event_id=9),
        OutcomeQuote("#5160", 6_900, Decimal("0.53"), Decimal("0.54"), snapshot_event_id=10),
    ]
    observation = markouts_for_fill(_fill(), quotes, (5,), tolerance_ms=2_500)[0]
    assert observation.status == "observed"
    assert observation.actual_elapsed_ms == 4_400
    assert observation.target_lag_ms == -600
    assert observation.snapshot_event_id == 9
