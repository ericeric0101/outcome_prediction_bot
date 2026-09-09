"""Read-only report for G13 market-regime and toxic-fill shadow evidence."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    """Summarise durable shadow facts without replaying books or placing orders."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT ts, event_type, payload_json FROM strategy_events
               WHERE event_type IN ('OUTCOME_MARKET_REGIME_SHADOW', 'OUTCOME_TOXIC_FILL_SHADOW',
                                    'OUTCOME_LIVE_STRATEGY_ENTRY_PLACED')
               ORDER BY id"""
        ).fetchall()

    regime_rows: list[dict[str, Any]] = []
    toxic_rows: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    latest_regime_by_market: dict[int, dict[str, Any]] = {}
    for row in rows:
        payload = _payload(row["payload_json"])
        if payload.get("period") not in {period, None}:
            continue
        item = {"ts": str(row["ts"]), **payload}
        if row["event_type"] == "OUTCOME_MARKET_REGIME_SHADOW":
            regime_rows.append(item)
            try:
                latest_regime_by_market[int(payload["outcome_id"])] = item
            except (KeyError, TypeError, ValueError):
                continue
        elif row["event_type"] == "OUTCOME_TOXIC_FILL_SHADOW":
            toxic_rows.append(item)
        else:
            try:
                market_id = int(payload["outcome_id"])
            except (KeyError, TypeError, ValueError):
                market_id = -1
            entries.append({
                "ts": str(row["ts"]), "outcome_id": market_id,
                "coin": payload.get("coin"), "entry_tier": payload.get("entry_tier"),
                "entry_reason": payload.get("entry_reason"),
                "shadow_regime_at_or_before_entry": latest_regime_by_market.get(market_id, {}).get("state"),
                "shadow_regime_reason_at_or_before_entry": latest_regime_by_market.get(market_id, {}).get("reason"),
            })
    state_counts = Counter(str(row.get("state") or "UNKNOWN") for row in regime_rows)
    toxic_state_counts = Counter(str(row.get("state") or "UNKNOWN") for row in toxic_rows)
    entry_state_counts = Counter(str(row.get("shadow_regime_at_or_before_entry") or "missing") for row in entries)
    return {
        "report": "outcome_market_regime_shadow",
        "schema_version": 1,
        "period": period,
        "regime_observation_count": len(regime_rows),
        "regime_state_counts": dict(sorted(state_counts.items())),
        "toxic_fill_observation_count": len(toxic_rows),
        "toxic_fill_state_counts": dict(sorted(toxic_state_counts.items())),
        "entries": entries,
        "entry_regime_counts": dict(sorted(entry_state_counts.items())),
        "limits": [
            "Only durable shadow events are counted; no raw L2 replay or queue/fill simulation is performed.",
            "Regime labels have no live entry, cancel, exit, sizing, or IOC authority in G13.",
            "A missing label means the shadow observer had not yet recorded an as-of state, not that the market was a trend.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome market-regime shadow report")
    parser.add_argument("--db", required=True, help="SQLite journal path")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
