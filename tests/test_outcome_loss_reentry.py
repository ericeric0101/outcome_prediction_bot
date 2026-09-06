import sqlite3
from datetime import datetime, timedelta, timezone

from bot.outcome_loss_reentry import OutcomeLossReentryGate
from monitoring.trade_journal_db import TradeJournalDB


def _record_loss(journal, gate):
    journal.log_order_event("run", "ORDER_FILLED", venue_order_id="sell", side="SELL", price=0.80, status="FILLED", instrument_id="#10", payload={
        "venue": "hyperliquid_outcome", "actual_fill": True, "fill_provenance": "hyperliquid_userFills",
    })
    assert gate.record_confirmed_loss_exit(outcome_id=1, period="1d", coin="#10", order_id="sell") is True


def test_reentry_gate_requires_official_sell_fill_then_allows_one_reclaimed_entry_after_cooldown(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    gate = OutcomeLossReentryGate(journal, "run")
    assert gate.evaluate(outcome_id=1, coin="#10", candidate_bid=0.8).allowed is True
    assert gate.record_confirmed_loss_exit(outcome_id=1, period="1d", coin="#10", order_id="sell") is False
    _record_loss(journal, gate)
    now = datetime.now(timezone.utc)
    cooling = gate.evaluate(outcome_id=1, coin="#10", candidate_bid=0.9, now=now)
    assert cooling.allowed is False
    assert cooling.reason == "loss_reentry_cooldown_active"
    assert cooling.cooldown_remaining_sec is not None
    after_cooldown = now + timedelta(seconds=gate.COOLDOWN_SEC + 1)
    not_reclaimed = gate.evaluate(outcome_id=1, coin="#10", candidate_bid=0.799, now=after_cooldown)
    assert not_reclaimed.allowed is False
    assert not_reclaimed.reason == "loss_reentry_same_side_exit_price_not_reclaimed"
    allowed = gate.evaluate(outcome_id=1, coin="#10", candidate_bid=0.80, now=after_cooldown)
    assert allowed.allowed is True
    assert allowed.is_limited_reentry is True
    assert allowed.prior_exit_price == 0.80
    assert gate.record_reentry_submitted(
        outcome_id=1, period="1d", coin="#10", order_id="new-buy", bid=0.80,
        target_price=0.82, entry_reason="up_spot_mark_oi_confirmed",
    ) is True
    used = gate.evaluate(outcome_id=1, coin="#10", candidate_bid=0.90, now=after_cooldown)
    assert used.allowed is False
    assert used.reason == "loss_reentry_already_used_until_market_rollover"
    with sqlite3.connect(journal.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategy_events WHERE event_type='OUTCOME_LOSS_EXIT_CONFIRMED'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM strategy_events WHERE event_type='OUTCOME_LOSS_REENTRY_SUBMITTED'").fetchone()[0] == 1


def test_reentry_can_take_new_confirmed_opposite_side_after_cooldown(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    gate = OutcomeLossReentryGate(journal, "run")
    _record_loss(journal, gate)
    after_cooldown = datetime.now(timezone.utc) + timedelta(seconds=gate.COOLDOWN_SEC + 1)
    decision = gate.evaluate(outcome_id=1, coin="#11", candidate_bid=0.60, now=after_cooldown)
    assert decision.allowed is True
    assert decision.reason == "loss_reentry_limited_recovery_authorized"


def test_legacy_loss_event_recovers_immutable_official_exit_price(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    gate = OutcomeLossReentryGate(journal, "run")
    journal.log_order_event("run", "ORDER_FILLED", venue_order_id="old-sell", side="SELL", price=0.80, status="FILLED", instrument_id="#10", payload={
        "venue": "hyperliquid_outcome", "actual_fill": True, "fill_provenance": "hyperliquid_userFills",
    })
    old = (datetime.now(timezone.utc) - timedelta(seconds=gate.COOLDOWN_SEC + 1)).isoformat()
    journal.log_strategy_event("run", gate.EVENT, {
        "outcome_id": 1, "coin": "#10", "order_id": "old-sell", "period": "1d",
        "official_fill_ts": old, "reentry_policy": "block_same_market_until_rollover",
    })
    # Event time is the durable clock, so set the inserted event to the same
    # historical timestamp without altering its immutable fill evidence.
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute("UPDATE strategy_events SET ts=? WHERE event_type=?", (old, gate.EVENT))
    decision = gate.evaluate(outcome_id=1, coin="#10", candidate_bid=0.80, now=datetime.now(timezone.utc))
    assert decision.allowed is True
    assert decision.prior_exit_price == 0.80
