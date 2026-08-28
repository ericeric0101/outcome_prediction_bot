"""Read-only X3 data-quality report."""
from __future__ import annotations
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from bot.outcome_oi_features import LABEL_HORIZONS_SEC

@dataclass(frozen=True)
class OutcomeOiFeatureReport:
    snapshot_rows: int
    oi_joined_rows: int
    max_oi_age_ms: int | None
    backfilled_joined_rows: int
    label_coverage: dict[int, int]
    actual_maker_fill_rows: int
    actual_maker_fill_rows_with_oi: int

def outcome_oi_feature_report(db_path: str | Path) -> OutcomeOiFeatureReport:
    path = Path(db_path)
    if not path.exists():
        return OutcomeOiFeatureReport(0, 0, None, 0, {h: 0 for h in LABEL_HORIZONS_SEC}, 0, 0)
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "outcome_oi_feature_rows" not in tables:
            return OutcomeOiFeatureReport(0, 0, None, 0, {h: 0 for h in LABEL_HORIZONS_SEC}, 0, 0)
        rows = conn.execute("SELECT oi_observation_id,oi_age_ms,oi_backfilled,labels_json FROM outcome_oi_feature_rows").fetchall()
        fill = conn.execute("SELECT COUNT(*),SUM(oi_observation_id IS NOT NULL) FROM outcome_oi_fill_feature_rows").fetchone() if "outcome_oi_fill_feature_rows" in tables else (0, 0)
    coverage = {h: 0 for h in LABEL_HORIZONS_SEC}
    for _, _, _, raw in rows:
        try: labels = json.loads(raw)
        except (TypeError, json.JSONDecodeError): continue
        for horizon in LABEL_HORIZONS_SEC:
            coverage[horizon] += int(bool(labels.get(f"future_{horizon}s", {}).get("available")))
    ages = [int(row[1]) for row in rows if row[1] is not None]
    return OutcomeOiFeatureReport(len(rows), sum(row[0] is not None for row in rows), max(ages) if ages else None,
        sum(bool(row[2]) for row in rows), coverage, int(fill[0] or 0), int(fill[1] or 0))

def as_dict(db_path: str | Path) -> dict[str, object]:
    return asdict(outcome_oi_feature_report(db_path))
