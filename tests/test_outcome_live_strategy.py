from decimal import Decimal
import sqlite3

from bot.outcome_live_strategy import OutcomeLiveStrategyConfig, OutcomeOiEntryGate
from monitoring.trade_journal_db import TradeJournalDB


def _oi(db, *, timestamp: int, oi: str, mark: str, tag: str) -> None:
    assert db.record_binance_oi_observation(
        run_id="oi", source="binance_usdm_public", endpoint="/fapi/v1/openInterest", symbol="BTCUSDT",
        exchange_timestamp_ms=timestamp - 5, local_received_at_ms=timestamp, request_latency_ms=1,
        open_interest=oi, mark_price=mark, raw_payload_hash=tag, raw_payload={}, backfilled=False,
    )


def test_gate_requires_aligned_spot_mark_and_oi_for_up(tmp_path):
    db = TradeJournalDB(tmp_path / "strategy.db")
    _oi(db, timestamp=1_000_000, oi="100", mark="100", tag="old")
    _oi(db, timestamp=1_300_000, oi="101", mark="101", tag="new")
    gate = OutcomeOiEntryGate(db.db_path, OutcomeLiveStrategyConfig(oi_max_age_sec=90))
    decision = gate.evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=1_330_000)
    assert decision.side_index == 0
    assert decision.reason == "up_spot_mark_oi_confirmed"
    assert decision.evidence["gate_variants"] == {
        "spot_mark_oi": {"eligible": True, "side_index": 0},
        "spot_mark": {"eligible": True, "side_index": 0},
        "spot_mark_or_oi": {"eligible": True, "side_index": 0},
    }


def test_gate_fails_closed_for_stale_or_conflicting_oi(tmp_path):
    db = TradeJournalDB(tmp_path / "strategy.db")
    _oi(db, timestamp=1_000_000, oi="100", mark="100", tag="old")
    _oi(db, timestamp=1_300_000, oi="99", mark="101", tag="new")
    gate = OutcomeOiEntryGate(db.db_path, OutcomeLiveStrategyConfig(oi_max_age_sec=90))
    assert gate.evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=1_330_000).side_index is None
    assert gate.evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=1_500_000).reason == "oi_observation_stale"


def test_gate_persists_nonexecuting_ablation_variants_without_relaxing_s0(tmp_path):
    db = TradeJournalDB(tmp_path / "strategy.db")
    _oi(db, timestamp=1_000_000, oi="100", mark="100", tag="old")
    # Spot and mark point UP, but OI falls: S0 stays blocked while the
    # spot+mark counterfactual is visible for the later report.
    _oi(db, timestamp=1_300_000, oi="99", mark="101", tag="new")
    gate = OutcomeOiEntryGate(db.db_path, OutcomeLiveStrategyConfig(oi_max_age_sec=90))
    decision = gate.evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=1_330_000)
    assert decision.side_index is None
    assert decision.evidence["gate_variants"]["spot_mark"] == {"eligible": True, "side_index": 0}
    assert decision.evidence["gate_variants"]["spot_mark_oi"] == {"eligible": False, "side_index": None}


def test_tier_b_uses_fresh_spot_mark_but_not_oi_direction_when_enabled(tmp_path):
    db = TradeJournalDB(tmp_path / "strategy.db")
    _oi(db, timestamp=1_000_000, oi="100", mark="100", tag="old")
    # OI falls while spot and mark both confirm UP.  Baseline stays blocked;
    # the explicit Tier-B experiment may select UP without treating falling
    # OI as a direction signal.
    _oi(db, timestamp=1_300_000, oi="99", mark="101", tag="new")
    gate = OutcomeOiEntryGate(
        db.db_path,
        OutcomeLiveStrategyConfig(oi_max_age_sec=90, tier_b_enabled=True),
    )
    decision = gate.evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=1_330_000)
    assert decision.side_index == 0
    assert decision.reason == "up_spot_mark_tier_b_confirmed"
    assert decision.evidence["entry_tier"] == "tier_b_spot_mark"
    assert decision.evidence["tier_b_enabled"] is True


