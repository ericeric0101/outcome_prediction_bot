"""Calendar features tied to the Outcome daily-market session, not raw dates.

Outcome 1d contracts roll at 14:00 Asia/Taipei.  A timestamp before that
hour belongs to the previous market session, so midnight alone must not
silently change a Friday market into a Saturday example.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


TAIPEI = timezone(timedelta(hours=8))
DAILY_ROLLOVER_HOUR = 14


def market_session_weekday(timestamp_ms: int) -> int:
    """Return the Taipei weekday (Mon=0 … Sun=6) of the market session."""
    observed = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=TAIPEI)
    if observed.hour < DAILY_ROLLOVER_HOUR:
        observed -= timedelta(days=1)
    return observed.weekday()


def market_session_calendar_features(timestamp_ms: int) -> dict[str, float | int | bool]:
    """Compact, repeatable calendar features for research-only models.

    Do not expose a raw date: that would let a model memorize a particular
    market rather than learn a recurring calendar/liquidity pattern.
    """
    weekday = market_session_weekday(timestamp_ms)
    angle = 2.0 * math.pi * weekday / 7.0
    return {
        "market_session_weekday": weekday,
        "market_session_is_weekend": weekday >= 5,
        "market_session_weekday_sin": math.sin(angle),
        "market_session_weekday_cos": math.cos(angle),
    }
