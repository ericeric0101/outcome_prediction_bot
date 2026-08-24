from decimal import Decimal

from bot.outcome_risk_gate import OutcomePreTradeRiskGate, OutcomeRiskLimits


def gate(**kwargs): return OutcomePreTradeRiskGate(OutcomeRiskLimits(**kwargs))


def test_risk_gate_allows_one_funded_minimum_entry():
    result = gate().evaluate(balances=[{"coin": "USDH", "total": "13.27", "hold": "0"}], open_orders=[], price=Decimal("0.77"), shares=13)
    assert result.allowed and result.entry_notional == Decimal("10.01")


def test_risk_gate_rejects_insufficient_free_collateral_and_existing_order():
    funded = [{"coin": "USDH", "total": "13", "hold": "10"}]
    assert gate().evaluate(balances=funded, open_orders=[], price=Decimal("0.77"), shares=13).reason == "insufficient_available_collateral"
    assert gate().evaluate(balances=[{"coin": "USDC", "total": "20", "hold": "0"}], open_orders=[{"oid": 1}], price=Decimal("0.77"), shares=13).reason == "open_order_cap"


def test_risk_gate_counts_outcome_inventory_at_worst_case_payout():
    result = gate(max_total_outcome_exposure_usdc=Decimal("20")).evaluate(
        balances=[{"coin": "USDH", "total": "30", "hold": "0"}, {"coin": "#11530", "total": "13", "hold": "0"}], open_orders=[], price=Decimal("0.77"), shares=13,
    )
    assert not result.allowed and result.reason == "outcome_exposure_cap"
