"""Unit tests for bot/adapters/outcome_auth.py."""

import time
from decimal import Decimal
import pytest
from eth_account import Account

from bot.adapters.outcome_auth import (
    OutcomeAuth,
    align_outcome_price,
    align_outcome_size,
    generate_cloid,
    hash_action,
    outcome_asset_id,
    parse_outcome_asset_id,
)


def test_outcome_asset_id_calculation():
    # Test case from HIP-4 spec: Outcome ID 516
    assert outcome_asset_id(516, 0) == 100005160
    assert outcome_asset_id(516, 1) == 100005161
    assert outcome_asset_id(0, 0) == 100000000
    assert outcome_asset_id(0, 1) == 100000001
    assert outcome_asset_id(12345, 0) == 100123450
    assert outcome_asset_id(12345, 1) == 100123451

    # Invalid side_index or negative outcome_id
    with pytest.raises(ValueError):
        outcome_asset_id(516, 2)
    with pytest.raises(ValueError):
        outcome_asset_id(-1, 0)


def test_outcome_price_alignment_uses_five_significant_figures_without_float_loss():
    assert align_outcome_price("0.550012") == "0.55001"
    assert align_outcome_price("0.000012345") == "0.000012345"
    assert align_outcome_price("0.999999") == "0.99999"


def test_parse_outcome_asset_id():
    assert parse_outcome_asset_id(100005160) == (516, 0)
    assert parse_outcome_asset_id(100005161) == (516, 1)
    assert parse_outcome_asset_id(100000000) == (0, 0)
    assert parse_outcome_asset_id(100123451) == (12345, 1)

    with pytest.raises(ValueError):
        parse_outcome_asset_id(99999999)


def test_align_outcome_price():
    # Supported FrontendMarket boundary and invalid price rejection.
    assert align_outcome_price(0.00001) == "0.00001"
    with pytest.raises(ValueError):
        align_outcome_price(1.5)
    assert align_outcome_price(0.0001) == "0.0001"
    assert align_outcome_price(0.9999) == "0.9999"

    # Tick precision
    assert align_outcome_price("0.45") == "0.45"
    assert align_outcome_price(Decimal("0.4500")) == "0.45"
    assert align_outcome_price(0.45123) == "0.45123"
    assert align_outcome_price("0.5000") == "0.5"


def test_align_outcome_size():
    assert align_outcome_size(10, 0) == "10"
    assert align_outcome_size(10.55, 1) == "10.6"
    assert align_outcome_size(Decimal("25.0"), 1) == "25.0"

    with pytest.raises(ValueError):
        align_outcome_size(0)


def test_generate_cloid():
    c1 = generate_cloid()
    c2 = generate_cloid()
    assert c1.startswith("0x")
    assert len(c1) == 34  # '0x' + 32 hex chars = 128 bits
    assert c1 != c2


def test_agent_authorization_requires_explicit_post_approval_verification():
    eoa = Account.create()
    agent = Account.create()
    auth = OutcomeAuth(eoa.address, private_key=eoa.key.hex(), agent_private_key=agent.key.hex())
    with pytest.raises(RuntimeError, match="unverified"):
        auth.require_agent_authorized()
    auth.mark_agent_authorized_after_verification()
    auth.require_agent_authorized()


def test_outcome_auth_signing():
    test_eoa = Account.create()
    test_agent = Account.create()

    auth = OutcomeAuth(
        wallet_address=test_eoa.address,
        private_key=test_eoa.key.hex(),
        agent_private_key=test_agent.key.hex(),
        is_testnet=True,
    )

    assert auth.wallet_address == test_eoa.address.lower()
    assert auth.agent_address == test_agent.address.lower()
    assert not auth.is_transient_agent

    # Test signing order action
    action = {
        "type": "order",
        "orders": [
            {
                "a": 100005160,
                "b": True,
                "p": "0.45",
                "s": "25",
                "r": False,
                "t": {"limit": {"tif": "Gtc"}},
                "c": generate_cloid(),
            }
        ],
        "grouping": "na",
    }
    signed_payload = auth.sign_l1_action(action=action, nonce=1700000000000)
    assert signed_payload["action"] == action
    assert signed_payload["nonce"] == 1700000000000
    assert "signature" in signed_payload
    sig = signed_payload["signature"]
    assert sig["r"].startswith("0x")
    assert sig["s"].startswith("0x")
    assert sig["v"] in (27, 28)


def test_outcome_auth_transient_agent_approval():
    test_eoa = Account.create()
    # Create auth with EOA only -> will generate transient agent key
    auth = OutcomeAuth(
        wallet_address=test_eoa.address,
        private_key=test_eoa.key.hex(),
        is_testnet=True,
    )
    assert auth.is_transient_agent is False  # Used EOA directly as agent since private key provided

    # Create auth with no keys (only wallet address) -> generates transient agent
    auth_wallet_only = OutcomeAuth(
        wallet_address=test_eoa.address,
        is_testnet=True,
    )
    assert auth_wallet_only.is_transient_agent is True
    assert auth_wallet_only.agent_address.startswith("0x")

    # Approve agent payload
    payload = auth.create_agent_approval_payload(agent_name="TestBot", nonce=1700000000000)
    assert payload["action"]["type"] == "approveAgent"
    assert payload["action"]["agentName"] == "TestBot"
    assert "signature" in payload
