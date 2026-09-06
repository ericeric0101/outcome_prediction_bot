"""Shared lifecycle enumerations for the active Outcome runtime."""
from __future__ import annotations

from enum import Enum


class MarketPhase(Enum):
    """Market lifecycle phases for BTC 15-min markets."""
    WAITING = "WAITING"           # No active market; searching for next one
    ACTIVE = "ACTIVE"             # Market is live, quoting is allowed
    REDUCE_ONLY = "REDUCE_ONLY"   # Close to market end, BUY blocked
    SETTLING = "SETTLING"         # Market has ended, all orders cancelled


class ActiveSide(Enum):
    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"
