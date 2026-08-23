from decimal import Decimal
import json
import sqlite3

import pytest

from bot.outcome_event_bridge import (
    OutcomeFillEvent,
    OutcomeJournalBridge,
    OutcomeSettlementEvent,
    parse_outcome_coin,
)
from monitoring.trade_journal_db import TradeJournalDB


def test_parse_outcome_coin_and_reject_non_outcome_assets():
    assert parse_outcome_coin("#11450") == (1145, 0)
    assert parse_outcome_coin("#11451") == (1145, 1)
    with pytest.raises(ValueError):
        parse_outcome_coin("BTC")
    with pytest.raises(ValueError):
        parse_outcome_coin("#11452")


def test_normalizes_hyperliquid_outcome_fill():
    fill = OutcomeFillEvent.from_user_fill(
        {
            "coin": "#11450",
            "cloid": "0xabc",
            "oid": 42,
            "tid": 99,
            "side": "B",
            "px": "0.42",
            "sz": "25.0",
            "fee": "0.003",
            "feeToken": "USDC",
            "time": 1787457505476,
            "crossed": False,
        }
    )
    assert fill.outcome_id == 1145
    assert fill.outcome_side == "UP"
    assert fill.side == "BUY"
    assert fill.price == Decimal("0.42")
    assert fill.quantity == Decimal("25.0")
    assert fill.is_maker is True


def test_settlement_is_not_accepted_as_a_trade_fill():
    with pytest.raises(ValueError):
        OutcomeFillEvent.from_user_fill(
            {"coin": "#11450", "dir": "Settlement", "side": "B", "px": "1", "sz": "10", "time": 1, "tid": 1}
        )


def test_records_fill_and_verified_settlement_in_existing_journal(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    bridge = OutcomeJournalBridge(journal, "outcome-run")
    fill = OutcomeFillEvent.from_user_fill(
        {
            "coin": "#11451", "oid": 43, "tid": 100, "side": "A",
            "px": "0.58", "sz": "20", "fee": "0.002", "time": 2, "crossed": True,
        }
    )
    bridge.record_fill(fill, market_key="outcome:1145", extra_payload={"slug": "outcome:1145"})
    bridge.record_settlement(
        OutcomeSettlementEvent(1145, 1, Decimal("77400"), "official_outcome_settlement"),
        market_key="outcome:1145",
    )

    with sqlite3.connect(journal.db_path) as conn:
        order = conn.execute("SELECT side, payload_json FROM order_events").fetchone()
        settlement = conn.execute("SELECT payload_json FROM strategy_events").fetchone()
    assert order[0] == "SELL"
    assert json.loads(order[1])["asset_id"] == 100011451
    assert json.loads(order[1])["liquidity_class"] == "taker"
    assert json.loads(settlement[0])["outcome"] == "DOWN"
    assert json.loads(settlement[0])["settlement_source"] == "official_outcome_settlement"
