import json
from datetime import datetime, timezone
from decimal import Decimal

from bot.outcome_active_dataset import ACTIVE_DATASET_SCHEMA_VERSION, dataset_report, feature_vector, load_decision_rows
from bot.outcome_calendar_features import market_session_weekday
from bot.outcome_active_decision import ActiveSideInput, OutcomeActiveActionOptimizer
from bot.outcome_active_model import OutcomeActiveModel, settlement_probability_report, train_report
from bot.outcome_active_replay import replay_report
from bot.outcome_active_shadow import OutcomeActiveChallengerShadow
from bot.outcome_active_shadow_report import shadow_report
from bot.outcome_oi_features import FEATURE_SCHEMA_VERSION
from monitoring.trade_journal_db import TradeJournalDB


def _seed_rows(journal: TradeJournalDB, *, markets: int = 5, timestamps: int = 8) -> None:
    event_id = 0
    for market in range(1, markets + 1):
        for index in range(timestamps):
            event_id += 1
            yes_bid = 0.54 + market * 0.005 + index * 0.002
            features = {
                "yes_bid": yes_bid, "yes_ask": yes_bid + 0.01,
                "no_bid": 0.99 - yes_bid, "no_ask": 1.0 - yes_bid,
                "time_left_sec": 80_000 - index * 60,
                "btc_mark_return_300s_bps": 10 + index,
                "btc_mark_return_900s_bps": 15 + index,
                "btc_mark_return_3600s_bps": 20 + index,
                "oi_return_300s_bps": 3,
            }
            labels = {}
            for horizon in (60, 300, 600, 900, 1800, 3600):
                move = 0.002 + horizon / 100_000
                labels[f"future_{horizon}s"] = {
                    "available": True,
                    "yes_future_bid": min(0.98, yes_bid + move),
                    "yes_future_ask": min(0.99, yes_bid + move + 0.01),
                    "no_future_bid": max(0.01, 0.99 - yes_bid - move),
                    "no_future_ask": max(0.02, 1.0 - yes_bid - move),
                }
            assert journal.upsert_outcome_oi_feature_row(
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                outcome_snapshot_event_id=event_id,
                outcome_id=market,
                period="1d",
                snapshot_timestamp_ms=market * 100_000_000 + index * 60_000,
                oi_observation_id=event_id,
                oi_exchange_timestamp_ms=1,
                oi_local_received_at_ms=1,
                oi_age_ms=1,
                oi_join_direction="as_of_local_received_at",
                oi_backfilled=False,
                features=features,
                labels=labels,
                market_context={},
            )


