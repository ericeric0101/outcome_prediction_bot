"""Canonical Outcome spot-coin identity helpers.

Hyperliquid account balances encode Outcome coins with ``+`` while the
orderbook and order APIs use ``#``.  Execution code must never make a risk
decision from the transport spelling, so this is the single normalization
boundary shared by recovery and pre-trade risk checks.
"""
from __future__ import annotations


def normalize_outcome_coin(value: object) -> str:
    """Return canonical ``#<asset>`` form for a valid Outcome balance coin."""
    coin = str(value or "")
    if coin.startswith("+") and coin[1:].isdigit():
        return "#" + coin[1:]
    return coin


def is_outcome_coin(value: object) -> bool:
    """Whether *value* identifies an Outcome coin in either venue spelling."""
    return normalize_outcome_coin(value).startswith("#")
