"""Read-only official Outcome activity vs local journal reconciliation.

The official SDK activity endpoint is a 30-day fill window.  This module does
not repair, alter, or authorize anything; it produces a bounded drift report
which an operator can investigate before trusting local FIFO analytics.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class OutcomeActivityReconciliationReport:
    official_trade_count: int
    local_fill_count: int
    matched_trade_count: int
    official_missing_locally: tuple[str, ...]
    local_missing_officially: tuple[str, ...]
    fifo_unknown_trade_references: tuple[str, ...]
    status: str
    limitation: str


def _official_trade_ids(activity: Iterable[Mapping[str, Any]]) -> set[str]:
    return {
        str(item.get("id")) for item in activity
        if str(item.get("type", "")) == "trade" and item.get("id") is not None and str(item.get("id"))
    }


def reconcile_official_activity(
    db_path: str | Path,
    activity: Iterable[Mapping[str, Any]],
) -> OutcomeActivityReconciliationReport:
    """Compare exact official trade ids against locally recorded Outcome fills."""
    official = _official_trade_ids(activity)
    path = Path(db_path)
    if not path.exists():
        return OutcomeActivityReconciliationReport(
            len(official), 0, 0, tuple(sorted(official)), (), (), "NO_LOCAL_DB",
            "Official activity is a rolling 30-day trade window; no local journal exists.",
        )
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT json_extract(payload_json, '$.trade_id')
            FROM order_events
            WHERE event_type='ORDER_FILLED'
              AND json_extract(payload_json, '$.venue')='hyperliquid_outcome'
              AND json_extract(payload_json, '$.actual_fill')=1
              AND COALESCE(json_extract(payload_json, '$.trade_id'), '') <> ''
            """
        ).fetchall()
        local = {str(row[0]) for row in rows if row[0] is not None}
        lots_exist = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcome_realized_pnl_lots'"
        ).fetchone()
        fifo_refs: set[str] = set()
        if lots_exist:
            lot_rows = conn.execute(
                "SELECT close_trade_id, open_trade_id FROM outcome_realized_pnl_lots"
            ).fetchall()
            fifo_refs = {str(value) for row in lot_rows for value in row if value is not None and str(value)}
    matched = official & local
    missing_local = tuple(sorted(official - local))
    missing_official = tuple(sorted(local - official))
    unknown_fifo = tuple(sorted(fifo_refs - local))
    status = "MATCHED" if not missing_local and not unknown_fifo else "DRIFT_DETECTED"
    return OutcomeActivityReconciliationReport(
        official_trade_count=len(official), local_fill_count=len(local), matched_trade_count=len(matched),
        official_missing_locally=missing_local, local_missing_officially=missing_official,
        fifo_unknown_trade_references=unknown_fifo, status=status,
        limitation="Official activity contains only the SDK's rolling 30-day trade mapping; local-only IDs may be older than that window or venue activity the SDK does not map as trade.",
    )


def as_dict(db_path: str | Path, activity: Iterable[Mapping[str, Any]]) -> dict[str, object]:
    return asdict(reconcile_official_activity(db_path, activity))
