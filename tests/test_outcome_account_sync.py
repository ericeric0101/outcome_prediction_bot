from decimal import Decimal

from bot.outcome_account_sync import OutcomeAccountSynchronizer


class ReadOnlyOutcomeClient:
    """Fixture client: exposes only account reads, never an exchange method."""

    def __init__(self) -> None:
        self.calls = []

    def get_spot_clearinghouse_state_sync(self, user):
        self.calls.append(("balances", user))
        return {
            "balances": [
                {"coin": "USDC", "total": "100", "hold": "0", "entryNtl": "0"},
                {"coin": "+5160", "total": "25", "hold": "5", "entryNtl": "10"},
                {"coin": "+5161", "total": "3", "hold": "0", "entryNtl": "2.4"},
            ]
        }

    def get_open_orders_sync(self, user):
        self.calls.append(("orders", user))
        return [
            {"coin": "#5160", "oid": 12, "cloid": "order-a", "side": "A", "limitPx": "0.44", "sz": "5"},
            {"coin": "BTC", "oid": 99, "side": "B", "limitPx": "1", "sz": "1"},
        ]

    def get_user_fills_sync(self, user):
        self.calls.append(("fills", user))
        return [
            {"coin": "#5160", "oid": 12, "tid": 1, "side": "B", "px": "0.4", "sz": "25", "fee": "0.01", "feeToken": "USDC", "time": 123, "crossed": False},
            {"coin": "#5160", "oid": 13, "tid": 2, "side": "B", "px": "1", "sz": "25", "fee": "0", "time": 124, "dir": "settlement"},
            {"coin": "BTC", "oid": 14, "tid": 3, "side": "B", "px": "1", "sz": "1", "fee": "0", "time": 125},
        ]


def test_account_sync_normalizes_outcome_balances_orders_and_fills_without_settlement_inference():
    client = ReadOnlyOutcomeClient()
    snapshot = OutcomeAccountSynchronizer(client, "0x" + "a" * 40).fetch_snapshot()

    up = snapshot.balance_for(516, 0)
    assert up is not None
    assert up.total_qty == Decimal("25")
    assert up.available_qty == Decimal("20")
    assert up.avg_entry_price == Decimal("0.4")
    assert len(snapshot.open_orders) == 1
    assert snapshot.open_orders[0].side == "SELL"
    assert len(snapshot.fills) == 1
    assert snapshot.fills[0].side == "BUY"
    assert len(snapshot.ignored_settlement_fills) == 1
    assert [name for name, _ in client.calls] == ["balances", "orders", "fills"]
