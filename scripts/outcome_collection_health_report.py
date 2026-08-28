#!/usr/bin/env python3
"""Read-only liveness report for the Outcome P2/P3 and Binance OI collectors."""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--db", default="logs/outcome_shadow.db")
parser.add_argument("--stale-sec", type=float, default=120.0)
args = parser.parse_args()
if args.stale_sec <= 0:
    parser.error("--stale-sec must be positive")

events = ("BINANCE_OI_HEARTBEAT", "OUTCOME_RESEARCH_CAPTURE_HEARTBEAT")
result: dict[str, object] = {"db": args.db, "stale_sec": args.stale_sec, "collectors": {}}
path = Path(args.db)
if path.exists():
    now = datetime.now(timezone.utc)
    with sqlite3.connect(path) as conn:
        for event in events:
            row = conn.execute("SELECT ts,payload_json FROM strategy_events WHERE event_type=? ORDER BY id DESC LIMIT 1", (event,)).fetchone()
            alert_event = event.replace("_HEARTBEAT", "_GAP_ALERT")
            alerts = conn.execute("SELECT COUNT(*) FROM strategy_events WHERE event_type=?", (alert_event,)).fetchone()[0]
            if row:
                observed = datetime.fromisoformat(row[0])
                age = (now - observed).total_seconds()
                result["collectors"][event] = {"last_heartbeat": row[0], "age_sec": round(age, 3), "healthy": age <= args.stale_sec, "gap_alert_count": int(alerts), "payload": json.loads(row[1] or "{}")}
            else:
                result["collectors"][event] = {"last_heartbeat": None, "healthy": False, "gap_alert_count": int(alerts), "reason": "no_heartbeat_recorded"}
print(json.dumps(result, indent=2, sort_keys=True))
