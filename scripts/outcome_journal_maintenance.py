#!/usr/bin/env python3
"""Safe maintenance for the Outcome journal's historical raw-WS footprint.

Run ``--report`` while live.  Any rewrite requires an exclusive SQLite lock,
so the command refuses rather than competing with a running trading bot.
``--vacuum`` additionally requires free disk headroom and is never implicit.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path


def _size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _report(path: Path) -> None:
    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        # Do not COUNT a multi-million-row raw WS history during a live
        # health check.  The exact deleted count is emitted by the explicit
        # maintenance operation; report only whether legacy rows remain.
        all_mids = conn.execute(
            """SELECT 1 FROM strategy_events
               WHERE event_type='OUTCOME_WS_ALL_MIDS'
                 AND json_extract(payload_json, '$.raw.recording_scope') IS NULL
               LIMIT 1"""
        ).fetchone() is not None
        pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    print({
        "journal": str(path), "db_bytes": _size(path), "wal_bytes": _size(wal), "shm_bytes": _size(shm),
        "legacy_all_mids_present": all_mids, "page_size": page_size,
        "free_pages": free_pages, "reclaimable_bytes_before_vacuum": free_pages * page_size,
    })


def _exclusive_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("BEGIN EXCLUSIVE")
    except sqlite3.Error:
        conn.close()
        raise RuntimeError("journal is active or locked; stop every bot/collector before maintenance")
    return conn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal-path", default="logs/outcome_shadow.db")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--checkpoint", action="store_true", help="truncate WAL only; requires exclusive lock")
    parser.add_argument("--prune-legacy-all-mids", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    args = parser.parse_args()
    path = Path(args.journal_path)
    if not path.exists():
        raise SystemExit(f"journal does not exist: {path}")
    if args.report or not (args.checkpoint or args.prune_legacy_all_mids or args.vacuum):
        _report(path)
        return 0

    # A delete is deliberately scoped to the payload class that was an
    # accidental all-market mirror.  Fills, P2 snapshots, P3 markouts, order
    # lifecycle and compact new allMids records remain untouched.
    with _exclusive_connection(path) as conn:
        if args.prune_legacy_all_mids:
            deleted = conn.execute(
                """DELETE FROM strategy_events
                   WHERE event_type='OUTCOME_WS_ALL_MIDS'
                     AND json_extract(payload_json, '$.raw.recording_scope') IS NULL"""
            ).rowcount
            print({"pruned_legacy_all_mids_rows": deleted})
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    if args.vacuum:
        # SQLite VACUUM needs a second near-full copy while it works.  Refuse
        # early rather than turning an already-full disk into a failed DB.
        free = shutil.disk_usage(path.parent).free
        required = int(_size(path) * 1.25)
        if free < required:
            raise SystemExit(
                f"refusing VACUUM: need about {required} free bytes, have {free}; "
                "the delete/checkpoint above is complete, free space then retry --vacuum"
            )
        with _exclusive_connection(path) as conn:
            conn.execute("COMMIT")
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _report(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
