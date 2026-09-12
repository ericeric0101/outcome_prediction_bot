import json
import sqlite3
from decimal import Decimal

from bot.outcome_crash_circuit_report import report as crash_report
from bot.outcome_crash_circuit_shadow import OutcomeCrashCircuitObservation, OutcomeCrashCircuitShadow
from bot.outcome_holding_path import OutcomeHoldingPathObservation, OutcomeHoldingPathRecorder
from bot.outcome_profit_lock_shadow_report import report as profit_lock_report
from bot.outcome_thesis_failure_shadow_report import report as thesis_failure_report
from monitoring.trade_journal_db import TradeJournalDB


def _path(*, bid: str, age: float) -> OutcomeHoldingPathObservation:
    return OutcomeHoldingPathObservation(
        outcome_id=7, period="1d", coin="#70", inventory=Decimal("10"), fill_vwap=Decimal("0.90"),
        best_bid=Decimal(bid), best_ask=Decimal(str(Decimal(bid) + Decimal("0.01"))),
        maker_close_fee_rate=Decimal("0.0004"), holding_age_sec=age, time_left_sec=50000,
        book_health="fresh_rest_book", oi_evidence={}, entry_lifecycle_id="official_buy:o:t",
        entry_order_id="o", entry_trade_id="t", entry_filled_at="2026-09-12T00:00:00+00:00",
        entry_filled_at_source="official_fill_timestamp_ms", entry_side_index=0,
        marketable_exit_vwap=Decimal(bid), marketable_exit_depth_shares=Decimal("10"),
        taker_close_fee_rate=Decimal("0.0007"),
    )


def _set_times(journal: TradeJournalDB, event_ids: list[int], timestamps: list[str]) -> None:
    with sqlite3.connect(journal.db_path) as conn:
        for event_id, timestamp in zip(event_ids, timestamps):
            conn.execute("UPDATE strategy_events SET ts=? WHERE id=?", (timestamp, event_id))
        conn.commit()


