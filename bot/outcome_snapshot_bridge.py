"""Build existing strategy snapshots from Hyperliquid Outcome state."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec, evaluate_outcome_market_phase
from bot.models import MarketSnapshot, PositionState
from bot.pricing.outcome_pricing import OutcomePricingState
from execution.exit_policy import ExitPolicy


def build_outcome_market_snapshot(
    *,
    market: OutcomeMarketSpec,
    side_index: int,
    pricing: OutcomePricingState,
    exit_policy: ExitPolicy,
    fee_rate: Decimal,
    slippage_buffer_pct: Decimal,
    fair: Optional[Decimal] = None,
    current_timestamp: Optional[int] = None,
) -> MarketSnapshot:
    """Adapt an Outcome side book to the unchanged exit/risk input model."""
    if side_index not in (0, 1):
        raise ValueError("side_index must be 0 (UP/YES) or 1 (DOWN/NO)")
    coin = market.yes_coin if side_index == 0 else market.no_coin
    bid, ask = pricing.get_best_bid_ask(coin)
    if bid is None or ask is None:
        raise ValueError(f"Outcome book is incomplete for {coin}")
    time_left_sec = max(0.0, market.time_to_expiry_sec(current_timestamp))
    phase = evaluate_outcome_market_phase(market, current_timestamp=current_timestamp)
    spread = ask - bid
    spot = pricing.get_btc_mark_price()
    spot_minus_strike_bps = None
    if spot is not None and market.strike > 0:
        distance = (spot - market.strike) / market.strike * Decimal("10000")
        spot_minus_strike_bps = distance if side_index == 0 else -distance
    return MarketSnapshot(
        instrument_id=coin,
        phase=phase.value,
        time_left_sec=time_left_sec,
        best_bid=bid,
        best_ask=ask,
        fee_rate=fee_rate,
        spread=spread,
        spread_pct=(spread / bid) if bid > 0 else Decimal("0"),
        slippage_buffer_pct=slippage_buffer_pct,
        exit_stage=exit_policy.stage(time_left_sec),
        in_reduce_only_tail=phase.value == "REDUCE_ONLY",
        stop_loss_disabled_in_tail=False,
        fair=fair,
        fair_edge_ps=(fair - bid) if fair is not None else None,
        spot_minus_strike_bps=spot_minus_strike_bps,
    )


def build_outcome_position_state(
    *,
    market: OutcomeMarketSpec,
    side_index: int,
    total_qty: Decimal,
    available_qty: Decimal,
    avg_entry_price: Decimal,
    entry_fee_remaining: Decimal = Decimal("0"),
    hold_sec: float = 0.0,
    stop_loss_confirm_hits: int = 0,
    peak_bid: Optional[Decimal] = None,
    peak_fair: Optional[Decimal] = None,
) -> PositionState:
    """Adapt confirmed Outcome balance data to the unchanged risk position model."""
    if side_index not in (0, 1):
        raise ValueError("side_index must be 0 (UP/YES) or 1 (DOWN/NO)")
    if total_qty < 0 or available_qty < 0:
        raise ValueError("Outcome position quantities must be non-negative")
    if available_qty > total_qty:
        raise ValueError("available_qty cannot exceed total_qty")
    coin = market.yes_coin if side_index == 0 else market.no_coin
    return PositionState(
        instrument_id=coin,
        qty=total_qty,
        sellable_qty=available_qty,
        avg_entry_price=avg_entry_price,
        entry_fee_remaining=entry_fee_remaining,
        hold_sec=hold_sec,
        stop_loss_confirm_hits=stop_loss_confirm_hits,
        held_side="UP" if side_index == 0 else "DOWN",
        peak_bid=peak_bid,
        peak_fair=peak_fair,
    )
