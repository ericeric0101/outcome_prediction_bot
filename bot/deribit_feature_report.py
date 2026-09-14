"""Read-only Deribit public-feature availability report."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class DeribitFeatureQualityReport:
    snapshots: int
    valid_snapshots: int
    unavailable_snapshots: int
    first_timestamp: str | None
    last_timestamp: str | None
    status_events: int


def deribit_feature_quality_report(db_path: str | Path) -> DeribitFeatureQualityReport:
    with sqlite3.connect(str(db_path)) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_events'").fetchone()
        if not exists:
            return DeribitFeatureQualityReport(0, 0, 0, None, None, 0)
        rows = conn.execute(
            "SELECT ts, payload_json FROM strategy_events "
            "WHERE event_type='DERIBIT_FEATURE_SNAPSHOT' ORDER BY id"
        ).fetchall()
        statuses = conn.execute("SELECT count(*) FROM strategy_events WHERE event_type='DERIBIT_RESEARCH_STATUS'").fetchone()[0]
    valid = sum(bool(json.loads(payload or "{}").get("valid")) for _ts, payload in rows)
    return DeribitFeatureQualityReport(
        snapshots=len(rows), valid_snapshots=valid, unavailable_snapshots=len(rows) - valid,
        first_timestamp=rows[0][0] if rows else None, last_timestamp=rows[-1][0] if rows else None,
        status_events=int(statuses),
    )


def as_json(db_path: str | Path) -> dict[str, object]:
    return asdict(deribit_feature_quality_report(db_path))
