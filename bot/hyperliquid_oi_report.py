"""Read-only provenance and as-of comparison for native and Binance BTC OI.

Raw OI quantities have venue-specific contract units.  This report therefore
never compares levels as if they were fungible; it compares collection health
and same-venue percentage changes at explicit, causal as-of timestamps.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class HyperliquidOiComparisonReport:
    hyperliquid_observations: int
    binance_live_observations: int
    matched_asof_pairs: int
    first_hyperliquid_timestamp_ms: int | None
    last_hyperliquid_timestamp_ms: int | None
    last_binance_timestamp_ms: int | None
    median_binance_age_ms: int | None
    return_correlation: float | None
    status: str
    note: str


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone())


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    values = sorted(values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) // 2


def _pearson(values: list[tuple[float, float]]) -> float | None:
    if len(values) < 3:
        return None
    left = [pair[0] for pair in values]
    right = [pair[1] for pair in values]
    left_mean, right_mean = sum(left) / len(left), sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_ss = sum((a - left_mean) ** 2 for a in left)
    right_ss = sum((b - right_mean) ** 2 for b in right)
    if left_ss <= 0 or right_ss <= 0:
        return None
    return numerator / math.sqrt(left_ss * right_ss)


def hyperliquid_oi_comparison_report(
    db_path: str | Path,
    *,
    max_binance_age_ms: int = 60_000,
) -> HyperliquidOiComparisonReport:
    """Compare public observations without serving any live strategy input."""
    path = Path(db_path)
    if not path.exists():
        return HyperliquidOiComparisonReport(0, 0, 0, None, None, None, None, None, "NO_DB", "database does not exist")
    with sqlite3.connect(path) as conn:
        if not _table_exists(conn, "hyperliquid_perp_context_observations"):
            return HyperliquidOiComparisonReport(0, 0, 0, None, None, None, None, None, "NO_HL_DATA", "native Hyperliquid context table is absent")
        hl_rows = conn.execute(
            "SELECT local_received_at_ms, open_interest FROM hyperliquid_perp_context_observations WHERE coin='BTC' ORDER BY local_received_at_ms"
        ).fetchall()
        if not _table_exists(conn, "binance_oi_observations"):
            binance_rows: list[tuple[int, str]] = []
        else:
            binance_rows = conn.execute(
                "SELECT local_received_at_ms, open_interest FROM binance_oi_observations WHERE symbol='BTCUSDT' AND backfilled=0 ORDER BY local_received_at_ms"
            ).fetchall()
    if not hl_rows:
        return HyperliquidOiComparisonReport(0, len(binance_rows), 0, None, None, None, None, None, "NO_HL_DATA", "collector has not received native BTC perp context")
    if not binance_rows:
        return HyperliquidOiComparisonReport(len(hl_rows), 0, 0, int(hl_rows[0][0]), int(hl_rows[-1][0]), None, None, None, "WAITING_FOR_BINANCE", "no live Binance OI rows to compare")

    pairs: list[tuple[int, float, float, int]] = []
    hl_index = 0
    latest_hl: tuple[int, float] | None = None
    for binance_time, binance_oi in binance_rows:
        while hl_index < len(hl_rows) and int(hl_rows[hl_index][0]) <= int(binance_time):
            try:
                latest_hl = (int(hl_rows[hl_index][0]), float(hl_rows[hl_index][1]))
            except (TypeError, ValueError):
                latest_hl = None
            hl_index += 1
        try:
            parsed_binance = float(binance_oi)
        except (TypeError, ValueError):
            continue
        if latest_hl is None or latest_hl[1] <= 0 or parsed_binance <= 0:
            continue
        age = int(binance_time) - latest_hl[0]
        if 0 <= age <= max_binance_age_ms:
            pairs.append((int(binance_time), latest_hl[1], parsed_binance, age))

    returns: list[tuple[float, float]] = []
    for previous, current in zip(pairs, pairs[1:]):
        if previous[1] > 0 and previous[2] > 0:
            returns.append((current[1] / previous[1] - 1.0, current[2] / previous[2] - 1.0))
    note = "Levels are not compared across venues; correlation uses matched percentage changes only."
    status = "READY_FOR_COLLECTION" if len(pairs) < 3 else "DESCRIPTIVE_ONLY"
    return HyperliquidOiComparisonReport(
        hyperliquid_observations=len(hl_rows), binance_live_observations=len(binance_rows),
        matched_asof_pairs=len(pairs), first_hyperliquid_timestamp_ms=int(hl_rows[0][0]),
        last_hyperliquid_timestamp_ms=int(hl_rows[-1][0]), last_binance_timestamp_ms=int(binance_rows[-1][0]),
        median_binance_age_ms=_median([row[3] for row in pairs]), return_correlation=_pearson(returns),
        status=status, note=note,
    )


def as_dict(db_path: str | Path, *, max_binance_age_ms: int = 60_000) -> dict[str, object]:
    return asdict(hyperliquid_oi_comparison_report(db_path, max_binance_age_ms=max_binance_age_ms))
