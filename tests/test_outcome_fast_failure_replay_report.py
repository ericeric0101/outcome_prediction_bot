import json
import sqlite3

from bot.outcome_fast_failure_replay_report import report
from monitoring.trade_journal_db import TradeJournalDB


def _event(journal, ts, event_type, payload):
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO strategy_events(ts,run_id,event_type,payload_json) VALUES(?,?,?,?)",
            (ts, "run", event_type, json.dumps(payload)),
        )
        conn.commit()


def _path(*, ts, lifecycle, trade, outcome, net, full=True):
    return {
        "period": "1d", "entry_lifecycle_id": lifecycle, "entry_trade_id": trade,
        "outcome_id": outcome, "coin": "#10", "entry_side_index": 0, "fill_vwap": "0.9",
        "marketable_net_exit_vs_entry_pct": net, "marketable_exit_full_inventory": full,
        "holding_age_sec": 120, "time_left_sec": 4000, "spot_strike_bps": "50",
    }


def _crash(lifecycle):
    return {
        "period": "1d", "entry_lifecycle_id": lifecycle,
        "bid_velocity_bps": {"30": "-300"}, "top3_depth_ratio": {"30": "0.5"},
    }


def test_replay_requires_persistent_multi_signal_and_marks_cap_status(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    lifecycle, trade = "official_buy:1:t", "t"
    _event(journal, "2026-09-12T00:00:00+00:00", "OUTCOME_CRASH_CIRCUIT_SHADOW", _crash(lifecycle))
    _event(journal, "2026-09-12T00:00:00+00:00", "OUTCOME_HOLDING_PATH_OBSERVATION", _path(ts=0, lifecycle=lifecycle, trade=trade, outcome=2437, net="-0.11"))
    _event(journal, "2026-09-12T00:00:11+00:00", "OUTCOME_HOLDING_PATH_OBSERVATION", _path(ts=11, lifecycle=lifecycle, trade=trade, outcome=2437, net="-0.11"))
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO outcome_realized_pnl_lots(close_trade_id,open_trade_id,outcome_id,side_index,close_kind,quantity,cost_usdc,proceeds_usdc,realized_net_usdc,source_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("c", trade, 2437, 0, "normal", "1", "10", "8", "-2", "{}", "2026-09-12T00:01:00+00:00"),
        )
        conn.commit()
    result = report(journal.db_path)
    row = result["lifecycles"][0]
    assert row["warning_candidate"] is not None
    assert row["hard_candidate"]["within_hard_cap"] is True
    assert result["summary"]["hard_candidate_within_cap_count"] == 1
    assert result["summary"]["profitable_lifecycles_with_hard_candidate_within_cap"] == 0
    assert result["live_authority"] is False


def test_replay_does_not_call_deep_loss_within_cap(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    lifecycle = "official_buy:2:t"
    _event(journal, "2026-09-12T00:00:00+00:00", "OUTCOME_CRASH_CIRCUIT_SHADOW", _crash(lifecycle))
    _event(journal, "2026-09-12T00:00:00+00:00", "OUTCOME_HOLDING_PATH_OBSERVATION", _path(ts=0, lifecycle=lifecycle, trade="t", outcome=2820, net="-0.2"))
    _event(journal, "2026-09-12T00:00:11+00:00", "OUTCOME_HOLDING_PATH_OBSERVATION", _path(ts=11, lifecycle=lifecycle, trade="t", outcome=2820, net="-0.2"))
    row = report(journal.db_path)["lifecycles"][0]
    assert row["hard_candidate"] is not None
    assert row["hard_candidate"]["within_hard_cap"] is False


def test_replay_can_rebuild_control_case_velocity_from_raw_ws(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    lifecycle = "official_buy:3:t"
    raw = lambda bid, depth: {"outcome_id": 2437, "raw": {"data": {"coin": "#10", "levels": [[
        {"px": bid, "sz": depth}], [{"px": "0.95", "sz": "10"}]]}}}
    _event(journal, "2026-09-11T23:59:20+00:00", "OUTCOME_WS_L2_BOOK", raw("0.9", "100"))
    _event(journal, "2026-09-12T00:00:00+00:00", "OUTCOME_WS_L2_BOOK", raw("0.85", "50"))
    _event(journal, "2026-09-12T00:00:11+00:00", "OUTCOME_WS_L2_BOOK", raw("0.85", "50"))
    _event(journal, "2026-09-12T00:00:00+00:00", "OUTCOME_HOLDING_PATH_OBSERVATION", _path(ts=0, lifecycle=lifecycle, trade="t", outcome=2437, net="-0.11"))
    _event(journal, "2026-09-12T00:00:11+00:00", "OUTCOME_HOLDING_PATH_OBSERVATION", _path(ts=11, lifecycle=lifecycle, trade="t", outcome=2437, net="-0.11"))
    row = report(journal.db_path, include_raw_known=True)["lifecycles"][0]
    assert row["hard_candidate"]["microstructure_source"] == "raw_ws_l2"


def test_replay_requires_meaningful_recovery_before_second_drawdown_label(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    lifecycle = "official_buy:4:t"
    for ts, net in (("2026-09-12T00:00:00+00:00", "-0.21"),
                    ("2026-09-12T00:00:11+00:00", "-0.09"),
                    ("2026-09-12T00:00:22+00:00", "-0.22")):
        _event(journal, ts, "OUTCOME_HOLDING_PATH_OBSERVATION",
               _path(ts=0, lifecycle=lifecycle, trade="t", outcome=2437, net=net))
    row = report(journal.db_path)["lifecycles"][0]
    candidate = row["second_drawdown_after_recovery_candidate"]
    assert candidate["first_drawdown_pct"] == "-0.21"
    assert candidate["recovery_pct"] == "-0.09"
    assert candidate["rebreak_pct"] == "-0.22"
