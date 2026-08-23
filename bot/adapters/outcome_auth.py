"""
Hyperliquid Outcome (HIP-4) Authentication, Key Management, and Signing Module.

Provides:
- Wire protocol Asset ID calculation: Asset ID = 100_000_000 + (outcomeId * 10) + sideIndex
- Price/size tick-alignment and formatting
- Cloid (client order ID) generation
- Transient / configured Agent Key management
- MessagePack serialization + keccak256 hashing + EIP-712 L1 Action signing
"""

from __future__ import annotations

import math
import os
import secrets
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple, Union

try:
    import msgpack
    def _pack_action(action: Dict[str, Any]) -> bytes:
        # Match Hyperliquid's reference ``msgpack.packb(action)`` byte-for-byte.
        return msgpack.packb(action)
except ImportError:
    from msgspec import msgpack
    def _pack_action(action: Dict[str, Any]) -> bytes:
        return msgpack.encode(action)
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak


HYPERLIQUID_MAINNET_API = "https://api.hyperliquid.xyz"
HYPERLIQUID_TESTNET_API = "https://api.hyperliquid-testnet.xyz"
HYPERLIQUID_MAINNET_WS = "wss://api.hyperliquid.xyz/ws"
HYPERLIQUID_TESTNET_WS = "wss://api.hyperliquid-testnet.xyz/ws"

EXCHANGE_DOMAIN = {
    "name": "Exchange",
    "version": "1",
    "chainId": 1337,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}

AGENT_EIP712_TYPES = {
    "Agent": [
        {"name": "source", "type": "string"},
        {"name": "connectionId", "type": "bytes32"},
    ],
}


def outcome_asset_id(outcome_id: int, side_index: int) -> int:
    """
    Compute Hyperliquid HIP-4 wire protocol Asset ID.
    Formula: 100_000_000 + (outcomeId * 10) + sideIndex
    sideIndex: 0 = YES / UP, 1 = NO / DOWN
    """
    if side_index not in (0, 1):
        raise ValueError(f"side_index must be 0 (YES/UP) or 1 (NO/DOWN), got {side_index}")
    if outcome_id < 0:
        raise ValueError(f"outcome_id must be non-negative, got {outcome_id}")
    return 100_000_000 + (int(outcome_id) * 10) + int(side_index)


def parse_outcome_asset_id(asset_id: int) -> Tuple[int, int]:
    """
    Parse wire protocol Asset ID into (outcome_id, side_index).
    """
    if asset_id < 100_000_000:
        raise ValueError(f"Invalid outcome asset_id: {asset_id} (must be >= 100_000_000)")
    rem = int(asset_id) - 100_000_000
    outcome_id = rem // 10
    side_index = rem % 10
    if side_index not in (0, 1):
        raise ValueError(f"Invalid outcome side_index {side_index} parsed from asset_id {asset_id}")
    return outcome_id, side_index


def align_outcome_price(price: Union[float, Decimal, str], max_sig_figs: int = 5) -> str:
    """
    Align a HIP-4 price to the SDK's five-significant-figure rule.

    This deliberately uses ``Decimal`` end-to-end.  Outcome's SDK accepts
    prices down to the FrontendMarket extreme of 0.00001, so a fixed four
    decimal-place tick would silently corrupt valid low-probability quotes.
    """
    p = Decimal(str(price))
    if not p.is_finite() or p <= 0 or p >= 1:
        raise ValueError("Outcome price must be strictly between 0 and 1")
    minimum, maximum = Decimal("0.00001"), Decimal("0.99999")
    p = max(minimum, min(maximum, p))
    decimal_places = max(0, max_sig_figs - 1 - p.adjusted())
    quantum = Decimal(1).scaleb(-decimal_places)
    rounded = p.quantize(quantum, rounding=ROUND_HALF_UP)
    rounded = max(minimum, min(maximum, rounded))
    return format(rounded.normalize(), "f")


