"""P3 markout primitives: actual fills only, with no synthetic fill claims."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from bot.outcome_event_bridge import OutcomeFillEvent


@dataclass(frozen=True)
class OutcomeQuote:
    coin: str
    timestamp_ms: int
    bid: Optional[Decimal]
    ask: Optional[Decimal]


@dataclass(frozen=True)
class OutcomeMarkout:
    trade_id: str
    horizon_sec: int
    executable_mark: Optional[Decimal]
    markout_per_share: Optional[Decimal]
    status: str


def markouts_for_fill(
    fill: OutcomeFillEvent,
    quotes: Iterable[OutcomeQuote],
    horizons_sec: tuple[int, ...] = (1, 5, 10, 30),
) -> tuple[OutcomeMarkout, ...]:
    """Use the first later *executable* quote; unknown means no observation."""
    own_quotes = sorted((q for q in quotes if q.coin == fill.coin), key=lambda q: q.timestamp_ms)
    observations = []
    for horizon in horizons_sec:
        target = fill.timestamp_ms + horizon * 1000
        quote = next((item for item in own_quotes if item.timestamp_ms >= target), None)
        executable = None
        if quote is not None:
            executable = quote.bid if fill.side == "BUY" else quote.ask
        if executable is None:
            observations.append(OutcomeMarkout(fill.trade_id, horizon, None, None, "missing_executable_quote"))
        else:
            value = executable - fill.price if fill.side == "BUY" else fill.price - executable
            observations.append(OutcomeMarkout(fill.trade_id, horizon, executable, value, "observed"))
    return tuple(observations)
