"""Data-derived, bounded target selection for Outcome 1d maker exits.

The target is not an entry filter.  It converts recent, executable Outcome
mid-price movement into a *net of close fee* target in a conservative 1--5%
band, with an explicit fallback when the current market has too little tape.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


MIN_NET_TARGET = Decimal("0.01")
MAX_NET_TARGET = Decimal("0.05")
FALLBACK_NET_TARGET = Decimal("0.03")
VOLATILITY_WINDOW_MS = 300_000
LOOKBACK_MS = 7_200_000
MIN_RETURNS = 12


@dataclass(frozen=True)
class OutcomeExitTargetDecision:
    target_return_pct: Decimal
    estimated_move_pct: Decimal | None
    sample_count: int
    source: str


def _mid(payload: dict[str, object], side_index: int) -> Decimal | None:
    raw = payload.get("yes_l2" if side_index == 0 else "no_l2")
    if not isinstance(raw, dict):
        return None
    levels = raw.get("levels")
    if not isinstance(levels, list) or len(levels) < 2 or not levels[0] or not levels[1]:
        return None
    try:
        bid = Decimal(str(levels[0][0]["px"]))
        ask = Decimal(str(levels[1][0]["px"]))
    except (KeyError, IndexError, ArithmeticError, TypeError):
        return None
    return (bid + ask) / Decimal("2") if Decimal("0") < bid < ask < Decimal("1") else None


class OutcomeExitTargetPolicy:
    """Read-only estimator over accepted snapshots for the selected market."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    def decide(self, *, outcome_id: int, side_index: int) -> OutcomeExitTargetDecision:
        try:
            with sqlite3.connect(f"file:{Path(self.db_path).resolve()}?mode=ro", uri=True) as conn:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM strategy_events
                    WHERE event_type='OUTCOME_P2_PARITY_SNAPSHOT'
                      AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                      AND json_extract(payload_json, '$.capture_quality.status')='accepted'
                    ORDER BY id DESC LIMIT 1500
                    """, (outcome_id,),
                ).fetchall()
        except sqlite3.Error:
            rows = []
        series: list[tuple[int, Decimal]] = []
        for (raw,) in reversed(rows):
            try:
                payload = json.loads(raw)
                timestamp = int(payload["snapshot_timestamp_ms"])
                midpoint = _mid(payload, side_index)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            if midpoint is not None:
                series.append((timestamp, midpoint))
        if not series:
            return OutcomeExitTargetDecision(FALLBACK_NET_TARGET, None, 0, "fallback_no_accepted_l2")
        latest_ts = series[-1][0]
        series = [item for item in series if item[0] >= latest_ts - LOOKBACK_MS]
        moves: list[Decimal] = []
        prior_index = 0
        for timestamp, midpoint in series:
            target_ts = timestamp - VOLATILITY_WINDOW_MS
            while prior_index + 1 < len(series) and series[prior_index + 1][0] <= target_ts:
                prior_index += 1
            prior_ts, prior_mid = series[prior_index]
            if prior_ts <= target_ts and prior_mid > 0:
                moves.append(abs(midpoint / prior_mid - Decimal("1")))
        if len(moves) < MIN_RETURNS:
            return OutcomeExitTargetDecision(FALLBACK_NET_TARGET, None, len(moves), "fallback_insufficient_l2_returns")
        moves.sort()
        estimate = moves[len(moves) // 2]
        target = min(MAX_NET_TARGET, max(MIN_NET_TARGET, estimate))
        return OutcomeExitTargetDecision(target, estimate, len(moves), "rolling_5m_median_executable_mid_move")
