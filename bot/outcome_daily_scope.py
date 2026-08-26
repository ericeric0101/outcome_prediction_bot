"""Hard scope boundary for the Outcome BTC daily research runtime.

This repository may retain 15m-era Polymarket modules as behaviour
specifications, but the active Outcome research universe is deliberately one
daily BTC contract.  Keeping this validation in one place prevents a stale
environment variable from silently switching the collector to another period.
"""
from __future__ import annotations

from collections.abc import Mapping

from bot.lifecycle.outcome_lifecycle import parse_period_preferences


OUTCOME_DAILY_PERIOD = "1d"


def daily_only_period_preferences(value: str | None) -> tuple[str, ...]:
    """Return the only permitted Outcome research period or fail closed."""
    periods = parse_period_preferences(value, default=(OUTCOME_DAILY_PERIOD,))
    if periods != (OUTCOME_DAILY_PERIOD,):
        raise ValueError(
            "Outcome BTC research is 1d-only; set OUTCOME_MARKET_PERIODS=1d"
        )
    return periods


def daily_only_allow_fallback(value: str | None) -> bool:
    """Reject period fallback; it would turn a missing daily contract into a different strategy."""
    if value is None:
        return False
    allowed = value.strip().lower() not in {"0", "false", "no", "off"}
    if allowed:
        raise ValueError(
            "Outcome BTC research is 1d-only; set OUTCOME_MARKET_ALLOW_FALLBACK=0"
        )
    return False


def resolve_daily_outcome_scope(environ: Mapping[str, str]) -> tuple[tuple[str, ...], bool]:
    """Read and validate the two supported market-selection environment values."""
    return (
        daily_only_period_preferences(environ.get("OUTCOME_MARKET_PERIODS")),
        daily_only_allow_fallback(environ.get("OUTCOME_MARKET_ALLOW_FALLBACK")),
    )
