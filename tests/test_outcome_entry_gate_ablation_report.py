import json

from bot.outcome_entry_gate_ablation_report import as_dict
from monitoring.trade_journal_db import TradeJournalDB


def _admission(*, variants, submitted=False, order_id=None):
    return {
        "period": "1d", "raw_signal_evidence": {"gate_variants": variants},
        "final_reason": "placed first-level ALO buy" if submitted else "live strategy no entry",
        "execution_submitted": submitted, "order_id": order_id,
    }


def test_report_compares_counterfactual_eligibility_without_claiming_counterfactual_pnl(tmp_path):
    journal = TradeJournalDB(tmp_path / "ablation.db")
    base = {"spot_mark_oi": {"eligible": True, "side_index": 0},
            "spot_mark": {"eligible": True, "side_index": 0},
            "spot_mark_or_oi": {"eligible": True, "side_index": 0}}
    added = {"spot_mark_oi": {"eligible": False, "side_index": None},
             "spot_mark": {"eligible": True, "side_index": 1},
             "spot_mark_or_oi": {"eligible": True, "side_index": 1}}
    journal.log_strategy_event("run", "OUTCOME_ENTRY_ADMISSION_DECISION", _admission(variants=base, submitted=True, order_id="buy-1"))
    journal.log_strategy_event("run", "OUTCOME_ENTRY_ADMISSION_DECISION", _admission(variants=added))
    journal.log_order_event("run", "ORDER_FILLED", venue_order_id="buy-1", side="BUY", status="FILLED")

    report = as_dict(journal.db_path)

    assert report["admission_observations_with_variants"] == 2
    assert report["variants"]["spot_mark_oi"]["eligible_observations"] == 1
    assert report["variants"]["spot_mark"]["eligible_observations"] == 2
    assert report["variants"]["spot_mark"]["added_vs_baseline"] == 1
    assert report["live_baseline"]["official_buy_fills"] == 1
    assert report["ready_for_live_gate_change"] is False
    assert "markout" in json.dumps(report["counterfactual_limits"])