def align_outcome_size(size: Union[float, Decimal, str], sz_decimals: int = 0) -> str:
    """
    Align Outcome order size to integer shares by default.

    Outcome ``outcomeMeta`` does not publish a per-market ``szDecimals`` and
    live venue replies reject fractional Outcome share amounts.  Keep an
    explicit override only for isolated test fixtures / a future official
    metadata field, never as the production default.
    """
    s = Decimal(str(size))
    if s <= 0:
        raise ValueError(f"Order size must be positive, got {size}")
    if sz_decimals <= 0:
        return str(int(s.to_integral_value(rounding=ROUND_HALF_UP)))
    fmt = "0." + "0" * sz_decimals
    s_rounded = s.quantize(Decimal(fmt), rounding=ROUND_HALF_UP)
    return format(s_rounded, "f")


def generate_cloid() -> str:
    """
    Generate a unique 128-bit hex client order ID (cloid) formatted as 0x<32-hex-digits>.
    """
    return "0x" + secrets.token_hex(16)


def hash_action(action: Dict[str, Any], vault_address: Optional[str], nonce: int) -> bytes:
    """
    Serialize action to MessagePack and compute keccak256 hash.
    Hyperliquid serializes:
      action dictionary (with custom msgpack packing)
      nonce (uint64 timestamp in ms)
      vaultAddress if present
    """
    packed_action = _pack_action(action)
    
    # Append nonce as 8 bytes big-endian
    nonce_bytes = int(nonce).to_bytes(8, byteorder="big")
    
    # Append vault bytes
    if vault_address is None or vault_address == "" or vault_address == "0x0000000000000000000000000000000000000000":
        vault_bytes = b"\x00"
    else:
        vault_bytes = b"\x01" + bytes.fromhex(vault_address.replace("0x", ""))
        
    full_data = packed_action + nonce_bytes + vault_bytes
    return keccak(full_data)


def construct_phantom_agent(connection_id: bytes, is_mainnet: bool) -> Dict[str, Any]:
    """Build Hyperliquid's canonical L1 EIP-712 message."""
    return {"source": "a" if is_mainnet else "b", "connectionId": connection_id}


