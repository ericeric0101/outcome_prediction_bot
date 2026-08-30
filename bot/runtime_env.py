"""Load a versioned strategy profile plus a small local deployment environment.

The profile contains reviewed strategy defaults.  ``.env`` contains secrets and
the supported operator overrides.  Existing process environment variables keep
the highest priority, which makes shell/CI overrides predictable.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping, MutableMapping

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = PROJECT_ROOT / "config" / "profiles"
DEFAULT_PROFILE = "btc15_twap_v3"

# Supported day-to-day deployment surface. Advanced values belong in the
# versioned profile so a strategy change is reviewed as code, not hidden in a
# machine-local file. Keep this list deliberately small.
CORE_ENV_KEYS = frozenset(
    {
        "STRATEGY_PROFILE",
        "POLYMARKET_PK",
        "POLYMARKET_FUNDER",
        "POLYGON_RPC_URL",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_PASSPHRASE",
        "POLYMARKET_SIGNATURE_TYPE",
        "POLYMARKET_CHAIN_ID",
        "POLYMARKET_CLOB_BASE_URL",
        "POLYMARKET_GAMMA_API",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_CHAT_ID",
        "POLYMARKET_CHAINLINK_TWAP_ENABLED",
        "POLYMARKET_CHAINLINK_TWAP_WINDOW_SEC",
        "REQUIRE_TWAP_REFERENCE_SPOT",
        "TWAP_DEGRADED_BLOCK_NEW_ENTRIES",
        "QUOTE_STALE_SEC",
        "QUOTE_RESUBSCRIBE_GRACE_SEC",
        "AUTO_NODE_ROLLOVER_ENABLED",
        "AUTO_NODE_RESTART_ON_UNEXPECTED_EXIT",
        "ENTRY_SCORE_MIN",
        "FIRST_ENTRY_SCORE_MIN",
        "FIRST_ENTRY_MAX_TIME_LEFT_SEC",
        "ENTRY_MIN_TIME_LEFT_SEC",
        "ENTRY_MAX_FAIR_PRICE",
        "EXTERNAL_CONFIRMATION_ENABLED",
        "EXTERNAL_CONFLICT_BOOK_MID_THRESHOLD_PS",
        "EXTERNAL_CONFLICT_ACTION",
        "ENTRY_MIN_ROBUST_NET_USDC",
        "EXECUTION_COST_MODE",
        "EXECUTION_COST_LOOKBACK_HOURS",
        "EXECUTION_COST_MIN_SAMPLES",
        "MARKET_TARGET_SHARES",
        "HIGH_PRICE_THRESHOLD",
        "HIGH_PRICE_TARGET_SHARES",
        "MARKET_MAX_POSITION_SHARES",
        "ORDER_POST_ONLY",
        "ORDER_TTL_SEC",
        "ORDER_REQUOTE_MIN_AGE_SEC",
        "ORDER_REQUOTE_HYSTERESIS_TICKS",
        "HOLD_TO_REDEEM",
        "RECOVERY_EXIT_ENABLED",
        "RECOVERY_EXIT_REQUIRE_TWAP_CONFIRMATION",
        "RECOVERY_EXIT_MAX_TIME_LEFT_SEC",
        "RECOVERY_EXIT_MIN_HOLD_SEC",
        "AUTO_REDEEM_ENABLED",
        "TRADE_DB_ENABLED",
        "TRADE_DB_PATH",
        "DASHBOARD_THEME",
        "TERMINAL_DASHBOARD",
        "FAIR_EDGE_BUCKET_SHADOW_ENABLED",
        "SHADOW_SIMULATION_ENABLED",
        "STARTUP_VERBOSE",
    }
)

# Secrets and host-specific credentials must never be migrated into a tracked
# profile even if they are not part of the operator surface.
SENSITIVE_ENV_KEYS = frozenset(
    {
        "POLYMARKET_WALLET_ADDRESS",
        "HL_WALLET_ADDRESS",
        "HYPERLIQUID_WALLET_ADDRESS",
        "HL_PRIVATE_KEY",
        "HYPERLIQUID_PRIVATE_KEY",
        "HL_AGENT_PRIVATE_KEY",
        "HYPERLIQUID_AGENT_PRIVATE_KEY",
        "HL_TESTNET",
        "HYPERLIQUID_TESTNET",
        "HL_BASE_URL",
        "HYPERLIQUID_BASE_URL",
        "HL_WS_URL",
        "HYPERLIQUID_WS_URL",
        "HL_MIN_NOTIONAL_USDC",
        "HL_REFERRAL_CODE",
        "POLYGON_RPC_URL",
        "POLYMARKET_PK",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_PASSPHRASE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_CHAT_ID",
        "TELEGRAM_POLLING_LOCK_PATH",
        "LIVE_PROCESS_LOCK_PATH",
    }
)

# Canonical names in .env map to legacy readers that have not yet been
# converged. The mapping is intentionally one-way.
# Migration-only compatibility map. Runtime configuration no longer applies
# these aliases to its process environment; the migration tool uses this map
# to convert old local files into the supported operator surface.
CANONICAL_TO_LEGACY = {
    "ENTRY_SCORE_MIN": "DIRECTIONAL_ENTRY_MIN_SCORE_ABS_NEW",
    "FIRST_ENTRY_SCORE_MIN": "DIRECTIONAL_FIRST_ENTRY_MIN_SCORE_ABS_NEW",
    "ENTRY_MAX_FAIR_PRICE": "MAKER_MAX_FAIR_PRICE",
    "EXTERNAL_CONFIRMATION_ENABLED": "EXTERNAL_ENTRY_CONFIRMATION_ENABLED",
    "EXTERNAL_CONFLICT_BOOK_MID_THRESHOLD_PS": "EXTERNAL_ENTRY_CONFIRMATION_BOOK_MID_THRESHOLD_PS",
    "ENTRY_MIN_ROBUST_NET_USDC": "MAKER_MIN_EXPECTED_NET_USDC",
    "EXECUTION_COST_LOOKBACK_HOURS": "MAKER_EXECUTION_EMPIRICAL_MARKOUT_LOOKBACK_HOURS",
    "EXECUTION_COST_MIN_SAMPLES": "MAKER_EXECUTION_EMPIRICAL_MARKOUT_MIN_SAMPLES",
    "MARKET_TARGET_SHARES": "MAKER_FIXED_SHARES",
    "HIGH_PRICE_THRESHOLD": "MAKER_HIGH_ENTRY_PRICE_SIZE_ADJUST_THRESHOLD",
    "RECOVERY_EXIT_ENABLED": "TAKER_EXIT_ENABLED",
    "RECOVERY_EXIT_REQUIRE_TWAP_CONFIRMATION": "TAKER_EXIT_REQUIRE_TWAP_CONFIRMATION",
    "RECOVERY_EXIT_MAX_TIME_LEFT_SEC": "TAKER_EXIT_MAX_TIME_LEFT_SEC",
    "RECOVERY_EXIT_MIN_HOLD_SEC": "TAKER_EXIT_MIN_HOLD_SEC",
}


def _set_if_not_external(
    environ: MutableMapping[str, str], name: str, value: str | None, external_keys: set[str]
) -> None:
    if value is not None and name not in external_keys:
        environ[name] = value


def _profile_path(profile_name: str, repo_root: Path) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", profile_name):
        raise ValueError(f"Invalid STRATEGY_PROFILE: {profile_name!r}")
    path = repo_root / "config" / "profiles" / f"{profile_name}.env"
    if not path.is_file():
        raise FileNotFoundError(f"Strategy profile not found: {path}")
    return path


def load_runtime_env(
    *,
    repo_root: Path | None = None,
    env_path: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> Path:
    """Load profile then local .env without overriding shell-provided values."""
    root = (repo_root or PROJECT_ROOT).resolve()
    local_env_path = (env_path or (root / ".env")).resolve()
    target_environ = os.environ if environ is None else environ
    external_keys = set(target_environ)
    local_values: Mapping[str, str | None] = (
        dotenv_values(local_env_path) if local_env_path.is_file() else {}
    )
    profile_name = (
        target_environ.get("STRATEGY_PROFILE")
        or local_values.get("STRATEGY_PROFILE")
        or DEFAULT_PROFILE
    )
    profile_path = _profile_path(str(profile_name), root)

    for key, value in dotenv_values(profile_path).items():
        _set_if_not_external(target_environ, key, value, external_keys)
    for key, value in local_values.items():
        _set_if_not_external(target_environ, key, value, external_keys)
    return profile_path
