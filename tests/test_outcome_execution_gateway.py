from decimal import Decimal

import pytest

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_execution_gateway import OutcomeExecutionGateway, whole_share_size


def _market() -> OutcomeMarketSpec:
    return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "15m", "")


def test_gateway_rounds_to_whole_share_minimum():
    assert whole_share_size(Decimal("0.77")) == 13
    assert whole_share_size(Decimal("0.77"), Decimal("13")) == 13
    assert whole_share_size(Decimal("0.77"), Decimal("13.1")) == 14
    assert whole_share_size(Decimal("0.77"), Decimal("3"), enforce_minimum=False) == 3


def test_gateway_only_uses_official_sidecar_contract():
    calls = []

    class Sidecar:
        def request(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"status": "resting", "orderId": "42"}

    result = OutcomeExecutionGateway(Sidecar()).place_alo(
        market=_market(), side_index=0, is_buy=True, price=Decimal("0.77"),
    )
    assert result["shares"] == 13
    assert calls == [("place_limit_order", {"payload": {"marketId": "1153", "outcome": "#11530", "side": "buy", "price": "0.77", "amount": "13", "timeInForce": "ALO"}, "allow_execution": True})]


def test_gateway_rejects_invalid_side_index():
    with pytest.raises(ValueError):
        OutcomeExecutionGateway.outcome_coin(_market(), 2)