def test_profit_lock_report_uses_first_full_depth_b5_action_and_never_marks_live(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    recorder = OutcomeHoldingPathRecorder(journal, "run")
    path_ids = [recorder.record(_path(bid=bid, age=age)) for bid, age in (("0.92", 60), ("0.65", 360), ("0.70", 960))]
    # recorder returns None; retrieve IDs as the production recorder would.
    with sqlite3.connect(journal.db_path) as conn:
        path_ids = [row[0] for row in conn.execute("SELECT id FROM strategy_events ORDER BY id")]
    _set_times(journal, path_ids, [
        "2026-09-12T00:00:00+00:00", "2026-09-12T00:05:00+00:00", "2026-09-12T00:15:00+00:00",
    ])
    event_id = journal.log_strategy_event("run", "OUTCOME_ACTIVE_HOLDING_CHALLENGER_SHADOW", {
        "period": "1d", "outcome_id": 7, "coin": "#70", "entry_lifecycle_id": "official_buy:o:t",
        "holding_age_sec": 60, "challenger": {"action": "MARKETABLE_PROFIT_EXIT"},
        "full_depth_execution": {
            "inventory": "10", "fill_vwap": "0.90", "marketable_exit_vwap": "0.92",
            "marketable_exit_depth_shares": "10", "marketable_exit_full_inventory": True,
            "taker_close_fee_rate": "0.0007", "marketable_net_exit_price": "0.919356",
            "marketable_net_exit_vs_entry_pct": "0.0215066666666666666666666667",
        }, "execution_submitted": False, "live_authority": False,
    })
    _set_times(journal, [event_id], ["2026-09-12T00:00:00+00:00"])
    result = profit_lock_report(journal.db_path)
    assert result["profit_lock_lifecycles"] == 1
    lifecycle = result["lifecycles"][0]
    assert lifecycle["later_full_depth_net_return_by_horizon_sec"]["300"] is not None
    assert lifecycle["later_fell_below_cost"] is True
    assert result["ready_for_live"] is False


def test_crash_shadow_emits_only_research_state_from_joint_velocity_and_depth():
    shadow = OutcomeCrashCircuitShadow()
    base = dict(
        lifecycle_id="official_buy:o:t", outcome_id=7, period="1d", coin="#70", fill_vwap=Decimal("0.90"),
        ask=Decimal("0.91"), spot_strike_bps=Decimal("-100"), mark_return_bps=Decimal("-110"),
        oi_return_bps=Decimal("15"), oi_age_ms=1000, regime_state="TRANSITION", reversal_state="WEAKENING",
    )
    shadow.observe(OutcomeCrashCircuitObservation(timestamp=0, bid=Decimal("0.90"), top3_bid_depth=Decimal("100"), **base))
    result = shadow.observe(OutcomeCrashCircuitObservation(timestamp=31, bid=Decimal("0.83"), top3_bid_depth=Decimal("50"), **base))
    assert result["research_state"] == "RAPID_DRAWDOWN_RESEARCH"
    assert result["episode_started"] is True
    assert result["live_authority"] is False
    assert result["execution_submitted"] is False


def test_crash_shadow_keeps_lifecycle_time_and_entry_audit_without_execution():
    shadow = OutcomeCrashCircuitShadow()
    result = shadow.observe(OutcomeCrashCircuitObservation(
        lifecycle_id="official_buy:o:t", outcome_id=7, period="1d", coin="#70", timestamp=1,
        fill_vwap=Decimal("0.90"), bid=Decimal("0.89"), ask=Decimal("0.90"), top3_bid_depth=Decimal("100"),
        spot_strike_bps=Decimal("-1"), mark_return_bps=Decimal("-2"), oi_return_bps=Decimal("3"),
        oi_age_ms=1000, regime_state="TRANSITION", reversal_state="WEAKENING",
        holding_age_sec=7200, time_left_sec=3500, entry_side_index=0,
        entry_tier="tier_b_spot_mark", entry_target_return_pct="0.02",
    ))
    assert result["holding_age_sec"] == 7200
    assert result["time_left_sec"] == 3500
    assert result["entry_tier"] == "tier_b_spot_mark"
    assert result["live_authority"] is False


def test_crash_report_joins_episode_to_later_full_depth_path(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    recorder = OutcomeHoldingPathRecorder(journal, "run")
    recorder.record(_path(bid="0.60", age=300))
    with sqlite3.connect(journal.db_path) as conn:
        path_id = conn.execute("SELECT id FROM strategy_events ORDER BY id LIMIT 1").fetchone()[0]
    _set_times(journal, [path_id], ["2026-09-12T00:05:00+00:00"])
    event_id = journal.log_strategy_event("run", "OUTCOME_CRASH_CIRCUIT_SHADOW", {
        "period": "1d", "outcome_id": 7, "coin": "#70", "entry_lifecycle_id": "official_buy:o:t",
        "research_state": "RAPID_DRAWDOWN_RESEARCH", "research_episode_id": 1,
        "ws_top_bid_gross_return_pct": "-0.08", "bid_velocity_bps": {"30": "-300"},
        "top3_depth_ratio": {"30": "0.5"}, "execution_submitted": False, "live_authority": False,
    })
    _set_times(journal, [event_id], ["2026-09-12T00:00:00+00:00"])
    result = crash_report(journal.db_path)
    assert result["rapid_drawdown_episodes"] == 1
    assert result["episodes"][0]["later_full_depth_net_return_by_horizon_sec"]["300"] is not None
    assert result["ready_for_live"] is False


def test_thesis_failure_report_is_lifecycle_bound_and_marks_manual_close_as_audit_only(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    recorder = OutcomeHoldingPathRecorder(journal, "run")
    recorder.record(_path(bid="0.60", age=300))
    with sqlite3.connect(journal.db_path) as conn:
        path_id = conn.execute("SELECT id FROM strategy_events ORDER BY id LIMIT 1").fetchone()[0]
    _set_times(journal, [path_id], ["2026-09-12T00:05:00+00:00"])
    event_id = journal.log_strategy_event("run", "OUTCOME_REVERSAL_SHADOW_DECISION", {
        "period": "1d", "outcome_id": 7, "coin": "#70", "entry_lifecycle_id": "official_buy:o:t",
        "entry_trade_id": "t", "state": "REVERSAL_CONFIRMED", "holding_age_sec": 60,
        "time_left_sec": 3500, "entry_side_index": 0, "entry_tier": "tier_a",
        "entry_target_return_pct": "0.02", "execution_submitted": False,
    })
    _set_times(journal, [event_id], ["2026-09-12T00:00:00+00:00"])
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            """INSERT INTO order_events(ts,run_id,event_type,side,price,qty,instrument_id,payload_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("2026-09-12T00:01:00+00:00", "run", "ORDER_FILLED", "SELL", .6, 10, "#70",
             json.dumps({"execution_origin": "external_manual_or_unknown"})),
        )
        conn.commit()
    result = thesis_failure_report(journal.db_path)
    assert result["episode_count"] == 1
    episode = result["episodes"][0]
    assert episode["trigger_kind"] == "reversal_confirmed"
    assert episode["time_left_bucket"] == "under_2h"
    assert episode["first_later_sell_execution_origin"] == "external_manual_or_unknown"
    assert result["ready_for_live"] is False
