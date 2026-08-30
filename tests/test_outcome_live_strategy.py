from decimal import Decimal

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


def test_gate_fails_closed_for_stale_or_conflicting_oi(tmp_path):
    db = TradeJournalDB(tmp_path / "strategy.db")
    _oi(db, timestamp=1_000_000, oi="100", mark="100", tag="old")
    _oi(db, timestamp=1_300_000, oi="99", mark="101", tag="new")
    gate = OutcomeOiEntryGate(db.db_path, OutcomeLiveStrategyConfig(oi_max_age_sec=90))
    assert gate.evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=1_330_000).side_index is None
    assert gate.evaluate(spot_price=Decimal("101"), strike_price=Decimal("100"), now_ms=1_500_000).reason == "oi_observation_stale"


def test_live_strategy_config_has_no_daily_entry_count_parameter(monkeypatch):
    monkeypatch.setenv("OUTCOME_LIVE_STRATEGY_MAX_DAILY_ENTRIES", "0")
    assert not hasattr(OutcomeLiveStrategyConfig.from_env(), "max_daily_entries")
