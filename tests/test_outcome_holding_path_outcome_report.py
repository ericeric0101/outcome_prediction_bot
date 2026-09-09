import sqlite3
from decimal import Decimal

from bot.outcome_holding_path import OutcomeHoldingPathObservation, OutcomeHoldingPathRecorder
from bot.outcome_holding_path_outcome_report import report
from monitoring.trade_journal_db import TradeJournalDB


def test_lifecycle_report_keeps_one_entry_path_and_labels_recovery(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    recorder = OutcomeHoldingPathRecorder(journal, "run")
    base = dict(
        outcome_id=1, period="1d", coin="#11", inventory=Decimal("10"), fill_vwap=Decimal("0.8"),
        maker_close_fee_rate=Decimal("0"), time_left_sec=40000, book_health="fresh",
        oi_evidence={"gate_variants": {"spot_mark": {"eligible": True, "side_index": 1}}},
        entry_lifecycle_id="official_buy:entry-order:entry-trade", entry_order_id="entry-order",
        entry_trade_id="entry-trade", entry_filled_at="2026-09-06T01:00:00+00:00", entry_side_index=1,
        entry_filled_at_source="official_fill_timestamp_ms",
        entry_tier="tier_b_spot_mark", entry_target_return_pct="0.03", entry_time_left_sec=50000,
    )
    recorder.record(OutcomeHoldingPathObservation(best_bid=Decimal("0.75"), best_ask=Decimal("0.76"), holding_age_sec=100, **base))
    recorder.record(OutcomeHoldingPathObservation(best_bid=Decimal("0.70"), best_ask=Decimal("0.71"), holding_age_sec=7300, **base))
    recorder.record(OutcomeHoldingPathObservation(best_bid=Decimal("0.84"), best_ask=Decimal("0.85"), holding_age_sec=8000, **base))
    # An unbound legacy point must not be combined with this lifecycle.
    recorder.record(OutcomeHoldingPathObservation(
        1, "1d", "#11", Decimal("10"), Decimal("0.8"), Decimal("0.1"), Decimal("0.2"),
        Decimal("0"), 9000, 30000, "fresh", {},
    ))
    with sqlite3.connect(journal.db_path) as conn:
        observation_ids = [row[0] for row in conn.execute(
            "SELECT id FROM strategy_events WHERE event_type='OUTCOME_HOLDING_PATH_OBSERVATION' ORDER BY id"
        )]
        for event_id, ts in zip(observation_ids[:3], (
            "2026-09-06T01:01:40+00:00", "2026-09-06T03:01:40+00:00", "2026-09-06T03:13:20+00:00",
        )):
            conn.execute("UPDATE strategy_events SET ts=? WHERE id=?", (ts, event_id))
        conn.execute(
            """INSERT INTO outcome_realized_pnl_lots
               (close_trade_id, open_trade_id, outcome_id, side_index, close_kind, quantity, cost_usdc, proceeds_usdc, realized_net_usdc, source_json, recorded_at)
               VALUES ('close', 'entry-trade', 1, 1, 'sell', '10', '8', '8.4', '0.4', '{}', '2026-09-06T03:30:00+00:00')"""
        )
        conn.commit()
    payload = report(journal.db_path)
    assert payload["lifecycle_count"] == 1
    assert payload["legacy_or_ambiguous_observations_excluded"] == 1
    row = payload["lifecycles"][0]
    assert row["entry_day_type"] == "weekend"
    assert row["holding_age_basis"] == "official_fill_timestamp_ms"
    assert row["same_side_reconfirmed_after_two_hours"] is True
    assert row["thresholds"]["5%"]["breached_after_two_hours"] is True
    assert row["thresholds"]["10%"]["recovered_to_cost_after_breach"] is True
    assert row["thresholds"]["10%"]["reached_target_after_breach"] is True
    assert row["final_status"] == "closed_target_or_better"
