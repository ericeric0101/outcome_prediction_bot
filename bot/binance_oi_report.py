"""Read-only quality report for Binance OI collection provenance."""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class BinanceOiQualityReport:
    observation_count: int
    live_count: int
    backfilled_count: int
    first_exchange_timestamp_ms: int | None
    last_exchange_timestamp_ms: int | None
    max_live_gap_ms: int | None
    max_request_latency_ms: float | None
    symbols: tuple[str, ...]


def binance_oi_quality_report(db_path: str | Path) -> BinanceOiQualityReport:
    path = Path(db_path)
    if not path.exists():
        return BinanceOiQualityReport(0, 0, 0, None, None, None, None, ())
    with sqlite3.connect(path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='binance_oi_observations'"
        ).fetchone()
        if not exists:
            return BinanceOiQualityReport(0, 0, 0, None, None, None, None, ())
        rows = conn.execute(
            """
            SELECT exchange_timestamp_ms, backfilled, request_latency_ms, symbol
            FROM binance_oi_observations
            ORDER BY exchange_timestamp_ms ASC
            """
        ).fetchall()
    if not rows:
        return BinanceOiQualityReport(0, 0, 0, None, None, None, None, ())
    live_times = [int(row[0]) for row in rows if not bool(row[1])]
    gaps = [right - left for left, right in zip(live_times, live_times[1:]) if right >= left]
    return BinanceOiQualityReport(
        observation_count=len(rows),
        live_count=len(live_times),
        backfilled_count=sum(bool(row[1]) for row in rows),
        first_exchange_timestamp_ms=int(rows[0][0]),
        last_exchange_timestamp_ms=int(rows[-1][0]),
        max_live_gap_ms=max(gaps) if gaps else None,
        max_request_latency_ms=max(float(row[2]) for row in rows),
        symbols=tuple(sorted({str(row[3]) for row in rows})),
    )


def as_dict(db_path: str | Path) -> dict[str, object]:
    return asdict(binance_oi_quality_report(db_path))
