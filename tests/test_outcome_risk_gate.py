from decimal import Decimal

from bot.outcome_risk_gate import OutcomePreTradeRiskGate, OutcomeRiskLimits


def test_risk_gate_counts_plus_encoded_outcome_inventory_as_exposure():
    gate = OutcomePreTradeRiskGate(OutcomeRiskLimits(
        max_entry_notional_usdc=Decimal("20"), max_total_outcome_exposure_usdc=Decimal("20"), max_open_orders=1,
    ))
    decision = gate.evaluate(
        balances=[{"coin": "USDH", "total": "100", "hold": "0"}, {"coin": "+11530", "total": "15", "hold": "0"}],
        open_orders=[], price=Decimal("0.50"), shares=11,
    )
    assert decision.allowed is False
    assert decision.reason == "outcome_exposure_cap"
    assert decision.current_exposure == Decimal("15")


def test_eleven_dollar_canary_accepts_venue_minimum_after_whole_share_rounding():
    """At 59.1c, HIP-4's $10 floor requires 17 whole shares ($10.047)."""
    gate = OutcomePreTradeRiskGate(OutcomeRiskLimits(
        max_entry_notional_usdc=Decimal("11"), max_total_outcome_exposure_usdc=Decimal("11"), max_open_orders=1,
    ))
    decision = gate.evaluate(
        balances=[{"coin": "USDH", "total": "20", "hold": "0"}],
        open_orders=[], price=Decimal("0.591"), shares=17,
    )
    assert decision.allowed is True
    assert decision.entry_notional == Decimal("10.047")
