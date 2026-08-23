"""Unit tests for bot/adapters/outcome_client.py."""

from decimal import Decimal
import pytest
from eth_account import Account
import httpx

from bot.adapters.outcome_auth import OutcomeAuth
from bot.adapters.outcome_client import OutcomeClient


def test_sync_info_retries_transient_502_then_returns_payload(monkeypatch):
    auth = OutcomeAuth(wallet_address="0x" + "a" * 40, is_testnet=True)
    client = OutcomeClient(auth)
    request = httpx.Request("POST", "https://example.test/info")
    responses = [
        httpx.Response(502, request=request),
        httpx.Response(200, json={"outcomes": []}, request=request),
    ]

    class FakeSyncClient:
        is_closed = False
        def post(self, *_args, **_kwargs):
            return responses.pop(0)

    monkeypatch.setattr(client, "get_sync_client", lambda: FakeSyncClient())
    monkeypatch.setattr("bot.adapters.outcome_client.time.sleep", lambda _seconds: None)
    assert client.post_info_sync({"type": "outcomeMeta"}) == {"outcomes": []}
    assert responses == []


@pytest.mark.anyio
async def test_outcome_client_mock_requests(monkeypatch):
    test_eoa = Account.create()
    auth = OutcomeAuth(
        wallet_address=test_eoa.address,
        private_key=test_eoa.key.hex(),
        is_testnet=True,
    )
    client = OutcomeClient(auth)

    info_payloads = []

    # Mock post_info
    async def mock_post_info(payload):
        info_payloads.append(payload)
        req_type = payload.get("type")
        if req_type == "outcomeMeta":
            return {
                "universe": [
                    {
                        "name": "@516",
                        "szDecimals": 1,
                        "maxLeverage": 1,
                        "onlyIsolated": True,
                        "description": "class:priceBinary|underlying:BTC|expiry:20260823-1015|targetPrice:78213|period:15m",
                    }
                ]
            }
        elif req_type == "allMids":
            return {"@516": "0.45", "BTC": "78250.5"}
        elif req_type == "l2Book":
            return {
                "coin": payload.get("coin"),
                "levels": [
                    [{"px": "0.45", "sz": "100.0", "n": 1}],
                    [{"px": "0.46", "sz": "100.0", "n": 1}],
                ],
            }
        elif req_type == "clearinghouseState":
            return {"crossMarginSummary": {"accountValue": "1000.0", "totalMarginUsed": "0.0"}}
        return {}

    # Mock post_exchange
    async def mock_post_exchange(action, vault_address=None):
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 12345}}]}}}

    monkeypatch.setattr(client, "post_info", mock_post_info)
    monkeypatch.setattr(client, "post_exchange", mock_post_exchange)

    # Test get_outcome_meta
    meta = await client.get_outcome_meta()
    assert len(meta["universe"]) == 1
    assert meta["universe"][0]["name"] == "@516"

    # Test get_all_mids
    mids = await client.get_all_mids()
    assert mids["BTC"] == "78250.5"
    assert mids["@516"] == "0.45"

    # Test get_l2_book
    book = await client.get_l2_book("@516")
    assert book["coin"] == "@516"
    assert len(book["levels"][0]) == 1

    # The official HIP-4 SDK scopes frontend open-order requests to all DEXes.
    await client.get_open_orders()
    assert info_payloads[-1]["type"] == "frontendOpenOrders"
    assert info_payloads[-1]["dex"] == "ALL_DEXS"

    # Test submit_order
    res = await client.submit_order(
        outcome_id=516,
        side_index=0,
        is_buy=True,
        price=0.45,
        size=25,
        order_type="ALO",
    )
    assert res["asset_id"] == 100005160
    assert res["price"] == "0.45"
    assert res["size"] == "25"
    assert res["is_buy"] is True
    assert res["result"]["status"] == "ok"
    assert res["success"] is True
    assert res["status"] == "resting"

    # Test split & merge
    split_res = await client.split_outcome(outcome_id=516, amount=50)
    assert split_res["status"] == "ok"

    merge_res = await client.merge_outcome(outcome_id=516, amount=50)
    assert merge_res["status"] == "ok"

    await client.close()
