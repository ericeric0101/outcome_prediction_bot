from decimal import Decimal

from bot.outcome_settlement import OutcomeSettlement
from bot.outcome_settlement_worker import OutcomeSettlementWorker
from monitoring.trade_journal_db import TradeJournalDB


class Account:
    wallet_address = "wallet"
    def __init__(self): self.calls = []
    def get_user_fills_sync(self, wallet): self.calls.append(("fills", wallet)); return []
    def get_spot_clearinghouse_state_sync(self, wallet): self.calls.append(("balance", wallet)); return {"balances": []}


class Adapter:
    def __init__(self): self.calls = []
    def fetch_outcome_id(self, outcome_id):
        self.calls.append(outcome_id)
        return OutcomeSettlement(outcome_id, True, Decimal("1"), "official", {})


class Reconciler:
    def __init__(self): self.sell_calls = 0; self.settlement_calls = []
    def reconcile_sells(self): self.sell_calls += 1; return 0
    def unresolved_outcome_ids(self): return set()
    def reconcile_settlement(self, **kwargs):
        self.settlement_calls.append(kwargs["settlement"].market_id)
        return "recorded"


def test_settlement_worker_runs_pnl_and_official_settlement_outside_launcher_lane(tmp_path):
    journal = TradeJournalDB(tmp_path / "worker.db")
    account, adapter, reconciler = Account(), Adapter(), Reconciler()
    worker = OutcomeSettlementWorker(
        account=account, settlement_adapter=adapter, pnl_reconciler=reconciler,
        journal=journal, interval_sec=30,
    )
    worker.add_candidates([1153])
    worker.run_once(now_monotonic=31)
    assert reconciler.sell_calls == 1
    assert adapter.calls == [1153]
    assert reconciler.settlement_calls == [1153]
    assert account.calls == [("fills", "wallet"), ("balance", "wallet")]
    # A recorded market is not re-polled on subsequent scheduler checks.
    worker.run_once(now_monotonic=62)
    assert adapter.calls == [1153]
