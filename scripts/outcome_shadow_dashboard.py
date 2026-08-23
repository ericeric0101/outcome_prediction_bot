#!/usr/bin/env python3
"""Terminal dashboard for the read-only Outcome shadow journal."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def load_latest_cycle(db_path: Path) -> tuple[Optional[str], dict[str, Any]]:
    if not db_path.exists():
        return None, {}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1) as conn:
            row = conn.execute(
                "SELECT ts, payload_json FROM strategy_events "
                "WHERE event_type='OUTCOME_SHADOW_CYCLE' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None, {}
        return str(row[0]), json.loads(row[1] or "{}")
    except (sqlite3.Error, json.JSONDecodeError):
        return None, {}


def _decimal(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def render(ts: Optional[str], payload: dict[str, Any], db_path: Path) -> Group:
    if not payload:
        return Group(Panel(
            f"Waiting for OUTCOME_SHADOW_CYCLE in\n{db_path}\n\n"
            "Start the collector in another terminal.",
            title="Outcome Shadow Dashboard", border_style="yellow",
        ))
    telemetry = payload.get("strategy_telemetry") or {}
    forecast = telemetry.get("forecast") or {}
    signal = telemetry.get("signal") or {}
    snapshots = payload.get("market_snapshots") or []
    market = Table.grid(expand=True)
    market.add_column(style="bold cyan", width=16)
    market.add_column()
    market.add_column(style="bold cyan", width=16)
    market.add_column()
    market.add_row("Outcome", f"#{payload.get('outcome_id', '—')} ({payload.get('period', '—')})", "Updated UTC", ts or "—")
    market.add_row("BTC mark", _decimal(forecast.get("spot"), 2), "Strike", _decimal(forecast.get("strike"), 2))
    market.add_row("Proposed side", str(telemetry.get("proposed_side", "NONE")), "Entry eligible", str(telemetry.get("entry_eligible", False)))
    market.add_row("Execution", "BLOCKED" if telemetry.get("execution_blocked") else "INVALID", "Sigma", _decimal(forecast.get("sigma"), 4))

    book = Table(title="Outcome L2 snapshot", expand=True)
    for column in ("Side", "Bid", "Ask", "Spread", "Fair", "Fair edge", "Time left"):
        book.add_column(column, justify="right" if column != "Side" else "left")
    for snapshot in snapshots:
        book.add_row(
            str(snapshot.get("side", "—")), _decimal(snapshot.get("best_bid")),
            _decimal(snapshot.get("best_ask")), _decimal(snapshot.get("spread")),
            _decimal(snapshot.get("fair")), _decimal(snapshot.get("fair_edge_ps")),
            f"{float(snapshot.get('time_left_sec') or 0):.0f}s",
        )

    decision = Table(title="Strategy telemetry", expand=True)
    decision.add_column("Signal", style="bold cyan")
    decision.add_column("Value", justify="right")
    for label, key in (("Composite score", "composite_score"), ("Confidence", "confidence"),
                       ("Market consensus", "market_consensus"), ("BTC trend", "btc_trend"),
                       ("Strike proximity", "strike_proximity")):
        decision.add_row(label, _decimal(signal.get(key)))
    decision.add_row("Fair UP / DOWN", f"{_decimal(telemetry.get('fair_up'))} / {_decimal(telemetry.get('fair_down'))}")
    decision.add_row("Account", f"balances={payload.get('account_balance_count', 0)}  orders={payload.get('account_open_order_count', 0)}  fills={payload.get('account_fill_count', 0)}")
    return Group(
        Panel(market, title="Hyperliquid Outcome · Read-only", border_style="green"),
        Panel(book, border_style="blue"),
        Panel(decision, border_style="magenta"),
        Text("Data source: local SQLite journal. Compare BTC mark and YES/NO bid-ask with Outcome UI. No execution path exists in this dashboard.", style="dim"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="View Outcome shadow telemetry from SQLite")
    parser.add_argument("--db", default="logs/outcome_shadow.db", help="Shadow SQLite journal path")
    parser.add_argument("--refresh-sec", type=float, default=1.0, help="Refresh interval (default: 1)")
    parser.add_argument("--once", action="store_true", help="Render one snapshot and exit")
    args = parser.parse_args()
    db_path = Path(args.db).expanduser().resolve()
    console = Console()
    if args.once:
        ts, payload = load_latest_cycle(db_path)
        console.print(render(ts, payload, db_path))
        return
    with Live(console=console, refresh_per_second=max(1, int(1 / max(0.1, args.refresh_sec))), screen=True) as live:
        while True:
            ts, payload = load_latest_cycle(db_path)
            live.update(render(ts, payload, db_path), refresh=True)
            time.sleep(max(0.1, args.refresh_sec))


if __name__ == "__main__":
    main()
