import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


class TerminalDashboard:
    """Minimal Rich terminal dashboard for live trading session stats."""

    def __init__(
        self,
        title: str = "BTC 15M Bot",
        refresh_interval_sec: float = 1.0,
    ) -> None:
        self.title = title
        self.refresh_interval_sec = max(0.2, float(refresh_interval_sec))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: Dict[str, Any] = {
            "started_at": datetime.now(timezone.utc),
            "phase": "WAITING",
            "slug": "-",
            "active_side": "NONE",
            "inventory_shares": 0.0,
            "wallet_balance_usdc": None,
            "fills_total": 0,
            "maker_fills": 0,
            "taker_fills": 0,
            "maker_buy_fills": 0,
            "maker_sell_fills": 0,
            "taker_exit_fills": 0,
            "fees_paid_usdc": 0.0,
            "cycle_total": 0,
            "cycle_wins": 0,
            "cycle_pnl_usdc": 0.0,
            "round_trips_closed": 0,
            "position_win_rate": 0.0,
            "position_realized_pnl": 0.0,
            "active_orders": 0,
            "last_fill": "-",
            "last_cycle": "-",
            "last_update": datetime.now(timezone.utc),
            "current_buy_order": None,
            "current_sell_order": None,
            "redeem_runs": 0,
            "recent_orders": deque(maxlen=8),
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="terminal-dashboard")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._state.update(kwargs)
            self._state["last_update"] = datetime.now(timezone.utc)

    def increment_redeem(self) -> None:
        with self._lock:
            self._state["redeem_runs"] += 1
            self._append_recent_order("REDEEM completed")
            self._state["last_update"] = datetime.now(timezone.utc)

    def _append_recent_order(self, text: str) -> None:
        recent: Deque[str] = self._state.setdefault("recent_orders", deque(maxlen=8))
        recent.appendleft(text)

    def record_order_submitted(
        self,
        *,
        side: str,
        token_side: str,
        qty: float,
        price: float,
        client_order_id: str,
        is_taker: bool = False,
    ) -> None:
        with self._lock:
            venue = "TAKER" if is_taker else "MAKER"
            self._append_recent_order(
                f"{venue} SUBMIT {side.upper()} {token_side} TOKEN {qty:.3f} @ {price:.4f} [{client_order_id}]"
            )
            self._state["last_update"] = datetime.now(timezone.utc)

    def record_order_canceled(self, *, client_order_id: str) -> None:
        with self._lock:
            self._append_recent_order(f"CANCELED [{client_order_id}]")
            self._state["last_update"] = datetime.now(timezone.utc)

    def increment_fill(
        self,
        *,
        is_maker_fill: bool,
        side: str,
        token_side: str,
        qty: float,
        price: float,
        commission_usdc: float,
        client_order_id: str,
        is_taker_exit: bool,
    ) -> None:
        with self._lock:
            self._state["fills_total"] += 1
            self._state["fees_paid_usdc"] += float(commission_usdc or 0.0)
            if is_maker_fill:
                self._state["maker_fills"] += 1
                if side == "buy":
                    self._state["maker_buy_fills"] += 1
                elif side == "sell":
                    self._state["maker_sell_fills"] += 1
            else:
                self._state["taker_fills"] += 1
                if is_taker_exit:
                    self._state["taker_exit_fills"] += 1
            self._state["last_fill"] = (
                f"{client_order_id} {side.upper()} {token_side} TOKEN {qty:.3f} @ {price:.4f} "
                f"{'MAKER' if is_maker_fill else 'TAKER'}"
            )
            self._append_recent_order(
                f"{'TAKER' if is_taker_exit else ('MAKER' if is_maker_fill else 'TAKER')} "
                f"FILL {side.upper()} {token_side} TOKEN {qty:.3f} @ {price:.4f} [{client_order_id}]"
            )
            self._state["last_update"] = datetime.now(timezone.utc)

    def record_cycle(self, *, slug: str, pnl_usdc: float) -> None:
        with self._lock:
            self._state["cycle_total"] += 1
            if pnl_usdc > 0:
                self._state["cycle_wins"] += 1
            self._state["cycle_pnl_usdc"] += float(pnl_usdc)
            self._state["last_cycle"] = f"{slug} pnl={pnl_usdc:+.4f}"
            self._state["last_update"] = datetime.now(timezone.utc)

    def record_position_closed(self, *, realized_pnl: float, total_trades: int, win_rate: float) -> None:
        with self._lock:
            self._state["round_trips_closed"] = int(total_trades)
            self._state["position_win_rate"] = float(win_rate)
            self._state["position_realized_pnl"] = float(self._state["position_realized_pnl"]) + float(realized_pnl)
            self._state["last_update"] = datetime.now(timezone.utc)

    def _build_layout(self) -> Group:
        with self._lock:
            snapshot = dict(self._state)

        # Left panel: Orders and Slug
        left_grid = Table.grid(expand=True, padding=(0, 0))
        left_grid.add_column()
        
        buy_text = snapshot.get("current_buy_order") or "No active buy order"
        sell_text = snapshot.get("current_sell_order") or "No active sell order"
        slug = str(snapshot["slug"])
        recent_orders = list(snapshot.get("recent_orders") or [])
        recent_orders_text = "\n".join(recent_orders) if recent_orders else "No recent orders"
        
        active_orders_grid = Table.grid(expand=True, padding=(0, 0))
        active_orders_grid.add_column()
        active_orders_grid.add_row(f"[cyan]Buy[/cyan]: {buy_text}")
        active_orders_grid.add_row(f"[magenta]Sell[/magenta]: {sell_text}")
        left_grid.add_row(Panel(active_orders_grid, title="Active Orders", border_style="cyan"))
        left_grid.add_row(Panel(recent_orders_text, title="Recent Orders", border_style="yellow"))
        strike_val = snapshot.get("strike")
        spot_val = snapshot.get("spot")
        strike_str = f"${strike_val:,.2f}" if strike_val else "..."
        spot_str = f"${spot_val:,.2f}" if spot_val else "..."
        spot_minus_strike = snapshot.get("spot_minus_strike")
        spot_minus_strike_str = "..."
        if spot_minus_strike is not None:
            delta = float(spot_minus_strike)
            if delta > 0:
                direction = "UP"
                color = "green"
            elif delta < 0:
                direction = "DOWN"
                color = "red"
            else:
                direction = "FLAT"
                color = "yellow"
            spot_minus_strike_str = f"[{color}]{delta:+,.2f} ({direction})[/{color}]"
        
        time_left_str = snapshot.get("time_left_str") or "..."
        signal_str = snapshot.get("signal_str") or "NONE"
        yes_ba = snapshot.get("yes_bid_ask") or "-/-"
        no_ba = snapshot.get("no_bid_ask") or "-/-"
        pos_desc = snapshot.get("position_desc") or "None"

        market_text = (
            f"[bold cyan]{slug}[/bold cyan] | Phase: [bold yellow]{snapshot.get('phase', 'WAITING')}[/bold yellow]\n"
            f"Strike: [bold]{strike_str}[/bold] | Spot Mark: [bold]{spot_str}[/bold]\n"
            f"Delta: {spot_minus_strike_str} | Time Left: [bold]{time_left_str}[/bold]\n"
            f"Signal: [bold]{signal_str}[/bold] | Position: [bold green]{pos_desc}[/bold green]\n"
            f"YES ({snapshot.get('yes_coin', 'YES')}): {yes_ba} | NO ({snapshot.get('no_coin', 'NO')}): {no_ba}"
        )
        
        left_grid.add_row(Panel(market_text, title="Market & Signals", border_style="blue"))

        # Right panel: Stats
        stats_table = Table(show_header=False, box=None, pad_edge=False)
        stats_table.add_column("k", style="bold green", width=16)
        stats_table.add_column("v", style="white")
        stats_table.add_row("Inventory", f"{float(snapshot.get('inventory_shares', 0.0)):.2f} shares")
        stats_table.add_row("Total Trades", str(snapshot.get("fills_total", 0)))
        stats_table.add_row("Buys", str(snapshot.get("maker_buy_fills", 0)))
        stats_table.add_row("Sells", str(int(snapshot.get("maker_sell_fills", 0)) + int(snapshot.get("taker_exit_fills", 0))))
        stats_table.add_row("Redeems", str(snapshot.get("redeem_runs", 0)))
        stats_table.add_row("Live PnL", f"{float(snapshot.get('cycle_pnl_usdc', 0.0)):+.4f} USDC")
        wallet_balance = snapshot.get("wallet_balance_usdc")
        stats_table.add_row("Wallet", "..." if wallet_balance is None else f"{float(wallet_balance):.2f} USDC")

        # Split layout
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(
            Panel(left_grid, title="Orders & Feeds", border_style="cyan"),
            Panel(stats_table, title="Stats & PnL", border_style="green")
        )

        return Panel(grid, title=f"{self.title} Live", border_style="yellow")

    def _run(self) -> None:
        with Live(
            self._build_layout(),
            refresh_per_second=max(1, int(round(1.0 / self.refresh_interval_sec))),
            transient=False,
            auto_refresh=False,
            screen=True,
        ) as live:
            while not self._stop_event.wait(self.refresh_interval_sec):
                live.update(self._build_layout(), refresh=True)
