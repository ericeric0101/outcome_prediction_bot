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
