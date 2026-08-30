"""Read-only MAE/MFE and passive-loss-duration report from S2-0 events."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    with sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            """SELECT ts, payload_json FROM strategy_events
               WHERE event_type='OUTCOME_HOLDING_PATH_OBSERVATION' ORDER BY id"""
        ).fetchall()
    paths: dict[tuple[int, str], list[tuple[str, Decimal]]] = defaultdict(list)
    for ts, raw in rows:
        try:
            payload = json.loads(raw)
            if payload.get("period") != period:
                continue
            key = (int(payload["outcome_id"]), str(payload["coin"]))
            paths[key].append((str(ts), Decimal(str(payload["net_exit_vs_entry_pct"]))))
        except (KeyError, TypeError, ValueError, ArithmeticError, json.JSONDecodeError):
            continue
    result: list[dict[str, Any]] = []
    for (outcome_id, coin), values in paths.items():
        returns = [value for _, value in values]
        breach = [ts for ts, value in values if value <= Decimal("-0.05")]
        result.append({
            "outcome_id": outcome_id, "coin": coin, "observations": len(values),
            "mae_pct": str(min(returns)), "mfe_pct": str(max(returns)),
            "loss_band_breach_observations": len(breach),
            "first_loss_band_breach_ts": breach[0] if breach else None,
        })
    return {"period": period, "paths": result, "path_count": len(result)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome S2 holding-path report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
