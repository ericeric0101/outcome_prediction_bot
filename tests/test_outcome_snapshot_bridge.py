from decimal import Decimal

import pytest

from bot.lifecycle.outcome_lifecycle import parse_outcome_market_spec
from bot.outcome_snapshot_bridge import build_outcome_market_snapshot, build_outcome_position_state
from bot.pricing.outcome_pricing import OutcomePricingState
from execution.exit_policy import ExitPolicy, ExitPolicyConfig, ExitStage


@pytest.fixture
def market():
    result = parse_outcome_market_spec(
        {"outcome": 516, "description": "class:priceBinary|underlying:BTC|expiry:20260823-1015|targetPrice:78213|period:15m"}
    )
    assert result is not None
    return result


def test_outcome_snapshot_has_the_existing_risk_model_shape(market):
    pricing = OutcomePricingState()
    pricing.update_btc_mark_price("78300", timestamp=1e20)
    pricing.update_l2_book(market.yes_coin, {"levels": [[{"px": "0.44", "sz": "25"}], [{"px": "0.46", "sz": "30"}]]}, timestamp=1e20)
    snapshot = build_outcome_market_snapshot(
        market=market, side_index=0, pricing=pricing,
        exit_policy=ExitPolicy(ExitPolicyConfig(aggressive_stage_sec=180, taker_stage_sec=75)),
        fee_rate=Decimal("0.00015"), slippage_buffer_pct=Decimal("0.002"),
        fair=Decimal("0.50"), current_timestamp=market.start_timestamp + 100,
    )
    assert snapshot.instrument_id == "#5160"
    assert snapshot.best_bid == Decimal("0.44")
    assert snapshot.fair_edge_ps == Decimal("0.06")
    assert snapshot.spot_minus_strike_bps is not None and snapshot.spot_minus_strike_bps > 0
    assert snapshot.exit_stage == ExitStage.PASSIVE


def test_outcome_position_preserves_up_down_and_sellable_inventory(market):
    position = build_outcome_position_state(
        market=market, side_index=1, total_qty=Decimal("10"), available_qty=Decimal("7"),
        avg_entry_price=Decimal("0.55"), hold_sec=120,
    )
    assert position.instrument_id == "#5161"
    assert position.held_side == "DOWN"
    assert position.sellable_qty == Decimal("7")
    with pytest.raises(ValueError):
        build_outcome_position_state(
            market=market, side_index=0, total_qty=Decimal("2"), available_qty=Decimal("3"),
            avg_entry_price=Decimal("0.5"),
        )
