from decimal import Decimal

from bot.outcome_p3_calibration import choose_consensus_calibration_side, take_profit_price


def test_take_profit_includes_maker_close_fee_and_respects_price_ceiling():
    assert take_profit_price(
        entry_price=Decimal("0.80"), target_return_pct=Decimal("0.10"), maker_close_fee_rate=Decimal("0.0004"),
    ) == Decimal("0.88") / Decimal("0.9996")
    assert take_profit_price(
        entry_price=Decimal("0.95"), target_return_pct=Decimal("0.10"), maker_close_fee_rate=Decimal("0.0004"),
    ) is None


def test_side_choice_follows_feasible_market_mid_consensus_not_sample_balance():
    assert choose_consensus_calibration_side(
        mids={0: Decimal("0.80"), 1: Decimal("0.20")}, entry_bids={0: Decimal("0.79"), 1: Decimal("0.19")},
        target_return_pct=Decimal("0.05"), maker_close_fee_rate=Decimal("0.0004"), tie_breaker=2,
    ) == 0
    # If the market-consensus side cannot fit below the legal price ceiling,
    # the complementary side is not used as a directional fallback.
    assert choose_consensus_calibration_side(
        mids={0: Decimal("0.97"), 1: Decimal("0.03")}, entry_bids={0: Decimal("0.96"), 1: Decimal("0.02")},
        target_return_pct=Decimal("0.05"), maker_close_fee_rate=Decimal("0.0004"), tie_breaker=2,
    ) is None