class OutcomeAuth:
    """
    Hyperliquid Outcome (HIP-4) Authentication & L1 Action Signer.
    
    Supports:
    - Master EOA wallet (holds funds)
    - Agent Key (sub-account key used for low-latency silent L1 order/cancel signing)
    - Transient Agent Key auto-generation
    """

    def __init__(
        self,
        wallet_address: str,
        private_key: Optional[str] = None,
        agent_private_key: Optional[str] = None,
        is_testnet: bool = False,
        base_url: Optional[str] = None,
        ws_url: Optional[str] = None,
    ) -> None:
        self.wallet_address = wallet_address.strip().lower() if wallet_address else ""
        self.is_testnet = is_testnet
        self.base_url = (
            base_url
            or (HYPERLIQUID_TESTNET_API if is_testnet else HYPERLIQUID_MAINNET_API)
        ).rstrip("/")
        self.ws_url = (
            ws_url
            or (HYPERLIQUID_TESTNET_WS if is_testnet else HYPERLIQUID_MAINNET_WS)
        )

        # Primary EOA account
        self.eoa_account = None
        if private_key:
            pk = private_key.strip()
            if not pk.startswith("0x") and len(pk) == 64:
                pk = "0x" + pk
            self.eoa_account = Account.from_key(pk)
            if not self.wallet_address:
                self.wallet_address = self.eoa_account.address.lower()

        # Agent Key account
        if agent_private_key:
            apk = agent_private_key.strip()
            if not apk.startswith("0x") and len(apk) == 64:
                apk = "0x" + apk
            self.agent_account = Account.from_key(apk)
            self._is_transient_agent = False
        else:
            # If no agent key provided but we have EOA key, we can use EOA directly or generate transient key
            if self.eoa_account:
                self.agent_account = self.eoa_account
                self._is_transient_agent = False
            else:
                # Generate transient agent key in memory
                random_pk = "0x" + secrets.token_hex(32)
                self.agent_account = Account.from_key(random_pk)
                self._is_transient_agent = True

        self.agent_address = self.agent_account.address.lower()
        # A key being present is not proof of the one-time on-chain approval
        # required by HIP-4.  Execution remains blocked until an integration
        # explicitly records successful external approval verification.
        self._agent_authorized = False

    @property
    def is_transient_agent(self) -> bool:
        return self._is_transient_agent

    @property
    def agent_authorized(self) -> bool:
        return self._agent_authorized

    def mark_agent_authorized_after_verification(self) -> None:
        """Enable signing only after the approved agent was independently verified."""
        if self.is_transient_agent:
            raise RuntimeError("A transient agent key cannot be marked authorized")
        self._agent_authorized = True

    def require_agent_authorized(self) -> None:
        if not self._agent_authorized:
            raise RuntimeError(
                "Outcome agent authorization is unverified; complete and verify HIP-4 agent approval first"
            )

    def create_agent_approval_payload(
        self,
        agent_address: Optional[str] = None,
        agent_name: str = "HyperliquidOutcomeBot",
        nonce: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create EIP-712 payload for approving an agent key by EOA.
        """
        target_agent = agent_address or self.agent_address
        current_nonce = nonce if nonce is not None else int(time.time() * 1000)
        chain_id = 1337

        types = {
            "HyperliquidTransaction:ApproveAgent": [
                {"name": "hyperliquidChain", "type": "string"},
                {"name": "agentAddress", "type": "address"},
                {"name": "agentName", "type": "string"},
                {"name": "nonce", "type": "uint64"},
            ],
        }

        domain = {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        }

        message = {
            "hyperliquidChain": "Testnet" if self.is_testnet else "Mainnet",
            "agentAddress": target_agent,
            "agentName": agent_name,
            "nonce": current_nonce,
        }

        action = {
            "type": "approveAgent",
            "hyperliquidChain": "Testnet" if self.is_testnet else "Mainnet",
            "signatureChainId": hex(chain_id),
            "agentAddress": target_agent,
            "agentName": agent_name,
            "nonce": current_nonce,
        }

        if self.eoa_account is None:
            return {
                "action": action,
                "domain": domain,
                "types": types,
                "message": message,
                "requires_eoa_signature": True,
            }

        encoded_data = encode_typed_data(
            domain_data=domain,
            message_types=types,
            message_data=message,
        )
        signed = self.eoa_account.sign_message(encoded_data)
        signature = {
            "r": hex(signed.r),
            "s": hex(signed.s),
            "v": signed.v,
        }

        return {
            "action": action,
            "nonce": current_nonce,
            "signature": signature,
        }

    def sign_l1_action(
        self,
        action: Dict[str, Any],
        nonce: Optional[int] = None,
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Sign an L1 action (order, cancel, userOutcome, etc.) using the Agent Key.
        """
        current_nonce = nonce if nonce is not None else int(time.time() * 1000)
        action_hash_bytes = hash_action(action, vault_address, current_nonce)

        # L1 actions are signed over a *phantom agent* message.  The source is
        # a network marker, not the signing wallet address; see Hyperliquid's
        # reference ``construct_phantom_agent`` implementation.
        message_data = construct_phantom_agent(action_hash_bytes, is_mainnet=not self.is_testnet)

        encoded_data = encode_typed_data(
            domain_data=EXCHANGE_DOMAIN,
            message_types=AGENT_EIP712_TYPES,
            message_data=message_data,
        )
        signed = self.agent_account.sign_message(encoded_data)

        payload: Dict[str, Any] = {
            "action": action,
            "nonce": current_nonce,
            "signature": {
                "r": hex(signed.r),
                "s": hex(signed.s),
                "v": signed.v,
            },
        }
        if vault_address:
            payload["vaultAddress"] = vault_address

        return payload