def test_tier_b_remains_off_by_default(tmp_path):
    db = TradeJournalDB(tmp_path / "strategy.db")
    _oi(db, timestamp=1_000_000, oi="100", mark="100", tag="old")
    _oi(db, timestamp=1_300_000, oi="99", mark="101", tag="new")
    decision = OutcomeOiEntryGate(
        db.db_path, OutcomeLiveStrategyConfig(oi_max_age_sec=90),
    ).evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=1_330_000)
    assert decision.side_index is None
    assert decision.evidence["tier_b_enabled"] is False


def test_live_strategy_config_has_no_daily_entry_count_parameter(monkeypatch):
    monkeypatch.setenv("OUTCOME_LIVE_STRATEGY_MAX_DAILY_ENTRIES", "0")
    assert not hasattr(OutcomeLiveStrategyConfig.from_env(), "max_daily_entries")


def test_live_oi_query_has_local_time_index_without_temp_sort(tmp_path):
    db = TradeJournalDB(tmp_path / "strategy.db")
    _oi(db, timestamp=1_000_000, oi="100", mark="100", tag="old")
    _oi(db, timestamp=1_300_000, oi="101", mark="101", tag="new")
    with sqlite3.connect(db.db_path) as conn:
        plan = conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT id, local_received_at_ms, open_interest, mark_price
               FROM binance_oi_observations
               WHERE symbol='BTCUSDT' AND backfilled=0 AND local_received_at_ms <= ?
               ORDER BY local_received_at_ms DESC LIMIT 250""",
            (2_000_000,),
        ).fetchall()
    detail = " ".join(str(row[-1]) for row in plan)
    assert "idx_binance_oi_symbol_backfilled_local_time" in detail
    assert "TEMP B-TREE" not in detail


def _trend_rows(db) -> None:
    # 66 minutes of real-cadence observations: a long UP move, followed by a
    # 5m pullback that remains smaller than the normal 5m movement scale.
    for second in range(0, 66 * 60 + 1, 30):
        if second <= 60 * 60:
            mark = Decimal("100") + Decimal(second) / Decimal("360")
        else:
            mark = Decimal("110") - Decimal(second - 60 * 60) / Decimal("1000")
        _oi(
            db, timestamp=10_000_000 + second * 1000,
            oi=str(Decimal("100") + Decimal(second) / Decimal("100000")), mark=str(mark), tag=f"trend-{second}",
        )


def test_trend_continuation_is_journalled_but_disabled_by_default(tmp_path):
    db = TradeJournalDB(tmp_path / "strategy.db")
    _trend_rows(db)
    config = OutcomeLiveStrategyConfig(oi_max_age_sec=90, trend_continuation_enabled=False)
    gate = OutcomeOiEntryGate(db.db_path, config)
    # Establish the required 15-minute persistent spot state first.
    gate.evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=10_000_000 + 50 * 60 * 1000)
    decision = gate.evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=10_000_000 + 65 * 60 * 1000)
    candidate = decision.evidence["trend_continuation"]
    assert decision.side_index is None
    assert candidate["eligible"] is True
    assert candidate["side_index"] == 0
    assert candidate["mark_15m_bps"] is not None
    assert candidate["mark_60m_bps"] is not None


def test_trend_continuation_canary_selects_only_a_persistent_small_pullback(tmp_path):
    db = TradeJournalDB(tmp_path / "strategy.db")
    _trend_rows(db)
    gate = OutcomeOiEntryGate(
        db.db_path, OutcomeLiveStrategyConfig(oi_max_age_sec=90, trend_continuation_enabled=True),
    )
    gate.evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=10_000_000 + 50 * 60 * 1000)
    decision = gate.evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=10_000_000 + 65 * 60 * 1000)
    assert decision.side_index == 0
    assert decision.reason == "up_trend_continuation_confirmed"
    assert decision.evidence["entry_tier"] == "tier_c_trend_continuation"
