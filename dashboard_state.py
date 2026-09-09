from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Tuple


@dataclass
class TradeRecord:
    trade_id: int
    market_slug: str
    side: str
    entry_price: float
    qty: float
    exit_price: Optional[float]
    redeem_amount: Optional[float]
    is_settled: bool
    expected_redeem_amount: Optional[float] = None
    realized_pnl: Optional[float] = None


@dataclass
class DashboardState:
    strike_price: float
    spot_price: float
    position_side: Optional[str]
    position_entry: Optional[float]
    position_qty: Optional[float]
    position_ask: Optional[float]
    current_market_price: float
    trades: List[TradeRecord]
    cumulative_pnl: float
    usdc_balance: float
    pol_balance: float
    account_last_updated: datetime
    visible_trades_pnl: float = 0.0
    market_slug: Optional[str] = None
    position_hold_sec: Optional[float] = None
    market_phase: str = "—"
    active_side: Optional[str] = None
    time_left_sec: Optional[float] = None
    decision_updated_at: Optional[datetime] = None
    side_score: Optional[float] = None
    p_fair: Optional[float] = None
    book_bid: Optional[float] = None
    book_ask: Optional[float] = None
    book_mid: Optional[float] = None
    robust_net_usdc: Optional[float] = None
    last_block_reason: Optional[str] = None
    pending_redeem_count: int = 0
    pending_redeem_usdc: float = 0.0
    open_exposure_usdc: float = 0.0
    recent_errors: List[Tuple[datetime, str]] = field(default_factory=list)
    last_heartbeat: datetime = field(default_factory=datetime.utcnow)
    consecutive_losses: int = 0
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _callbacks: List[Callable[[], None]] = field(default_factory=list, init=False, repr=False)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if not hasattr(self, key):
                    raise AttributeError(f"DashboardState has no field '{key}'")
                setattr(self, key, value)
            self.updated_at = datetime.now(timezone.utc)
            callbacks = list(self._callbacks)
        self._notify(callbacks)

    def snapshot(self) -> "DashboardState":
        with self._lock:
            return DashboardState(
                strike_price=self.strike_price,
                spot_price=self.spot_price,
                position_side=self.position_side,
                position_entry=self.position_entry,
                position_qty=self.position_qty,
                position_ask=self.position_ask,
                current_market_price=self.current_market_price,
                trades=list(self.trades),
                cumulative_pnl=self.cumulative_pnl,
                usdc_balance=self.usdc_balance,
                pol_balance=self.pol_balance,
                account_last_updated=self.account_last_updated,
                visible_trades_pnl=self.visible_trades_pnl,
                market_slug=self.market_slug,
                position_hold_sec=self.position_hold_sec,
                market_phase=self.market_phase,
                active_side=self.active_side,
                time_left_sec=self.time_left_sec,
                decision_updated_at=self.decision_updated_at,
                side_score=self.side_score,
                p_fair=self.p_fair,
                book_bid=self.book_bid,
                book_ask=self.book_ask,
                book_mid=self.book_mid,
                robust_net_usdc=self.robust_net_usdc,
                last_block_reason=self.last_block_reason,
                pending_redeem_count=self.pending_redeem_count,
                pending_redeem_usdc=self.pending_redeem_usdc,
                open_exposure_usdc=self.open_exposure_usdc,
                recent_errors=list(self.recent_errors),
                last_heartbeat=self.last_heartbeat,
                consecutive_losses=self.consecutive_losses,
                updated_at=self.updated_at,
            )

    def upsert_redeem(self, market_slug: str, redeem_amount: float) -> bool:
        with self._lock:
            for trade in self.trades:
                if trade.market_slug == market_slug:
                    trade.redeem_amount = float(redeem_amount)
                    trade.is_settled = True
                    self.updated_at = datetime.now(timezone.utc)
                    callbacks = list(self._callbacks)
                    break
            else:
                return False
        self._notify(callbacks)
        return True

    def add_listener(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    @staticmethod
    def _notify(callbacks: List[Callable[[], None]]) -> None:
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass
