from decimal import Decimal

import pytest

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_settlement import OutcomeSettlementAdapter


def market(): return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "15m", "")


def test_settlement_uses_official_settled_outcome_reply():
    class Sidecar:
        def request(self, command, **_):
            assert command == "fetch_settled_outcome"
            return {"settleFraction": "1", "details": "official"}
    result = OutcomeSettlementAdapter(Sidecar()).fetch(market())
    assert result.settled and result.settle_fraction == Decimal("1")


def test_merge_refuses_unsettled_market_before_execution():
    class Sidecar:
        def request(self, command, **_): return None if command == "fetch_settled_outcome" else pytest.fail("must not execute")
    with pytest.raises(RuntimeError, match="not confirmed"):
        OutcomeSettlementAdapter(Sidecar()).merge_paired_shares(market=market(), amount=Decimal("10"))


def test_merge_is_a_separate_guarded_action_after_settlement():
    calls = []
    class Sidecar:
        def request(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"settleFraction": "1"} if command == "fetch_settled_outcome" else {"success": True}
    assert OutcomeSettlementAdapter(Sidecar()).merge_paired_shares(market=market(), amount=Decimal("10"))["success"]
    assert calls[1][0] == "merge_outcome"
