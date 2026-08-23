#!/usr/bin/env python3
"""Run the Hyperliquid Outcome read-only shadow collector.

This command never imports an execution adapter and never submits an exchange
action.  It is the supported data-collection entry point before live trading.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.adapters.outcome_auth import OutcomeAuth
from bot.adapters.outcome_client import OutcomeClient
from bot.app_config import AppConfig
from bot.outcome_shadow_runner import (
    OutcomeShadowRunner,
    build_shadow_risk_components,
    build_shadow_telemetry_config,
)
from bot.outcome_spec_audit import OutcomeSpecAudit
from bot.outcome_ws_recorder import OutcomeWebSocketRecorder
from bot.runtime_env import load_runtime_env
from monitoring.trade_journal_db import TradeJournalDB


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Hyperliquid Outcome shadow collector")
    parser.add_argument("--cycles", type=int, default=None, help="Number of collection cycles (default: run until Ctrl-C)")
    parser.add_argument("--interval-sec", type=float, default=5.0, help="Seconds between cycles (default: 5)")
    parser.add_argument("--journal-path", default=None, help="Override SQLite journal path")
    parser.add_argument(
        "--ws", action="store_true",
        help="Also record read-only WebSocket L2, allMids, and trades with reconnect/resync evidence",
    )
    args = parser.parse_args()
    if args.cycles is not None and args.cycles < 1:
        parser.error("--cycles must be at least 1")

    load_runtime_env()
    wallet = os.getenv("HL_WALLET_ADDRESS") or os.getenv("HYPERLIQUID_WALLET_ADDRESS")
    if not wallet:
        parser.error("HL_WALLET_ADDRESS is required; no private or agent key is needed")
    config = AppConfig.from_env(enable_terminal_dashboard=False)
    auth = OutcomeAuth(
        wallet_address=wallet,
        is_testnet=(os.getenv("HL_TESTNET", "0").lower() in {"1", "true", "yes", "on"}),
        base_url=os.getenv("HL_BASE_URL") or None,
        ws_url=os.getenv("HL_WS_URL") or None,
    )
    exit_policy, position_manager, exit_engine = build_shadow_risk_components(config)
    client = OutcomeClient(auth)
    journal = TradeJournalDB(args.journal_path or config.operations.trade_db_path)
    runner = OutcomeShadowRunner(
        client=client, wallet_address=wallet, journal=journal,
        exit_policy=exit_policy, position_manager=position_manager, exit_engine=exit_engine,
        slippage_buffer_pct=config.exit.taker_exit_slippage_buffer_pct,
        telemetry_config=build_shadow_telemetry_config(config),
    )
    runner.spec_audit = OutcomeSpecAudit(journal, runner.run_id)
    if args.ws:
        runner.ws_recorder = OutcomeWebSocketRecorder(client, journal, runner.run_id)
    print("Outcome shadow mode: read-only /info requests only; exchange submission is disabled.")
    if args.ws:
        print("Outcome WebSocket recorder enabled: L2, allMids, and trades remain read-only.")
    try:
        runner.run(cycles=args.cycles, interval_sec=args.interval_sec)
    except KeyboardInterrupt:
        print("Outcome shadow collector stopped.")


if __name__ == "__main__":
    main()
