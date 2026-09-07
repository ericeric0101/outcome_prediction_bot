"""Load the single local Outcome deployment environment.

There are no venue profiles or legacy aliases. Shell/CI variables retain
priority over local ``.env`` values so operations remain explicit.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Deliberately small, current Outcome-only local configuration surface. Legacy
# keys may remain in an old private .env during migration but are never loaded.
LOCAL_ENV_KEYS = frozenset({
    "HL_WALLET_ADDRESS", "HL_PRIVATE_KEY", "HL_AGENT_PRIVATE_KEY", "HL_TESTNET", "HL_BASE_URL", "HL_WS_URL",
    "LIVE_PROCESS_LOCK_PATH",
    "OUTCOME_MARKET_PERIODS", "OUTCOME_MARKET_ALLOW_FALLBACK", "OUTCOME_EXECUTION_JOURNAL_PATH",
    "OUTCOME_RESEARCH_CAPTURE_INTERVAL_SEC", "OUTCOME_RESEARCH_HEARTBEAT_SEC", "OUTCOME_RESEARCH_GAP_ALERT_SEC",
    "BINANCE_OI_HEARTBEAT_SEC", "BINANCE_OI_GAP_ALERT_SEC",
    "OUTCOME_LIVE_STRATEGY_TARGET_RETURN_PCT", "OUTCOME_LIVE_STRATEGY_NARROW_AFTER_SEC",
    "OUTCOME_LIVE_STRATEGY_NARROW_RETURN_PCT", "OUTCOME_LIVE_STRATEGY_FLOOR_AFTER_SEC",
    "OUTCOME_LIVE_STRATEGY_FLOOR_RETURN_PCT", "OUTCOME_LIVE_STRATEGY_SPOT_STRIKE_MIN_BPS",
    "OUTCOME_LIVE_STRATEGY_MARK_RETURN_MIN_BPS", "OUTCOME_LIVE_STRATEGY_OI_RETURN_MIN_BPS",
    "OUTCOME_LIVE_STRATEGY_OI_LOOKBACK_SEC", "OUTCOME_LIVE_STRATEGY_OI_MAX_AGE_SEC",
    "OUTCOME_TIER_B_ENABLED", "OUTCOME_LIVE_STRATEGY_MIN_ENTRY_PRICE",
    "OUTCOME_MAX_ENTRY_NOTIONAL_USDC", "OUTCOME_MAX_OUTCOME_EXPOSURE_USDC", "OUTCOME_MAX_OPEN_ORDERS",
})


def load_runtime_env(
    *,
    repo_root: Path | None = None,
    env_path: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> Path:
    """Load local ``.env`` without overriding shell-provided values."""
    root = (repo_root or PROJECT_ROOT).resolve()
    path = (env_path or (root / ".env")).resolve()
    target = os.environ if environ is None else environ
    external_keys = set(target)
    if path.is_file():
        for key, value in dotenv_values(path).items():
            if key in LOCAL_ENV_KEYS and value is not None and key not in external_keys:
                target[key] = value
    return path
