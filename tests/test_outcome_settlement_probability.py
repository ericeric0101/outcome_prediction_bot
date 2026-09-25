import json
import math
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from bot.outcome_active_model import settlement_probability_report
from bot.outcome_settlement_probability import estimate_settlement_probability
from bot.outcome_research_capture import OutcomeResearchCapture
from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from monitoring.trade_journal_db import TradeJournalDB


def _history(start_ms: int, *, count: int = 301):
    # Small, deterministic returns with adequate 25-minute valid coverage.
    return [(start_ms + i * 5_000, 80_000.0 * math.exp((i % 7 - 3) * 0.00001)) for i in range(count)]


def test_settlement_probability_uses_asof_history_and_emits_both_sides():
    now = 1_800_000_000_000
    result = estimate_settlement_probability(
        spot_price=80_100, strike=80_000, time_left_sec=10_800,
        as_of_ms=now, price_points=_history(now - 1_500_000), max_spot_age_ms=15_000,
    )
    assert result["status"] == "available"
    assert result["probability_up"] > 0.5
    assert result["probability_up"] + result["probability_down"] == 1.0
    assert result["live_authority"] is False


def test_settlement_probability_rejects_stale_or_gapped_history_without_interpolation():
    now = 1_800_000_000_000
    stale = estimate_settlement_probability(
        spot_price=80_100, strike=80_000, time_left_sec=10_800,
        as_of_ms=now, price_points=_history(now - 1_520_000), max_spot_age_ms=15_000,
    )
    assert stale["reason"] == "spot_observation_stale"
    sparse = [(now - 1_500_000, 80_000.0), (now - 900_000, 80_010.0), (now, 80_100.0)]
    result = estimate_settlement_probability(
        spot_price=80_100, strike=80_000, time_left_sec=10_800,
        as_of_ms=now, price_points=sparse, max_spot_age_ms=15_000,
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "volatility_history_insufficient"
    assert result["valid_returns"] == 0


def test_research_capture_persists_compact_probability_comparison(tmp_path):
    journal = TradeJournalDB(str(tmp_path / "shadow.db"))
    market = OutcomeMarketSpec(
        516, "@516", "#5160", "#5161", 100005160, 100005161,
        "priceBinary", "BTC", "20260830-0000", 2_000_000_000, 1,
        Decimal("70000"), "1d", "raw",
    )
    yes = {"time": 2_000, "levels": [[{"px": "0.70", "sz": "20"}], [{"px": "0.72", "sz": "20"}]]}
    no = {"time": 2_000, "levels": [[{"px": "0.27", "sz": "20"}], [{"px": "0.29", "sz": "20"}]]}
    capture = OutcomeResearchCapture(
        client=object(), wallet_address="0x" + "a" * 40, journal=journal,
        interval_sec=1, heartbeat_sec=1, gap_alert_sec=3,
    )
    forecast = {
        "status": "available", "probability_up": 0.68, "probability_down": 0.32,
        "source": "hyperliquid_spot_l2_mid", "live_authority": False,
        "execution_enabled": False,
    }
    capture.capture_if_due(
        market=market, yes_book=yes, no_book=no, yes_local_received_at_ms=2_000,
        no_local_received_at_ms=2_000, capture_complete_at_ms=2_000,
        settlement_probability_shadow=forecast,
    )
    with sqlite3.connect(journal.db_path) as conn:
        event = json.loads(conn.execute(
            "SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_SETTLEMENT_PROBABILITY_SHADOW'"
        ).fetchone()[0])
        p2 = json.loads(conn.execute(
            "SELECT payload_json FROM strategy_events WHERE event_type='OUTCOME_P2_PARITY_SNAPSHOT'"
        ).fetchone()[0])
    assert event["probability_down"] == 0.32
    assert event["market_up_probability_mid"] == "0.71"
    assert event["market_down_probability_mid"] == "0.28"
    assert p2["settlement_probability_shadow"]["status"] == "available"
    assert p2["settlement_probability_shadow"]["live_authority"] is False


def test_settlement_probability_report_scores_online_shadow_on_official_labels(tmp_path):
    journal = TradeJournalDB(str(tmp_path / "shadow.db"))
    settled_at = datetime.fromtimestamp(1_800_010_000, tz=timezone.utc).isoformat()
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO outcome_market_settlement_registry VALUES(?,?,?,?,?,?)",
            (77, 1, "official_sdk_settled_outcome+official_account_evidence", "1", "{}", settled_at),
        )
    journal.log_strategy_event("test", "OUTCOME_SETTLEMENT_PROBABILITY_SHADOW", {
        "status": "available", "outcome_id": 77, "decision_timestamp_ms": 1_800_000_000_000,
        "time_left_sec": 10_800, "market_up_probability_mid": "0.70", "probability_up": 0.60,
        "capture_quality_status": "accepted",
    })
    report = settlement_probability_report(journal.db_path)
    metrics = report["checkpoints"]["10800"]["online_spot_shadow_common_checkpoint_metrics"]
    assert metrics["markets"] == 1
    assert metrics["market_midpoint"]["rows"] == 1
    assert metrics["hyperliquid_spot_strike_realized_vol"]["rows"] == 1
    assert metrics["hyperliquid_spot_strike_realized_vol"]["brier"] == 0.36
