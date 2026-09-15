from decimal import Decimal

from bot.outcome_entry_quality_shadow import (
    OutcomeEntryQualityShadow,
    OutcomePostFillQualityInput,
    OutcomeRestingBuyQualityInput,
)


def _resting(*, signal_side=0, age=31.0, context=None):
    return OutcomeRestingBuyQualityInput(
        outcome_id=1, period="1d", coin="#yes", order_id="entry-1", side_index=0,
        order_price=Decimal("0.60"), order_age_sec=age,
        current_signal_side_index=signal_side, current_signal_reason="confirmed",
        entry_audit={"entry_tier": "tier_a"}, market_context=context or {
            "yes_best_bid": "0.60", "yes_best_ask": "0.61",
        },
    )


def test_signal_decay_after_predeclared_age_is_shadow_cancel_only():
    payload = OutcomeEntryQualityShadow().evaluate_resting_buy(_resting(signal_side=None))
    assert payload["signal_state"] == "SIGNAL_DECAY"
    assert payload["stale_cancel_shadow"]["action"] == "CANCEL_STALE_SHADOW"
    assert payload["live_authority"] is False
    assert payload["execution_submitted"] is False


def test_same_side_fresh_order_stays_baseline_and_reports_ioc_cost():
    payload = OutcomeEntryQualityShadow().evaluate_resting_buy(_resting(age=5.0))
    assert payload["stale_cancel_shadow"]["action"] == "KEEP_SHADOW"
    assert payload["baseline_policy"]["action"] == "JOIN_BEST_BID"
    assert payload["ioc_counterfactual"]["action"] == "PRICE_PROTECTED_IOC_COUNTERFACTUAL"
    assert Decimal(payload["ioc_counterfactual"]["cross_cost_bps_from_resting_quote"]) > 0


def test_postfill_watch_is_not_a_stop_or_execution_authority():
    payload = OutcomeEntryQualityShadow().evaluate_post_fill(OutcomePostFillQualityInput(
        outcome_id=1, period="1d", coin="#yes", order_id="entry-1", fill_trade_id="trade-1",
        fill_vwap=Decimal("0.60"), holding_age_sec=20.0,
        best_bid=Decimal("0.57"), best_ask=Decimal("0.58"), top3_bid_depth=Decimal("10"),
    ))
    assert payload["post_fill_scratch_shadow"]["action"] == "SCRATCH_IOC_COUNTERFACTUAL"
    assert payload["post_fill_scratch_shadow"]["live_authority"] is False
    assert payload["execution_submitted"] is False