def test_b1_dataset_is_sampled_and_has_discrete_path_labels(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    _seed_rows(journal, markets=2, timestamps=3)
    rows = load_decision_rows(journal.db_path)
    assert len(rows) == 12
    assert rows[0].targets["future_bid_300s"] is not None
    assert "observed_horizon_mfe" in rows[0].targets
    result = dataset_report(journal.db_path)
    assert result["schema_version"] == ACTIVE_DATASET_SCHEMA_VERSION
    assert result["live_authority"] is False


def test_calendar_features_follow_taipei_14h_market_rollover_not_midnight():
    # 13:59 Taipei Saturday is still the Friday market; 14:00 begins Saturday.
    before_rollover = int(datetime(2026, 9, 12, 5, 59, tzinfo=timezone.utc).timestamp() * 1000)
    after_rollover = int(datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert market_session_weekday(before_rollover) == 4
    assert market_session_weekday(after_rollover) == 5
    features = {
        "yes_bid": 0.60, "yes_ask": 0.61, "no_bid": 0.39, "no_ask": 0.40,
        "time_left_sec": 70_000, "btc_mark_return_300s_bps": 10,
        "btc_mark_return_900s_bps": 20, "btc_mark_return_3600s_bps": 30,
        "oi_return_300s_bps": 2,
    }
    weekday_vector = feature_vector(features, 0, timestamp_ms=before_rollover)
    weekend_vector = feature_vector(features, 0, timestamp_ms=after_rollover)
    assert weekday_vector is not None and weekend_vector is not None
    assert weekday_vector[8] == 0.0
    assert weekend_vector[8] == 1.0


def test_b2_model_uses_market_walk_forward_and_writes_shadow_artifact(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    _seed_rows(journal)
    artifact = tmp_path / "active.json"
    result = train_report(journal.db_path, artifact_path=artifact)
    assert result["market_instances"] == 5
    assert result["folds"]
    assert result["ready_for_live"] is False
    model = OutcomeActiveModel(artifact)
    row = load_decision_rows(journal.db_path)[0]
    score = model.score(row.vector, spread_bps=80)
    assert score["available"] is True
    assert score["live_authority"] is False
    assert "future_bid_900s" in score["outputs"]
    assert result["oos_metrics"]["future_bid_300s"]["persistence_rmse"] is not None
    assert result["settlement_probability"]["live_authority"] is False


def test_official_settlement_probability_uses_common_expanding_oos_rows(tmp_path):
    journal = TradeJournalDB(tmp_path / "settlement.db")
    _seed_rows(journal, markets=12, timestamps=8)
    import hashlib
    import sqlite3
    from datetime import timedelta

    with sqlite3.connect(journal.db_path) as conn:
        for market in range(1, 13):
            rows = conn.execute(
                "SELECT snapshot_timestamp_ms,features_json FROM outcome_oi_feature_rows WHERE outcome_id=? ORDER BY snapshot_timestamp_ms",
                (market,),
            ).fetchall()
            decision_ms = int(rows[0][0])
            for timestamp_ms, raw_features in rows:
                features = json.loads(raw_features)
                features.update({
                    "time_left_sec": 3600,
                    "strike": 100.0,
                    "binance_mark_price": 100.0 + market * 0.1,
                    "btc_mark_return_300s_bps": market,
                    "btc_mark_return_900s_bps": market * 2,
                    "oi_return_300s_bps": market / 10,
                    "taker_imbalance": 0.1 if market % 2 else -0.1,
                })
                conn.execute(
                    "UPDATE outcome_oi_feature_rows SET features_json=? WHERE outcome_id=? AND snapshot_timestamp_ms=?",
                    (json.dumps(features), market, timestamp_ms),
                )
            settlement_time = datetime.fromtimestamp((decision_ms + 3_600_000) / 1000, tz=timezone.utc)
            conn.execute(
                "INSERT INTO outcome_market_settlement_registry VALUES(?,?,?,?,?,?)",
                (market, market % 2, "official_sdk_settled_outcome+official_account_evidence", "1.0", "{}", settlement_time.isoformat()),
            )
            for sample in range(61):
                mark_ts = decision_ms - (60 - sample) * 30_000
                mark = 100_000 + market * 100 + sample * (1 if market % 2 else -1)
                digest = hashlib.sha256(f"{market}:{sample}".encode()).hexdigest()
                conn.execute(
                    """INSERT INTO binance_oi_observations(
                        run_id,source,endpoint,symbol,exchange_timestamp_ms,local_received_at_ms,request_latency_ms,
                        open_interest,mark_price,backfilled,raw_payload_hash,raw_payload_json,context_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?)""",
                    (f"run-{market}", "binance_usdm_public", "/fapi/v1/openInterest", "BTCUSDT", mark_ts,
                     mark_ts, 1.0, "1", str(mark), digest, "{}", "{}"),
                )
        conn.commit()

    result = settlement_probability_report(journal.db_path)
    checkpoint = result["checkpoints"]["3600"]
    assert checkpoint["expanding_walk_forward_oos_markets"] == 4
    assert checkpoint["common_oos_metrics"]["market_midpoint"]["rows"] == 4
    assert checkpoint["common_oos_metrics"]["simple_spot_strike_realized_vol"]["rows"] == 4
    assert checkpoint["common_oos_metrics"]["external_increment_logistic"]["rows"] == 4
    assert [row["outcome_id"] for row in checkpoint["oos_predictions"]] == [9, 10, 11, 12]
    assert all(row["prior_settled_training_markets"] >= 8 for row in checkpoint["oos_predictions"])
    assert result["live_authority"] is False


def _score(*, fair_q10: str, tail: str = "0.10", hit: str = "0.80", fill: str = "0.40"):
    return {
        "available": True,
        "outputs": {
            "future_bid_900s": {"q10": fair_q10, "mean": fair_q10},
            "breach_minus_10pct_1h": tail,
            "hit_plus_1pct_1h": hit,
            "breach_minus_20pct_1h": "0.70",
            "recovered_after_minus_10pct_1h": "0.20",
        },
        "maker_fill_probability": fill,
    }


def test_b3_optimizer_can_propose_marketable_action_but_has_no_live_authority():
    decision = OutcomeActiveActionOptimizer().choose_entry((
        ActiveSideInput(0, Decimal("0.60"), Decimal("0.61"), _score(fair_q10="0.64")),
    ), regime="TREND")
    assert decision.action == "BOUNDED_MARKETABLE_BUY"
    assert decision.execution_submitted is False
    assert decision.live_authority is False
    blocked = OutcomeActiveActionOptimizer().choose_entry((
        ActiveSideInput(0, Decimal("0.60"), Decimal("0.61"), _score(fair_q10="0.64")),
    ), regime="RANGE")
    assert blocked.action == "WAIT"


def test_b4_replay_never_counts_maker_opportunity_as_fill(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    _seed_rows(journal)
    result = replay_report(journal.db_path)
    assert result["folds"]
    assert result["ready_for_live"] is False
    assert result["live_authority"] is False
    assert "maker_quote_opportunity_not_fill" in result
    assert "b6_requires_separate_operator_authorization" in result["blockers"]


def test_b5_shadow_emits_structured_action_without_execution(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    _seed_rows(journal)
    artifact = tmp_path / "active.json"
    train_report(journal.db_path, artifact_path=artifact)
    shadow = OutcomeActiveChallengerShadow(OutcomeActiveModel(artifact))
    result = shadow.evaluate_entry(
        context={
            "yes_best_bid": 0.60, "yes_best_ask": 0.61,
            "no_best_bid": 0.39, "no_best_ask": 0.40,
            "mark_return_bps": 12, "oi_return_bps": 3,
            "trend_continuation": {"mark_15m_bps": 17, "mark_60m_bps": 22},
        },
        time_left_sec=70_000,
        production_side_index=None,
        production_reason="directional_confirmation_not_met",
        regime={"state": "UNKNOWN"},
    )
    assert result["execution_submitted"] is False
    assert result["live_authority"] is False
    assert result["decision_kind"] == "entry"
    assert result["challenger"]["execution_submitted"] is False
    assert result["observed_context"]["yes_bid"] is not None
    assert isinstance(result["observed_context"]["market_session_is_weekend"], bool)


def test_b3_wait_retains_counterfactual_side_without_submitting():
    decision = OutcomeActiveActionOptimizer().choose_entry((
        ActiveSideInput(1, Decimal("0.60"), Decimal("0.61"), _score(fair_q10="0.59")),
    ), regime="TREND")
    assert decision.action == "WAIT"
    assert decision.side_index == 1
    assert decision.execution_submitted is False


def test_b5_report_keeps_counterfactual_paths_separate_from_execution(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    _seed_rows(journal)
    event_id = journal.log_strategy_event("run", "OUTCOME_ACTIVE_CHALLENGER_SHADOW", {
        "period": "1d", "outcome_id": 99, "production_reason": "no_directional_edge",
        "observed_context": {"yes_bid": 0.60, "yes_ask": 0.61, "no_bid": 0.39, "no_ask": 0.40},
        "scores": {"0": {"artifact_trained_through_ms": 1}},
        "challenger": {"action": "WAIT", "side_index": 0, "quote": None},
        "execution_submitted": False, "live_authority": False,
    })
    assert event_id is not None
    import sqlite3
    from datetime import datetime
    with sqlite3.connect(journal.db_path) as conn:
        ts = conn.execute("SELECT ts FROM strategy_events WHERE id=?", (event_id,)).fetchone()[0]
    event_ms = int(datetime.fromisoformat(ts).timestamp() * 1000)
    features = {
        "yes_bid": 0.63, "yes_ask": 0.64, "no_bid": 0.36, "no_ask": 0.37,
        "time_left_sec": 60_000, "btc_mark_return_300s_bps": 1,
        "btc_mark_return_900s_bps": 1, "btc_mark_return_3600s_bps": 1,
        "oi_return_300s_bps": 1,
    }
    assert journal.upsert_outcome_oi_feature_row(
        feature_schema_version=FEATURE_SCHEMA_VERSION, outcome_snapshot_event_id=99999,
        outcome_id=99, period="1d", snapshot_timestamp_ms=event_ms + 300_000,
        oi_observation_id=1, oi_exchange_timestamp_ms=1, oi_local_received_at_ms=1,
        oi_age_ms=1, oi_join_direction="as_of_local_received_at", oi_backfilled=False,
        features=features, labels={}, market_context={},
    )
    result = shadow_report(journal.db_path)
    assert result["entry_actions"]["WAIT"] == 1
    assert result["unseen_entry_events"] == 1
    assert result["future_path_by_horizon_sec"]["300"]["observations"] == 1
    assert result["ready_for_live"] is False
    assert result["maker_semantics"].startswith("conditional_quote")


def test_b5_report_separates_join_as_maker_from_same_time_taker_head_to_head(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    event_id = journal.log_strategy_event("run", "OUTCOME_ACTIVE_CHALLENGER_SHADOW", {
        "period": "1d", "outcome_id": 100,
        "production_side_index": 0,
        "production_reason": "directional_confirmation_met",
        "observed_context": {"yes_bid": 0.60, "yes_ask": 0.61, "no_bid": 0.39, "no_ask": 0.40},
        "scores": {"0": {"artifact_trained_through_ms": 1}},
        "challenger": {"action": "JOIN_BEST_BID", "side_index": 0, "quote": 0.60},
        "execution_submitted": False, "live_authority": False,
    })
    assert event_id is not None
    import sqlite3
    with sqlite3.connect(journal.db_path) as conn:
        ts = conn.execute("SELECT ts FROM strategy_events WHERE id=?", (event_id,)).fetchone()[0]
    event_ms = int(datetime.fromisoformat(ts).timestamp() * 1000)
    features = {
        "yes_bid": 0.63, "yes_ask": 0.64, "no_bid": 0.36, "no_ask": 0.37,
        "time_left_sec": 60_000, "btc_mark_return_300s_bps": 1,
        "btc_mark_return_900s_bps": 1, "btc_mark_return_3600s_bps": 1,
        "oi_return_300s_bps": 1,
    }
    assert journal.upsert_outcome_oi_feature_row(
        feature_schema_version=FEATURE_SCHEMA_VERSION, outcome_snapshot_event_id=100000,
        outcome_id=100, period="1d", snapshot_timestamp_ms=event_ms + 300_000,
        oi_observation_id=1, oi_exchange_timestamp_ms=1, oi_local_received_at_ms=1,
        oi_age_ms=1, oi_join_direction="as_of_local_received_at", oi_backfilled=False,
        features=features, labels={}, market_context={},
    )
    result = shadow_report(journal.db_path)
    taker = result["taker_at_observed_ask_counterfactual_by_action_by_horizon_sec"]["300"]["JOIN_BEST_BID"]
    assert taker["observations"] == 1
    assert Decimal(taker["mean_fee_adjusted_return"]) > 0
    same_side = result["same_timestamp_s0_vs_active_taker_counterfactual_by_horizon_sec"]["300"]["same_side"]
    assert same_side["production_taker"]["observations"] == 1
    assert same_side["challenger_taker"]["observations"] == 1
    assert Decimal(same_side["challenger_minus_production"]["mean_fee_adjusted_return"]) == 0
    assert result["head_to_head_semantics"].startswith("read_only_bbo_counterfactual")
