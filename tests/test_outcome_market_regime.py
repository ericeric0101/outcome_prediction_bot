from decimal import Decimal

from bot.outcome_market_regime import (
    OutcomeExecutionRisk,
    OutcomeMarketRegime,
    OutcomeMarketRegimeInput,
    OutcomeMarketRegimeShadow,
    OutcomeToxicFillInput,
)


def market(*, now: float, midpoint: str, side: int = 1,
           spot: str = "20", mark5: str = "20", mark15: str = "20", mark60: str = "20"):
    return OutcomeMarketRegimeInput(
        outcome_id=1, now_ts=now, yes_midpoint=Decimal(midpoint),
        spot_strike_bps=Decimal(spot), mark_5m_bps=Decimal(mark5),
        mark_15m_bps=Decimal(mark15), mark_60m_bps=Decimal(mark60),
        candidate_side_index=side,
    )


def toxic(*, now: float, bid: str, depth: str, age: float = 30.0):
    return OutcomeToxicFillInput(
        outcome_id=1, coin="#1", now_ts=now, holding_age_sec=age,
        fill_vwap=Decimal("0.70"), best_bid=Decimal(bid), top3_bid_depth=Decimal(depth),
    )


def test_multihorizon_alignment_marks_trend():
    shadow = OutcomeMarketRegimeShadow()
    result = shadow.observe_market(market(now=1.0, midpoint="0.40", side=1, spot="-30", mark5="-20", mark15="-15", mark60="-10"))
    assert result.state is OutcomeMarketRegime.TREND


def test_multihorizon_conflict_marks_transition_without_changing_authority():
    shadow = OutcomeMarketRegimeShadow()
    result = shadow.observe_market(market(now=1.0, midpoint="0.70", side=0, spot="30", mark5="20", mark15="-15", mark60="20"))
    assert result.state is OutcomeMarketRegime.TRANSITION
    assert result.reason == "multihorizon_direction_conflict"


def test_two_durable_hysteresis_crosses_mark_range():
    shadow = OutcomeMarketRegimeShadow()
    # First establish a confirmed DOWN zone.
    shadow.observe_market(market(now=0.0, midpoint="0.47", side=0))
    shadow.observe_market(market(now=600.0, midpoint="0.47", side=0))
    # Then complete the first durable crossing to UP.
    shadow.observe_market(market(now=601.0, midpoint="0.53", side=0))
    first = shadow.observe_market(market(now=1201.0, midpoint="0.53", side=0))
    assert first.state is OutcomeMarketRegime.TRANSITION
    # And a second, independently durable crossing back to DOWN.
    shadow.observe_market(market(now=1202.0, midpoint="0.47", side=1, spot="-20", mark5="-20", mark15="-20", mark60="-20"))
    result = shadow.observe_market(market(now=1802.0, midpoint="0.47", side=1, spot="-20", mark5="-20", mark15="-20", mark60="-20"))
    assert result.state is OutcomeMarketRegime.RANGE
    assert result.confirmed_crosses_2h == 2


def test_single_tick_through_fifty_is_not_a_cross():
    shadow = OutcomeMarketRegimeShadow()
    shadow.observe_market(market(now=0.0, midpoint="0.47"))
    shadow.observe_market(market(now=600.0, midpoint="0.47"))
    result = shadow.observe_market(market(now=601.0, midpoint="0.53"))
    assert result.confirmed_crosses_2h == 0
    assert result.state is not OutcomeMarketRegime.RANGE


def test_toxic_fill_requires_joint_price_and_depth_deterioration_to_persist():
    shadow = OutcomeMarketRegimeShadow()
    baseline = shadow.observe_toxic_fill(toxic(now=0.0, bid="0.70", depth="100"))
    assert baseline.state is OutcomeExecutionRisk.NORMAL
    # Loss alone does not become a toxic-fill diagnosis without depth loss.
    benign = shadow.observe_toxic_fill(toxic(now=3.0, bid="0.64", depth="90"))
    assert benign.state is OutcomeExecutionRisk.NORMAL
    first = shadow.observe_toxic_fill(toxic(now=6.0, bid="0.60", depth="60"))
    second = shadow.observe_toxic_fill(toxic(now=9.0, bid="0.59", depth="60"))
    third = shadow.observe_toxic_fill(toxic(now=12.0, bid="0.58", depth="55"))
    assert first.state is OutcomeExecutionRisk.DETERIORATING
    assert second.state is OutcomeExecutionRisk.DETERIORATING
    assert third.state is OutcomeExecutionRisk.TOXIC_FILL


def test_toxic_fill_is_unknown_after_short_observation_window():
    shadow = OutcomeMarketRegimeShadow()
    result = shadow.observe_toxic_fill(toxic(now=121.0, bid="0.50", depth="10", age=121.0))
    assert result.state is OutcomeExecutionRisk.UNKNOWN
