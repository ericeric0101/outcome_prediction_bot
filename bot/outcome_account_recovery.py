"""Account-truth recovery for Outcome execution after restart or transport loss."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Protocol

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec


class AccountSnapshotReader(Protocol):
    def get_spot_clearinghouse_state_sync(self, user: str) -> dict[str, Any]: ...
    def get_open_orders_sync(self, user: str) -> list[dict[str, Any]]: ...


class StrategyJournal(Protocol):
    def log_strategy_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class RecoveryFinding:
    market_id: int
    coin: str
    inventory: Decimal
    buy_order_ids: tuple[str, ...]
    sell_order_ids: tuple[str, ...]
    state: str


@dataclass(frozen=True)
class RecoveryReport:
    safe_for_new_entry: bool
    findings: tuple[RecoveryFinding, ...]
    reason: str


class OutcomeAccountRecovery:
    """Rebuild managed state from live balances and owned open orders only."""

    def __init__(self, *, account: AccountSnapshotReader, wallet: str, journal: StrategyJournal | None = None, run_id: str = "outcome-recovery") -> None:
        self.account, self.wallet, self.journal, self.run_id = account, wallet, journal, run_id

    @staticmethod
    def _balances(snapshot: dict[str, Any]) -> dict[str, Decimal]:
        return {str(row.get("coin")): Decimal(str(row.get("total", "0"))) for row in snapshot.get("balances", [])}

    def reconcile(self, markets: Iterable[OutcomeMarketSpec]) -> RecoveryReport:
        markets = tuple(markets)
        balances = self._balances(self.account.get_spot_clearinghouse_state_sync(self.wallet))
        orders = self.account.get_open_orders_sync(self.wallet)
        known_coins = {coin for market in markets for coin in (market.yes_coin, market.no_coin)}
        findings: list[RecoveryFinding] = []
        unsafe: list[str] = []

        # Relevant positions/orders outside the current selection cannot be
        # safely attributed to this run; do not let a fresh signal overlap it.
        unknown_coins = {coin for coin, total in balances.items() if coin.startswith("#") and total > 0 and coin not in known_coins}
        unknown_coins.update(str(order.get("coin")) for order in orders if str(order.get("coin", "")).startswith("#") and order.get("coin") not in known_coins)
        if unknown_coins:
            unsafe.append(f"unmanaged Outcome exposure: {', '.join(sorted(unknown_coins))}")

        for market in markets:
            for coin in (market.yes_coin, market.no_coin):
                inventory = balances.get(coin, Decimal("0"))
                coin_orders = [order for order in orders if order.get("coin") == coin]
                buys = tuple(str(order.get("oid")) for order in coin_orders if order.get("side") == "B")
                sells = tuple(str(order.get("oid")) for order in coin_orders if order.get("side") == "A")
                covering = any(Decimal(str(order.get("sz", "0"))) >= inventory for order in coin_orders if order.get("side") == "A")
                if inventory > 0 and covering:
                    state = "protected_inventory"
                elif inventory > 0:
                    state = "unprotected_inventory"
                    unsafe.append(f"{coin} inventory has no covering sell")
                elif buys and sells:
                    state = "conflicting_orders"
                    unsafe.append(f"{coin} has both buy and sell resting without inventory")
                elif buys:
                    state = "buy_resting"
                elif sells:
                    state = "orphan_sell"
                    unsafe.append(f"{coin} sell order has no inventory")
                else:
                    state = "flat"
                findings.append(RecoveryFinding(market.outcome_id, coin, inventory, buys, sells, state))

        report = RecoveryReport(not unsafe, tuple(findings), "; ".join(unsafe) or "account state reconciled")
        if self.journal:
            self.journal.log_strategy_event(self.run_id, "OUTCOME_ACCOUNT_RECONCILED", {
                "safe_for_new_entry": report.safe_for_new_entry, "reason": report.reason,
                "findings": [{"market_id": item.market_id, "coin": item.coin, "inventory": str(item.inventory), "buy_order_ids": item.buy_order_ids, "sell_order_ids": item.sell_order_ids, "state": item.state} for item in report.findings],
            })
        return report
