"""Read-only replay of E0 exit plans over recorded Outcome P2/P3 facts."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.outcome_exit_quote_planner import ExitQuoteInput, OutcomeExitQuotePlanner
from bot.outcome_p3_calibration import take_profit_price
from monitoring.trade_journal_db import TradeJournalDB


@dataclass(frozen=True)
class ExitReplayReport:
    run_id: str
    snapshots_considered: int
    plans_written: int
    keep_count: int
    replace_count: int
    block_count: int


def _top(book: dict[str, Any]) -> tuple[Decimal, Decimal] | None:
    try:
        levels = book["levels"]
        return Decimal(str(levels[0][0]["px"])), Decimal(str(levels[1][0]["px"]))
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def replay_exit_quotes(*, db_path: str | Path, period: str = "1d", run_id: str | None = None,
                       target_return_pct: Decimal = Decimal("0.05"), loss_reprice_pct: Decimal = Decimal("0.05"),
                       maker_close_fee_rate: Decimal = Decimal("0.0004"), now_ms: int | None = None) -> ExitReplayReport:
    """Write counterfactual plans only; it never instantiates an execution gateway."""
    journal = TradeJournalDB(db_path)
    run_id = run_id or f"outcome-exit-replay-{uuid.uuid4().hex[:10]}"
    planner = OutcomeExitQuotePlanner()
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    with sqlite3.connect(journal.db_path) as conn:
        snapshots = conn.execute("SELECT id, payload_json FROM strategy_events WHERE event_type='OUTCOME_P2_PARITY_SNAPSHOT' ORDER BY id").fetchall()
        fills = conn.execute("SELECT payload_json, price, qty, side FROM order_events WHERE event_type='ORDER_FILLED' ORDER BY id").fetchall()
    buy_by_coin: dict[str, tuple[int, Decimal, Decimal]] = {}
    for raw, price, qty, side in fills:
        try:
            payload = json.loads(raw)
            if str(side).upper() != "BUY" or payload.get("venue") != "hyperliquid_outcome" or payload.get("period") != period:
                continue
            coin, timestamp = str(payload["coin"]), int(payload["timestamp_ms"])
            buy_by_coin.setdefault(coin, (timestamp, Decimal(str(price)), Decimal(str(qty))))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    considered = written = keep = replace = block = 0
    for event_id, raw in snapshots:
        try:
            payload = json.loads(raw)
            if payload.get("venue") != "hyperliquid_outcome" or payload.get("period") != period:
                continue
            timestamp = int(payload["snapshot_timestamp_ms"])
            outcome_id = int(payload["outcome_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        for coin_key, book_key in (("yes_coin", "yes_l2"), ("no_coin", "no_l2")):
            coin = str(payload.get(coin_key) or "")
            fill = buy_by_coin.get(coin)
            if fill is None or timestamp < fill[0]:
                continue
            top = _top(payload.get(book_key) or {})
            if top is None:
                continue
            considered += 1
            fill_ts, entry, inventory = fill
            existing = take_profit_price(entry_price=entry, target_return_pct=target_return_pct, maker_close_fee_rate=maker_close_fee_rate)
            if existing is None:
                continue
            plan = planner.plan(ExitQuoteInput(
                inventory=inventory, fill_vwap=entry, maker_close_fee_rate=maker_close_fee_rate,
                minimum_return_pct=target_return_pct, loss_reprice_pct=loss_reprice_pct,
                existing_order_id=f"replay-{coin}-{fill_ts}", existing_price=existing,
                best_bid=top[0], best_ask=top[1], book_age_sec=0.0, now_ts=timestamp / 1000.0,
            ))
            journal.log_strategy_event(run_id, "OUTCOME_EXIT_REQUOTE_REPLAY", {
                "venue": "hyperliquid_outcome", "read_only": True, "counterfactual": True,
                "source_snapshot_event_id": event_id, "outcome_id": outcome_id, "period": period, "coin": coin,
                "snapshot_timestamp_ms": timestamp, "entry_fill_timestamp_ms": fill_ts,
                "entry_vwap": str(entry), "inventory": str(inventory), "best_bid": str(top[0]), "best_ask": str(top[1]),
                "plan": {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(plan).items()},
                "execution_submitted": False,
            })
            written += 1
            if plan.action.value == "KEEP": keep += 1
            elif plan.action.value == "CANCEL_REPLACE": replace += 1
            else: block += 1
    return ExitReplayReport(run_id, considered, written, keep, replace, block)
