"""P3 markout primitives: actual fills only, with no synthetic fill claims."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from bot.outcome_event_bridge import OutcomeFillEvent


# The live research collector is intentionally a 5-second process.  It cannot
# make a defensible one-second observation, so 1s is not emitted as a fake
# "first later snapshot" label.
P3_MARKOUT_SCHEMA_VERSION = 2
P3_MARKOUT_HORIZONS_SEC = (5, 10, 30)
P3_MARKOUT_TOLERANCE_MS = 2_500


@dataclass(frozen=True)
class OutcomeQuote:
    coin: str
    timestamp_ms: int
    bid: Optional[Decimal]
    ask: Optional[Decimal]
    snapshot_event_id: Optional[int] = None


@dataclass(frozen=True)
class OutcomeMarkout:
    trade_id: str
    horizon_sec: int
    executable_mark: Optional[Decimal]
    markout_per_share: Optional[Decimal]
    status: str
    quote_timestamp_ms: Optional[int] = None
    actual_elapsed_ms: Optional[int] = None
    target_lag_ms: Optional[int] = None
    snapshot_event_id: Optional[int] = None


def markouts_for_fill(
    fill: OutcomeFillEvent,
    quotes: Iterable[OutcomeQuote],
    horizons_sec: tuple[int, ...] = P3_MARKOUT_HORIZONS_SEC,
    tolerance_ms: int = P3_MARKOUT_TOLERANCE_MS,
) -> tuple[OutcomeMarkout, ...]:
    """Return only quotes that are genuinely close to each requested horizon.

    A 5-second sampler cannot label its next successful observation as "1s".
    For each target, choose the nearest post-fill quote within the explicit
    tolerance window.  Missing cadence is missing research evidence, never a
    stretched horizon label.
    """
    if tolerance_ms < 0:
        raise ValueError("tolerance_ms must be non-negative")
    own_quotes = sorted((q for q in quotes if q.coin == fill.coin), key=lambda q: q.timestamp_ms)
    observations = []
    for horizon in horizons_sec:
        target = fill.timestamp_ms + horizon * 1000
        eligible = [
            item for item in own_quotes
            if item.timestamp_ms >= fill.timestamp_ms and abs(item.timestamp_ms - target) <= tolerance_ms
        ]
        quote = min(eligible, key=lambda item: (abs(item.timestamp_ms - target), item.timestamp_ms)) if eligible else None
        executable = None
        if quote is not None:
            executable = quote.bid if fill.side == "BUY" else quote.ask
        if executable is None:
            status = "missing_horizon_quote" if not eligible else "missing_executable_quote"
            observations.append(OutcomeMarkout(fill.trade_id, horizon, None, None, status))
        else:
            value = executable - fill.price if fill.side == "BUY" else fill.price - executable
            observations.append(OutcomeMarkout(
                fill.trade_id, horizon, executable, value, "observed",
                quote_timestamp_ms=quote.timestamp_ms,
                actual_elapsed_ms=quote.timestamp_ms - fill.timestamp_ms,
                target_lag_ms=quote.timestamp_ms - target,
                snapshot_event_id=quote.snapshot_event_id,
            ))
    return tuple(observations)
