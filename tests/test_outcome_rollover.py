from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec, evaluate_outcome_market_phase
from bot.enums import MarketPhase
from bot.outcome_rollover import OutcomeRolloverCoordinator


def daily(market_id: int, start: int, expiry: int) -> OutcomeMarketSpec:
    return OutcomeMarketSpec(
        market_id, f"@{market_id}", f"#{market_id}0", f"#{market_id}1", 1, 2,
        "priceBinary", "BTC", "", expiry, start, Decimal("1"), "1d", "",
    )


def test_daily_reduce_only_is_the_last_hour_but_short_periods_keep_five_minutes():
    one_day = daily(1, 0, 100_000)
    assert evaluate_outcome_market_phase(one_day, current_timestamp=96_399) == MarketPhase.ACTIVE
    assert evaluate_outcome_market_phase(one_day, current_timestamp=96_400) == MarketPhase.REDUCE_ONLY
    short = OutcomeMarketSpec(2, "@2", "#20", "#21", 1, 2, "priceBinary", "BTC", "", 100_000, 0, Decimal("1"), "15m", "")
    assert evaluate_outcome_market_phase(short, current_timestamp=96_400) == MarketPhase.ACTIVE
    assert evaluate_outcome_market_phase(short, current_timestamp=99_800) == MarketPhase.REDUCE_ONLY


def test_rollover_tracks_old_market_on_switch_and_recovers_it_after_restart_from_meta():
    old, new = daily(10, 0, 1_000), daily(11, 1_000, 2_000)
    coordinator = OutcomeRolloverCoordinator()
    assert coordinator.observe(selected=old, discovered=[old, new], now=999) == ()
    assert coordinator.observe(selected=new, discovered=[old, new], now=1_000) == (old,)
    restarted = OutcomeRolloverCoordinator()
    assert restarted.observe(selected=new, discovered=[old, new], now=1_001) == (old,)
    assert restarted.reconciliation_markets(new) == (new, old)
