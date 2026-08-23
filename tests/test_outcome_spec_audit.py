import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bot.lifecycle.outcome_lifecycle import parse_outcome_market_spec
from bot.outcome_spec_audit import OutcomeSpecAudit
from monitoring.trade_journal_db import TradeJournalDB


def _raw_market():
    expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
    return {
        "outcome": 1145,
        "description": (
            "class:priceBinary|underlying:BTC|expiry:"
            f"{expiry.strftime('%Y%m%d-%H%M')}|targetPrice:77431|period:15m"
        ),
        "quoteToken": "USDC",
        "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
    }


def test_spec_audit_preserves_raw_spec_and_never_infers_resolution(tmp_path):
    raw = _raw_market()
    market = parse_outcome_market_spec(raw)
    assert market is not None
    db = tmp_path / "audit.db"
    audit = OutcomeSpecAudit(TradeJournalDB(db), "audit-run")

    audit.observe(market, raw)
    audit.observe(market, raw)
    audit.mark_pending_resolution(market, market.expiry_timestamp - 1)
    audit.mark_pending_resolution(market, market.expiry_timestamp)

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT event_type, payload_json FROM strategy_events ORDER BY id").fetchall()
    assert [row[0] for row in rows] == ["OUTCOME_MARKET_SPEC_OBSERVED", "OUTCOME_RESOLUTION_PENDING"]
    observed = json.loads(rows[0][1])
    pending = json.loads(rows[1][1])
    assert observed["raw_outcome_meta"] == raw
    assert observed["side_names"] == ["Yes", "No"]
    assert pending["winning_side"] is None
    assert pending["settlement_fee"] is None


def test_spec_audit_requires_explicit_official_resolution_source(tmp_path):
    audit = OutcomeSpecAudit(TradeJournalDB(tmp_path / "audit.db"), "audit-run")
    with pytest.raises(ValueError, match="official"):
        audit.record_official_resolution(
            outcome_id=1145, winning_side_index=0, payout_per_share=Decimal("1"),
            settlement_fee_per_share=None, source="derived_from_price", raw={},
        )
    audit.record_official_resolution(
        outcome_id=1145, winning_side_index=1, payout_per_share=Decimal("1"),
        settlement_fee_per_share=Decimal("0"), source="official_outcome_api", raw={"status": "resolved"},
    )
