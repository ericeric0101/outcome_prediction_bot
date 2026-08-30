from decimal import Decimal

from bot.outcome_holding_path import OutcomeHoldingPathObservation


def test_holding_path_uses_executable_bid_net_of_close_fee_not_midpoint():
    payload = OutcomeHoldingPathObservation(
        1, "1d", "#10", Decimal("13"), Decimal("0.8"), Decimal("0.7"), Decimal("0.71"),
        Decimal("0.0004"), 20, 100, "fresh_rest_book", {},
    ).payload()
    assert payload["executable_exit_price"] == "0.69972"
    assert Decimal(payload["net_exit_vs_entry_pct"]) < 0
