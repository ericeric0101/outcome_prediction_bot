import json
import sqlite3
from decimal import Decimal

from bot.outcome_capital_efficiency_report import report as capital_report
from bot.outcome_efficiency_shadow import OutcomeConfidenceEntryShadow, OutcomeQueueAwarePricingShadow
from bot.outcome_efficiency_milestone_report import report as milestone_report
from bot.outcome_fair_value_model import OutcomeFairValueShadow, train_report
from bot.outcome_holding_path import OutcomeHoldingPathObservation, OutcomeHoldingPathRecorder
from bot.outcome_marketable_profit_exit_report import report as marketable_report
from bot.outcome_oi_features import FEATURE_SCHEMA_VERSION
from bot.outcome_portfolio_allocator import OutcomePortfolioAllocator, OutcomePortfolioCandidate
from monitoring.trade_journal_db import TradeJournalDB


def test_e1_capital_report_attributes_directional_idle_without_process_gap(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    with sqlite3.connect(journal.db_path) as conn:
        for index, (ts, reason, submitted) in enumerate((
            ("2026-09-10T00:00:00+00:00", "live strategy no entry: directional_confirmation_not_met", 0),
            ("2026-09-10T00:00:05+00:00", "live strategy no entry: entry_spread_exceeds_calibrated_ceiling", 0),
            ("2026-09-10T00:02:00+00:00", "placed first-level ALO buy", 1),
        )):
            conn.execute(
                "INSERT INTO strategy_events(ts,run_id,event_type,payload_json) VALUES(?, 'run','OUTCOME_ENTRY_ADMISSION_DECISION',?)",
                (ts, json.dumps({"period": "1d", "outcome_id": 1, "final_reason": reason, "execution_submitted": bool(submitted)})),
            )
        conn.commit()
    result = capital_report(journal.db_path)
    funnel = {row["state"]: row for row in result["time_funnel"]}
    assert funnel["no_directional_edge"]["attributed_seconds"] == 5.0
    assert funnel["execution_quality_rejected"]["attributed_seconds"] == 30.0
    assert funnel["buy_submitted"]["observations"] == 1


def _features(bid: float) -> dict:
    return {
        "yes_bid": bid, "yes_ask": bid + 0.01, "no_bid": 0.99 - bid, "no_ask": 1.0 - bid,
        "time_left_sec": 40000, "btc_mark_return_300s_bps": 10,
        "btc_mark_return_900s_bps": 20, "btc_mark_return_3600s_bps": 30,
        "oi_return_300s_bps": 2,
    }


def test_e2_train_artifact_and_e3_e4_shadow_never_gain_live_authority(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    for index, bid in enumerate((0.55, 0.57, 0.59)):
        features = _features(bid)
        labels = {"future_300s": {"available": True, "yes_future_bid": bid + 0.02, "no_future_bid": 0.98 - bid}}
        assert journal.upsert_outcome_oi_feature_row(
            feature_schema_version=FEATURE_SCHEMA_VERSION, outcome_snapshot_event_id=index + 1,
            outcome_id=1, period="1d", snapshot_timestamp_ms=1_000_000 + index * 300_000,
            oi_observation_id=index + 1, oi_exchange_timestamp_ms=1, oi_local_received_at_ms=1,
            oi_age_ms=1, oi_join_direction="as_of_local_received_at", oi_backfilled=False,
            features=features, labels=labels, market_context={},
        )
    artifact = tmp_path / "fair.json"
    trained = train_report(journal.db_path, artifact_path=artifact)
    assert trained["shadow_model_available"] is True and trained["ready_for_live"] is False
    fair = OutcomeFairValueShadow(artifact)
    context = {
        "yes_best_bid": 0.59, "yes_best_ask": 0.60, "no_best_bid": 0.40, "no_best_ask": 0.41,
        "mark_return_bps": 10, "oi_return_bps": 2,
        "trend_continuation": {"mark_15m_bps": 20, "mark_60m_bps": 30},
    }
    confidence = OutcomeConfidenceEntryShadow(fair).evaluate(context=context, time_left_sec=40000)
    assert confidence["execution_submitted"] is False
    score = confidence["scores"]["0"]
    quote = OutcomeQueueAwarePricingShadow().evaluate(
        side_index=0,
        book={"bids": [{"price": "0.59", "size": "50"}, {"price": "0.58", "size": "50"}],
              "asks": [{"price": "0.60", "size": "50"}, {"price": "0.61", "size": "50"}]},
        requested_shares=20, fair_score=score, current_bid=Decimal("0.59"),
    )
    assert quote["live_authority"] is False and quote["production_bid_unchanged"] == "0.59"


def test_e5_marketability_requires_full_depth_vwap_and_entry_taker_fee(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    recorder = OutcomeHoldingPathRecorder(journal, "run")
    recorder.record(OutcomeHoldingPathObservation(
        1, "1d", "#10", Decimal("20"), Decimal("0.60"), Decimal("0.62"), Decimal("0.63"),
        Decimal("0.0004"), 60, 1000, "fresh_rest_book", {},
        entry_lifecycle_id="life", entry_trade_id="trade", marketable_exit_vwap=Decimal("0.62"),
        marketable_exit_depth_shares=Decimal("20"), taker_close_fee_rate=Decimal("0.001"),
    ))
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            """INSERT INTO outcome_realized_pnl_lots
               VALUES('close','trade',1,0,'sell','20','12','12.8','0.8','{}','2026-09-10T00:02:00+00:00')"""
        )
        conn.commit()
    result = marketable_report(journal.db_path)
    assert result["lifecycles_with_depth_and_taker_fee"] == 1
    assert result["lifecycles"][0]["thresholds"]["1%"]["first_depth_sufficient_hit_ts"] is not None
    assert result["ready_for_live"] is False


def test_e5_excludes_partial_depth_even_if_a_vwap_was_supplied(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    recorder = OutcomeHoldingPathRecorder(journal, "run")
    recorder.record(OutcomeHoldingPathObservation(
        1, "1d", "#10", Decimal("20"), Decimal("0.60"), Decimal("0.62"), Decimal("0.63"),
        Decimal("0.0004"), 60, 1000, "fresh_rest_book", {},
        entry_lifecycle_id="life", entry_trade_id="trade", marketable_exit_vwap=Decimal("0.62"),
        marketable_exit_depth_shares=Decimal("10"), taker_close_fee_rate=Decimal("0.001"),
    ))
    result = marketable_report(journal.db_path)
    assert result["lifecycles_with_depth_and_taker_fee"] == 0
    assert result["legacy_observations_excluded"] == 1


def test_e6_allocator_respects_global_market_group_and_venue_minimum():
    allocator = OutcomePortfolioAllocator(
        global_notional_cap=Decimal("40"), per_market_cap=Decimal("20"),
        correlation_group_cap=Decimal("20"), max_active_markets=2,
    )
    candidates = (
        OutcomePortfolioCandidate("a", 1, "#1", "BTC", Decimal("0.03"), 600, Decimal("20"), Decimal("20")),
        OutcomePortfolioCandidate("b", 2, "#2", "BTC", Decimal("0.02"), 600, Decimal("20"), Decimal("20")),
        OutcomePortfolioCandidate("c", 3, "#3", "ETH", Decimal("0.01"), 600, Decimal("20"), Decimal("9")),
    )
    result = {row.candidate_id: row for row in allocator.allocate(candidates)}
    assert result["a"].allocated_notional == Decimal("20")
    assert result["b"].reason == "capacity_below_minimum"
    assert result["c"].reason == "capacity_below_minimum"


def test_e4_malformed_book_is_unavailable_instead_of_raising():
    result = OutcomeQueueAwarePricingShadow().evaluate(
        side_index=0,
        book={"bids": [{"price": None, "size": "50"}], "asks": [{"price": "0.60", "size": "50"}]},
        requested_shares=20,
        fair_score={"available": True, "one_sided_95pct_lower_edge": 0.01, "confidence": "high"},
        current_bid=Decimal("0.59"),
    )
    assert result == {"available": False, "reason": "valid_book_unavailable", "live_authority": False}


def test_unified_milestone_report_is_read_only_and_handles_empty_evidence(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    result = milestone_report(journal.db_path)
    assert result["research_implementation_complete"] is True
    assert result["live_policy_changed"] is False
    assert result["e2_executable_fair_value"]["ready_for_live"] is False
    assert result["e5_marketable_profit_exit"]["ready_for_live"] is False
    assert result["e6_portfolio_allocator"]["live_authority"] is False
