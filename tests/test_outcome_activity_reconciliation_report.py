from bot.outcome_activity_reconciliation_report import reconcile_official_activity
from monitoring.trade_journal_db import TradeJournalDB


def test_activity_reconciliation_uses_official_trade_ids_and_surfaces_drift(tmp_path):
    db = TradeJournalDB(tmp_path / "shadow.db")
    assert db.log_outcome_fill_once(
        "run", trade_id="matched", side="BUY", price=0.5, qty=10, instrument_id="#10",
        payload={"venue": "hyperliquid_outcome", "actual_fill": True, "trade_id": "matched"},
    )
    assert db.log_outcome_fill_once(
        "run", trade_id="local-only", side="BUY", price=0.5, qty=10, instrument_id="#10",
        payload={"venue": "hyperliquid_outcome", "actual_fill": True, "trade_id": "local-only"},
    )
    report = reconcile_official_activity(tmp_path / "shadow.db", [
        {"id": "matched", "type": "trade"}, {"id": "venue-only", "type": "trade"},
        {"id": "deposit", "type": "deposit"},
    ])
    assert report.matched_trade_count == 1
    assert report.official_missing_locally == ("venue-only",)
    assert report.local_missing_officially == ("local-only",)
    assert report.status == "DRIFT_DETECTED"
