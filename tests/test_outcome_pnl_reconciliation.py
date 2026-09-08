from decimal import Decimal
import sqlite3

from bot.outcome_pnl_reconciliation import OutcomePnLReconciler
from bot.outcome_settlement import OutcomeSettlement
from monitoring.trade_journal_db import TradeJournalDB


def _fill(journal, *, trade_id, side, price, qty, fee="0", outcome_id=516, side_index=0,
          timestamp_ms=None):
    payload = {
        "venue": "hyperliquid_outcome", "actual_fill": True,
        "fill_provenance": "hyperliquid_userFills", "outcome_id": outcome_id,
        "side_index": side_index, "trade_id": trade_id,
    }
    if timestamp_ms is not None:
        payload["timestamp_ms"] = timestamp_ms
    journal.log_order_event(
        "run", "ORDER_FILLED", side=side, price=float(price), qty=float(qty), commission_usdc=float(fee),
        payload=payload,
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


def test_reverse_ingested_user_fills_use_official_time_and_do_not_create_false_inventory(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    # Persist a newest-first API batch.  Exchange timestamps still describe
    # four complete BUY->SELL round trips.
    facts = [
        ("sell-4", "SELL", "0.74", "4", 800),
        ("buy-4", "BUY", "0.70", "4", 700),
        ("sell-3", "SELL", "0.64", "3", 600),
        ("buy-3", "BUY", "0.60", "3", 500),
        ("sell-2", "SELL", "0.54", "2", 400),
        ("buy-2", "BUY", "0.50", "2", 300),
        ("sell-1", "SELL", "0.44", "1", 200),
        ("buy-1", "BUY", "0.40", "1", 100),
    ]
    for trade_id, side, price, qty, timestamp_ms in facts:
        _fill(
            journal, trade_id=trade_id, side=side, price=price, qty=qty,
            timestamp_ms=timestamp_ms,
        )
    reconciler = OutcomePnLReconciler(journal, "pnl-run")

    assert reconciler.unresolved_outcome_ids() == set()
    assert reconciler.reconcile_sells() == 4
    with sqlite3.connect(journal.db_path) as conn:
        pairs = conn.execute(
            """SELECT close_trade_id,open_trade_id,quantity
               FROM outcome_realized_pnl_lots ORDER BY close_trade_id"""
        ).fetchall()
    assert pairs == [
        ("sell-1", "buy-1", "1.0"),
        ("sell-2", "buy-2", "2.0"),
        ("sell-3", "buy-3", "3.0"),
        ("sell-4", "buy-4", "4.0"),
    ]


def test_reconciliation_replaces_stale_derived_sell_allocations(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    _fill(journal, trade_id="buy-1", side="BUY", price="0.40", qty="5", timestamp_ms=100)
    _fill(journal, trade_id="sell-1", side="SELL", price="0.50", qty="5", timestamp_ms=200)
    _fill(journal, trade_id="buy-2", side="BUY", price="0.45", qty="1", timestamp_ms=300)
    journal.record_outcome_realized_pnl_lot(
        close_trade_id="sell-1", open_trade_id="buy-2", outcome_id=516, side_index=0,
        close_kind="sell", quantity=Decimal("2"), cost_usdc=Decimal("0.8"),
        proceeds_usdc=Decimal("0.9"), source={"fifo_ordering": "legacy_local_event_id"},
    )

    reconciler = OutcomePnLReconciler(journal, "pnl-run")
    assert reconciler.reconcile_sells() == 2
    with sqlite3.connect(journal.db_path) as conn:
        rows = conn.execute(
            """SELECT close_trade_id,open_trade_id,quantity,cost_usdc,proceeds_usdc
               FROM outcome_realized_pnl_lots WHERE close_kind='sell'"""
        ).fetchall()
    assert rows == [("sell-1", "buy-1", "5.0", "2.00", "2.50")]
