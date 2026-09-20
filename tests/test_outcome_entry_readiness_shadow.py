from decimal import Decimal

from bot.outcome_entry_readiness_shadow import EntryReadinessConfig, OutcomeEntryReadinessShadow


def _book(bid: str, ask: str = "0.99", depth: str = "1200") -> dict[str, object]:
    per_level = str(Decimal(depth) / 3)
    return {"levels": [
        [{"px": bid, "sz": per_level} for _ in range(3)],
        [{"px": ask, "sz": "100"}],
    ]}


def _populate(observer: OutcomeEntryReadinessShadow, *, now: float = 1000.0) -> None:
    # 10-second samples supply all four -30/-20/-10/current snapshots.
    for timestamp in range(840, 1001, 10):
        elapsed = Decimal(timestamp - 840) / Decimal("10000")
        observer.observe_l2(coin="#yes", timestamp=timestamp, payload=_book(str(Decimal("0.50") + elapsed), "0.95"))
        observer.observe_l2(coin="#no", timestamp=timestamp, payload=_book("0.40", "0.95"))
        observer.observe_btc_mid(timestamp=timestamp, price=str(Decimal("80000") + Decimal(timestamp - 840) * 2))
    # The market-level stream has a stable baseline and a larger recent rate.
    rows = [{"timestamp": timestamp, "tid": f"baseline-{timestamp}"} for timestamp in range(850, 970, 10)]
    rows += [{"timestamp": timestamp, "tid": f"recent-{timestamp}"} for timestamp in range(971, 1001)]
    observer.observe_trades(items=rows, received_at=now)


def test_missing_evidence_fails_closed_without_fake_readiness():
    result = OutcomeEntryReadinessShadow().evaluate(
        outcome_id=1, period="1d", side_index=0, yes_coin="#yes", no_coin="#no", now=1000,
    )
    assert result["candidate"] is False
    assert result["persistent_candidate"] is False
    assert result["live_authority"] is False
    assert result["promotion_boundary"]["may_submit_order"] is False


def test_market_level_trade_dedup_rejects_mirrored_side_records():
    observer = OutcomeEntryReadinessShadow()
    observer.observe_trades(items=[
        {"timestamp": 1000, "tid": "economic-trade", "coin": "#yes"},
        {"timestamp": 1000, "tid": "economic-trade", "coin": "#no"},
    ], received_at=1000)
    assert len(observer._market_trades) == 1


def test_ready_requires_three_of_four_features_and_pre30_persistence():
    config = EntryReadinessConfig(
        bilateral_min_depth=Decimal("1"), btc_favorable_velocity_30s_bps=Decimal("0"),
        participation_accel_30v120=Decimal("0"), cross_side_confirmation_30s_bps=Decimal("999999"),
    )
    observer = OutcomeEntryReadinessShadow(config)
    _populate(observer)
    result = observer.evaluate(outcome_id=1, period="1d", side_index=0, yes_coin="#yes", no_coin="#no", now=1000)
    assert result["current"]["complete"] is True
    assert result["current"]["votes"] == 3
    assert result["candidate"] is True
    assert result["persistent_candidate"] is True
    assert result["state"] == "ENTRY_READINESS_PERSISTENT_SHADOW"


def test_two_votes_are_not_ready_even_when_evidence_is_complete():
    config = EntryReadinessConfig(
        bilateral_min_depth=Decimal("1"), btc_favorable_velocity_30s_bps=Decimal("999999"),
        participation_accel_30v120=Decimal("0"), cross_side_confirmation_30s_bps=Decimal("999999"),
    )
    observer = OutcomeEntryReadinessShadow(config)
    _populate(observer)
    result = observer.evaluate(outcome_id=1, period="1d", side_index=0, yes_coin="#yes", no_coin="#no", now=1000)
    assert result["current"]["complete"] is True
    assert result["current"]["votes"] == 2
    assert result["candidate"] is False


def test_no_side_specific_trade_mirroring_is_needed_for_no_normalization():
    config = EntryReadinessConfig(
        bilateral_min_depth=Decimal("1"), btc_favorable_velocity_30s_bps=Decimal("-999999"),
        participation_accel_30v120=Decimal("0"), cross_side_confirmation_30s_bps=Decimal("-999999"),
    )
    observer = OutcomeEntryReadinessShadow(config)
    _populate(observer)
    result = observer.evaluate(outcome_id=1, period="1d", side_index=1, yes_coin="#yes", no_coin="#no", now=1000)
    assert result["held_coin"] == "#no"
    assert result["opposite_coin"] == "#yes"
    assert result["current"]["complete"] is True


def test_module_has_no_execution_dependency():
    import inspect
    import bot.outcome_entry_readiness_shadow as module

    source = inspect.getsource(module)
    for forbidden in ("OutcomeExecutionGateway", "place_alo(", "cancel_and_confirm(", "EmergencyExit"):
        assert forbidden not in source
