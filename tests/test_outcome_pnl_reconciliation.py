from decimal import Decimal
import sqlite3

from bot.outcome_pnl_reconciliation import OutcomePnLReconciler
from bot.outcome_settlement import OutcomeSettlement
from monitoring.trade_journal_db import TradeJournalDB


def _fill(journal, *, trade_id, side, price, qty, fee="0", outcome_id=516, side_index=0):
    journal.log_order_event(
        "run", "ORDER_FILLED", side=side, price=float(price), qty=float(qty), commission_usdc=float(fee),
        payload={
            "venue": "hyperliquid_outcome", "actual_fill": True, "fill_provenance": "hyperliquid_userFills",
            "outcome_id": outcome_id, "side_index": side_index, "trade_id": trade_id,
        },
    )


def test_fifo_sell_and_official_settlement_are_canonical_and_idempotent(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    _fill(journal, trade_id="buy-1", side="BUY", price="0.60", qty="10", fee="0.10")
    _fill(journal, trade_id="sell-1", side="SELL", price="0.70", qty="5", fee="0.05")
    _fill(journal, trade_id="buy-2", side="BUY", price="0.50", qty="3")
    reconciler = OutcomePnLReconciler(journal, "pnl-run")

    assert reconciler.reconcile_sells() == 1
    # Same source fills can be replayed safely after a process restart.
    assert reconciler.reconcile_sells() == 0
    with sqlite3.connect(journal.db_path) as conn:
        sell = conn.execute(
            "SELECT cost_usdc,proceeds_usdc,realized_net_usdc FROM outcome_realized_pnl_lots WHERE close_kind='sell'"
        ).fetchone()
    assert sell == ("3.05", "3.45", "0.40")

    settlement = OutcomeSettlement(516, True, Decimal("1"), "official", {"settleFraction": "1"})
    status = reconciler.reconcile_settlement(
        settlement=settlement,
        raw_fills=[{"coin": "#5160", "dir": "settlement", "px": "1", "sz": "8", "fee": "0"}],
        clearinghouse={"balances": []},
    )
    assert status == "recorded"
    assert reconciler.reconcile_settlement(
        settlement=settlement,
        raw_fills=[{"coin": "#5160", "dir": "settlement", "px": "1", "sz": "8", "fee": "0"}],
        clearinghouse={"balances": []},
    ) == "already_recorded"
    with sqlite3.connect(journal.db_path) as conn:
        settlement_rows = conn.execute(
            "SELECT COUNT(*) FROM strategy_events WHERE event_type='MARKET_SETTLEMENT'"
        ).fetchone()[0]
        remaining = conn.execute(
            "SELECT COUNT(*) FROM outcome_realized_pnl_lots WHERE close_kind='settlement'"
        ).fetchone()[0]
    assert settlement_rows == 1
    assert remaining == 2


def test_winning_settlement_without_official_payout_stays_pending(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    _fill(journal, trade_id="buy-1", side="BUY", price="0.60", qty="10")
    reconciler = OutcomePnLReconciler(journal, "pnl-run")
    status = reconciler.reconcile_settlement(
        settlement=OutcomeSettlement(516, True, Decimal("1"), "official", {"settleFraction": "1"}),
        raw_fills=[], clearinghouse={"balances": []},
    )
    assert status == "pending_winning_payout_evidence"


def test_losing_settlement_requires_an_explicit_account_balance_payload(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    _fill(journal, trade_id="buy-1", side="BUY", price="0.60", qty="10")
    reconciler = OutcomePnLReconciler(journal, "pnl-run")
    status = reconciler.reconcile_settlement(
        settlement=OutcomeSettlement(516, True, Decimal("0"), "official", {"settleFraction": "0"}),
        raw_fills=[], clearinghouse={},
    )
    assert status == "pending_losing_zero_balance_evidence"
