import sqlite3
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


class PendingReconciler(Reconciler):
    def reconcile_settlement(self, **kwargs):
        self.settlement_calls.append(kwargs["settlement"].market_id)
        return "pending_winning_payout_evidence"


class CursorAccount(Account):
    def __init__(self):
        super().__init__()
        self.by_time_calls = []

    def get_user_fills_by_time_sync(self, wallet, *, start_time_ms, end_time_ms):
        self.by_time_calls.append((wallet, start_time_ms, end_time_ms))
        return [{
            "tid": 99, "time": end_time_ms - 1, "coin": "#11530", "dir": "settlement",
            "px": "1", "sz": "5", "fee": "0",
        }]


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


def test_settlement_worker_persists_payout_cursor_and_logs_pending_only_on_transition(tmp_path):
    journal = TradeJournalDB(tmp_path / "worker.db")
    account, adapter, reconciler = CursorAccount(), Adapter(), PendingReconciler()
    worker = OutcomeSettlementWorker(
        account=account, settlement_adapter=adapter, pnl_reconciler=reconciler,
        journal=journal, interval_sec=30,
    )
    worker.add_candidates([1153])
    worker.run_once(now_monotonic=31)
    worker.run_once(now_monotonic=62)

    assert len(account.by_time_calls) == 2
    assert reconciler.settlement_calls == [1153, 1153]
    assert journal.load_outcome_settlement_fill_cursor("wallet") is not None
    stored = journal.load_outcome_settlement_payout_fills("wallet")
    assert len(stored) == 1
    assert stored[0]["dir"] == "settlement"
    with sqlite3.connect(journal.db_path) as conn:
        assert conn.execute(
            "SELECT status FROM outcome_settlement_status WHERE outcome_id=1153"
        ).fetchone()[0] == "pending_winning_payout_evidence"
