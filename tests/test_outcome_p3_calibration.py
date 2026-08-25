from decimal import Decimal

from bot.outcome_p3_calibration import choose_balanced_calibration_side, take_profit_price


def test_take_profit_includes_maker_close_fee_and_respects_price_ceiling():
    assert take_profit_price(
        entry_price=Decimal("0.80"), target_return_pct=Decimal("0.10"), maker_close_fee_rate=Decimal("0.0004"),
    ) == Decimal("0.88") / Decimal("0.9996")
    assert take_profit_price(
        entry_price=Decimal("0.95"), target_return_pct=Decimal("0.10"), maker_close_fee_rate=Decimal("0.0004"),
    ) is None


def test_side_choice_is_balanced_and_not_directional():
    bids = {0: Decimal("0.70"), 1: Decimal("0.30")}
    assert choose_balanced_calibration_side(
        bids=bids, maker_fill_counts={0: 4, 1: 1}, target_return_pct=Decimal("0.10"),
        maker_close_fee_rate=Decimal("0.0004"), tie_breaker=2,
    ) == 1
    # A high-probability side is excluded only because a +10% net target would
    # exceed the venue's legal limit; no price-direction signal is involved.
    assert choose_balanced_calibration_side(
        bids={0: Decimal("0.95"), 1: Decimal("0.30")}, maker_fill_counts={0: 0, 1: 0},
        target_return_pct=Decimal("0.10"), maker_close_fee_rate=Decimal("0.0004"), tie_breaker=2,
    ) == 1
