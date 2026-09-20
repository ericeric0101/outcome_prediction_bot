import json
import sqlite3
import time

import pytest

from bot.outcome_entry_timing_replay_report import as_dict
from monitoring.trade_journal_db import TradeJournalDB


def test_timing_replay_uses_only_exact_audited_s0_fill_and_p3_markout(tmp_path):
    journal = TradeJournalDB(tmp_path / "timing.db")
    now_ms = int(time.time() * 1000)
    with sqlite3.connect(journal.db_path) as conn:
        for index, (offset, oi, mark) in enumerate(((-300_000, "100", "100"), (0, "101", "101"))):
            stamp = now_ms + offset
            conn.execute(
                """INSERT INTO binance_oi_observations(
                    run_id,source,endpoint,symbol,exchange_timestamp_ms,local_received_at_ms,
                    request_latency_ms,open_interest,mark_price,backfilled,raw_payload_hash,raw_payload_json,context_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("run", "binance", "fixture", "BTCUSDT", stamp, stamp, 1.0, oi, mark, 0, f"hash-{index}", "{}", "{}"),
            )
        conn.commit()
    journal.log_order_event(
        "run", "ORDER_SUBMIT", venue_order_id="owned-buy", side="BUY", instrument_id="#7",
        payload={"audit": {
            "entry_policy_kind": "s0_oi_spot_mark_confirmation",
            "target_decision_at_ms": now_ms,
        }},
    )
    journal.log_strategy_event("run", "OUTCOME_LIVE_STRATEGY_ENTRY_PLACED", {
        "order_id": "owned-buy", "entry_evidence": {"spot_strike_bps": "20"},
    })
    journal.log_order_event(
        "run", "ORDER_FILLED", venue_order_id="owned-buy", side="BUY", instrument_id="#7",
        payload={
            "actual_fill": True, "period": "1d", "liquidity_class": "maker", "trade_id": "trade-1",
        },
    )
    journal.log_order_event(
        "run", "FILL_MARKOUT", side="BUY", instrument_id="#7",
        payload={
            "actual_fill": True, "p3_markout_schema_version": 2,
            "fill_id": "trade-1", "horizon_sec": 30, "signed_markout_ps": "-0.01",
        },
    )

    report = as_dict(journal.db_path)
    assert report["status"] == "read_only"
    assert report["actual_maker_buy_fills_with_exact_submission_audit"] == 1
    assert report["short_lookback_replay"]["300"]["would_still_qualify"] == 1
    assert report["stale_passive_cancel_only_replay"]["15"]["fills_kept_before_cancel_age"] == 1
    assert report["stale_passive_cancel_only_replay"]["15"]["delayed_fills_cancelled_counterfactually"] == 0
    assert report["fill_delay_bucket_markouts"]
    assert json.loads(json.dumps(report))["report"] == "outcome_entry_timing_replay"


def test_stale_cancel_counterfactual_requires_tape_price_through_and_marks_queue_unknown(tmp_path):
    journal = TradeJournalDB(tmp_path / "timing-stale.db")
    now_ms = int(time.time() * 1000)
    journal.log_order_event(
        "run", "ORDER_SUBMIT", venue_order_id="stale-buy", side="BUY", instrument_id="#7",
        payload={"audit": {
            "entry_policy_kind": "s0_oi_spot_mark_confirmation",
            "target_decision_at_ms": now_ms - 61_000,
            "entry_submit_bid": "0.6", "entry_submitted_shares": "10",
        }},
    )
    journal.log_strategy_event("run", "OUTCOME_STALE_ENTRY_CANCEL_CONFIRMED", {
        "order_id": "stale-buy", "outcome_id": 7, "coin": "#7", "order_age_sec": 60.0,
    })
    journal.log_strategy_event("run", "OUTCOME_WS_TRADES", {
        "raw": {"data": [{
            "coin": "#7", "side": "A", "px": "0.59", "sz": "20", "time": now_ms + 1_000,
            "tid": 123,
        }]},
    })
    # A single post-horizon full-depth book is sufficient for the 5 minute
    # outcome; absent 15/30m snapshots must remain missing rather than being
    # interpolated from this row.
    journal.log_strategy_event("run", "OUTCOME_P2_PARITY_SNAPSHOT", {
        "snapshot_timestamp_ms": now_ms + 5 * 60_000 + 2_000,
        "yes_coin": "#7", "no_coin": "#8",
        "yes_l2": {"levels": [[{"px": "0.63", "sz": "10"}], []]},
        "no_l2": {"levels": [[], []]},
    })

    report = as_dict(journal.db_path)
    replay = report["stale_cancel_price_through_counterfactual"]
    assert replay["status"] == "read_only_conditional_not_a_fill_reconstruction"
    row = replay["orders"][0]
    assert row["tape_price_through_observed"] is True
    assert row["queue_fill_status"] == "not_reconstructible_from_public_tape_and_l2"
    assert row["forward_full_depth"]["5"]["status"] == "full_depth_available"
    assert row["forward_full_depth"]["5"]["gross_pnl_usdc"] == pytest.approx(0.3)
    assert row["forward_full_depth"]["15"]["status"] == "snapshot_missing"
