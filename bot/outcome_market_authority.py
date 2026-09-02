"""Small atomic read-only authority export for downstream observers."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


DEFAULT_AUTHORITY_PATH = "/Users/cheng-kaihuang/hyperliquid_prediction_bot/logs/outcome_market_authority.json"


def publish_outcome_market_authority(market: Any, *, path: str | None = None) -> None:
    """Atomically publish the currently selected Outcome market, never a journal scan."""
    target = Path(path or os.getenv("OUTCOME_MARKET_AUTHORITY_PATH", DEFAULT_AUTHORITY_PATH))
    payload = {
        "schema_version": 1,
        "market_id": int(market.outcome_id),
        "period": str(market.period),
        "side0_coin": str(market.yes_coin),
        "side1_coin": str(market.no_coin),
        "strike": str(market.strike),
        "expiry": str(market.expiry_str),
        "updated_at_ms": int(time.time() * 1000),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(target)
