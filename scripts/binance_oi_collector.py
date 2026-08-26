#!/usr/bin/env python3
"""Collect public Binance BTCUSDT futures OI for Outcome 1d research only."""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.binance_oi import BinanceOiCollector, BinanceUsdMFuturesPublicClient
from monitoring.trade_journal_db import TradeJournalDB


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-path", default="logs/outcome_shadow.db")
    parser.add_argument("--interval-sec", type=float, default=30.0)
    parser.add_argument("--cycles", type=int, default=None, help="Default: run until Ctrl-C")
    parser.add_argument("--backfill-limit", type=int, default=500, help="5m historical OI rows to request on startup; 0 skips")
    args = parser.parse_args()
    if args.cycles is not None and args.cycles < 1:
        parser.error("--cycles must be at least 1")
    if args.interval_sec < 1.0:
        parser.error("--interval-sec must be at least 1 second")
    if not 0 <= args.backfill_limit <= 500:
        parser.error("--backfill-limit must be in [0, 500]")

    journal = TradeJournalDB(args.journal_path)
    run_id = f"binance-oi-{uuid.uuid4().hex[:12]}"
    client = BinanceUsdMFuturesPublicClient()
    collector = BinanceOiCollector(journal=journal, run_id=run_id, client=client)
    journal.log_run_start(run_id, "BINANCE_OI_COLLECTOR", True, False, notes={"read_only": True, "symbol": "BTCUSDT"})
    print("Binance OI collector: public read-only market-data requests only; Binance trading is disabled.", flush=True)
    try:
        if args.backfill_limit:
            written = collector.backfill_5m(limit=args.backfill_limit)
            print(f"[BINANCE_OI_BACKFILL] written={written}", flush=True)
        count = 0
        while args.cycles is None or count < args.cycles:
            try:
                written = collector.collect_current()
                print(f"[BINANCE_OI_CYCLE {count + 1}] written={int(written)}", flush=True)
            except Exception as exc:
                journal.log_strategy_event(run_id, "BINANCE_OI_COLLECTION_ERROR", {
                    "source": "binance_usdm_public", "read_only": True,
                    "error_type": type(exc).__name__, "error": str(exc),
                })
                print(f"[BINANCE_OI_RETRY] {type(exc).__name__}: {exc}", flush=True)
            count += 1
            if args.cycles is None or count < args.cycles:
                time.sleep(args.interval_sec)
    except KeyboardInterrupt:
        print("Binance OI collector stopped.", flush=True)
    finally:
        client.close()
        journal.log_run_stop(run_id, {"read_only": True})


if __name__ == "__main__":
    main()
