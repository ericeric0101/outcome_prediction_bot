from decimal import Decimal

from bot.outcome_market_risk_monitor import OutcomeMarketRiskMonitor, OutcomeMarketRiskObservation


def observation(timestamp: float, bid: str, depth: str) -> OutcomeMarketRiskObservation:
    return OutcomeMarketRiskObservation(
        lifecycle_id="l", outcome_id=1, coin="#1", period="1d", timestamp=timestamp,
        entry_price=Decimal(".8"), position_size=Decimal("15"), best_bid=Decimal(bid), best_ask=Decimal(bid) + Decimal(".01"),
        top1_depth=Decimal(depth), top3_depth=Decimal(depth), side_index=0, time_left_sec=100, entry_time_left_sec=200,
        spot_strike_bps=Decimal("100"), mark_return_bps=None, oi_return_bps=None, oi_age_ms=None,
        reversal_state="WEAKENING", independent_confirmation_count=0,
    )


def test_monitor_is_read_only_and_emits_shadow_risk_state():
    monitor = OutcomeMarketRiskMonitor()
    monitor.observe(observation(0, ".8", "100"))
    decision = monitor.observe(observation(31, ".7", "50"))
    assert decision["live_authority"] is False
    assert decision["execution_submitted"] is False
    assert decision["state"] == "SEVERE_DISLOCATION_RESEARCH"
    assert not hasattr(monitor, "gateway")


def test_lane_requires_persistence_and_full_depth_cap_without_mutation():
    monitor = OutcomeMarketRiskMonitor()
    monitor.observe(observation(0, ".8", "100"))
    monitor.observe(observation(31, ".7", "50"))
    decision = monitor.observe(observation(42, ".6", "20"))
    lane = decision["fast_failure_lane_shadow"]
    assert lane["state"] == "WARNING_CANDIDATE"
    depth = monitor.assess_full_depth(
        lifecycle_id="l", timestamp=42, entry_price=Decimal(".8"),
        full_inventory_vwap=Decimal(".7"), full_inventory=True,
        taker_close_fee_rate=Decimal(".0007"),
    )
    assert depth["state"] == "HARD_CANDIDATE_WITHIN_CAP"
    assert depth["live_authority"] is False
    assert depth["execution_submitted"] is False
    assert monitor.latest_persistent_lane(lifecycle_id="l", now=42) is not None
