from decimal import Decimal

from bot.outcome_exit_continuation import OutcomeExitContinuationObserver
from monitoring.trade_journal_db import TradeJournalDB


def test_exit_continuation_records_only_missing_due_checkpoints(tmp_path):
    journal = TradeJournalDB(str(tmp_path / "journal.db"))
    observer = OutcomeExitContinuationObserver(journal, "test")
    observer.register(outcome_id=1, coin="#10", side_index=0, entry_vwap=Decimal("0.6"),
                      inventory=Decimal("10"), exit_order_id="exit", execution_type="ioc", exit_timestamp=100.0)
    due = observer.due(outcome_id=1, now=401.0)
    assert [(target, item.entry_vwap, item.inventory) for item, target in due] == [(300, Decimal("0.6"), Decimal("10"))]
    item, target = due[0]
    assert observer.record(continuation=item, target_sec=target, best_bid=Decimal("0.65"), best_ask=Decimal("0.66"),
                           marketable_vwap=Decimal("0.649"), depth_shares=Decimal("10"))
    assert observer.due(outcome_id=1, now=401.0) == []
    assert [target for _, target in observer.due(outcome_id=1, now=1001.0)] == [900]
