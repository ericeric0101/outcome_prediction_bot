"""Unit tests for bot/execution/outcome_execution.py."""

from decimal import Decimal
import pytest
from eth_account import Account

from bot.adapters.outcome_auth import OutcomeAuth
from bot.adapters.outcome_client import OutcomeClient
from bot.execution.outcome_execution import OutcomeExecutionAdapter
from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec


@pytest.fixture
def mock_execution_adapter(monkeypatch):
    test_eoa = Account.create()
    auth = OutcomeAuth(
        wallet_address=test_eoa.address,
        private_key=test_eoa.key.hex(),
        is_testnet=True,
    )
    client = OutcomeClient(auth)

    submitted_actions = []

    async def mock_post_exchange(action, vault_address=None):
        submitted_actions.append(action)
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"resting": {"oid": 98765}}]},
            },
        }

    monkeypatch.setattr(client, "post_exchange", mock_post_exchange)
    adapter = OutcomeExecutionAdapter(client, min_notional_usdc=Decimal("10.0"))
    adapter.submitted_actions = submitted_actions
    return adapter


@pytest.mark.anyio
async def test_submit_maker_buy_min_notional(mock_execution_adapter):
    adapter = mock_execution_adapter
    # price = 0.40, size = 10 -> notional = 4 < 10 -> 25 whole shares.
    res = await adapter.submit_maker_buy(
        outcome_id=516,
        side_index=0,
        price=Decimal("0.40"),
        size=Decimal("10.0"),
    )
    assert res["outcome_id"] == 516
    assert res["side_index"] == 0
    assert res["price"] == Decimal("0.40")
    assert res["size"] >= Decimal("25.0")
    assert res["venue_oid"] == 98765

    cloid = res["cloid"]
    order = adapter.get_order_by_cloid(cloid)
    assert order is not None
    assert order.order_type == "ALO"
    assert adapter.submitted_actions[-1]["orders"][0]["s"] == "25"


@pytest.mark.anyio
async def test_submit_take_profit(mock_execution_adapter):
    adapter = mock_execution_adapter
    res = await adapter.submit_take_profit(
        outcome_id=516,
        side_index=0,
        size=Decimal("25.0"),
        tp_price=Decimal("0.97"),
    )
    assert res["price"] == Decimal("0.97")
    assert res["size"] == Decimal("25.0")
    cloid = res["cloid"]
    order = adapter.get_order_by_cloid(cloid)
    assert order is not None
    assert order.is_exit is True
    assert order.order_type == "Gtc"
    assert adapter.submitted_actions[-1]["orders"][0]["s"] == "25"


@pytest.mark.anyio
async def test_submit_recovery_ladder(mock_execution_adapter):
    adapter = mock_execution_adapter
    # Stage 1: Passive recovery
    res1 = await adapter.submit_recovery_exit(
        outcome_id=516,
        side_index=0,
        size=Decimal("25.0"),
        exit_price=Decimal("0.42"),
        is_ioc=False,
    )
    assert res1["is_ioc"] is False
    order1 = adapter.get_order_by_cloid(res1["cloid"])
    assert order1.is_recovery is True
    assert order1.is_urgent is False

    # Stage 2: IOC Exit
    res2 = await adapter.submit_recovery_exit(
        outcome_id=516,
        side_index=0,
        size=Decimal("25.0"),
        exit_price=Decimal("0.30"),
        is_ioc=True,
    )
    assert res2["is_ioc"] is True
    order2 = adapter.get_order_by_cloid(res2["cloid"])
    assert order2.is_recovery is True
    assert order2.is_urgent is True
    assert adapter.submitted_actions[-1]["orders"][0]["s"] == "25"


def test_compute_settlement_rejects_unverified_mark_inference(mock_execution_adapter):
    adapter = mock_execution_adapter
    market_spec = OutcomeMarketSpec(
        outcome_id=516,
        coin_name="@516",
        yes_coin="#5160",
        no_coin="#5161",
        yes_asset_id=100005160,
        no_asset_id=100005161,
        market_class="priceBinary",
        underlying="BTC",
        expiry_str="20260823-1015",
        expiry_timestamp=1700000900,
        start_timestamp=1700000000,
        target_price=Decimal("78213.0"),
        period="15m",
        raw_spec="",
    )

    with pytest.raises(RuntimeError, match="inference is disabled"):
        adapter.compute_settlement(
        market_spec=market_spec,
        settlement_mark_price=Decimal("78250.0"),
        inventory_shares=Decimal("25.0"),
        inventory_cost_usdc=Decimal("11.25"),
        held_side_index=0,
        )
