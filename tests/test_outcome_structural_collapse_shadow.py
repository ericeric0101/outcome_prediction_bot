from decimal import Decimal

from bot.outcome_structural_collapse_shadow import OutcomeStructuralCollapseShadow


def _book(mid="0.50", depth="300", spread_bps=100):
    mid = Decimal(mid); half = mid * Decimal(str(spread_bps)) / Decimal("20000")
    return {"levels": [
        [{"px": str(mid - half), "sz": str(Decimal(depth) / 3)} for _ in range(3)],
        [{"px": str(mid + half), "sz": "100"}],
    ]}


def _watch(observer, coin="yes", trigger=1_000.0):
    observer._watch[coin] = {"trigger_ts": trigger, "trigger_price": Decimal("0.45"), "peak": Decimal("0.60"), "trough": Decimal("0.48"), "recovery_high": Decimal("0.54")}


def _books(observer, coin, lo, hi, *, depth="300", spread=100):
    for timestamp in range(int(lo + 30), int(hi + 1), 30):
        observer.observe_l2(coin=coin, timestamp=float(timestamp), payload=_book(depth=depth, spread_bps=spread))


def _trades(observer, coin, lo, hi, every, prefix):
    for index, timestamp in enumerate(range(int(lo + every), int(hi + 1), every)):
        observer.observe_trades(coin=coin, received_at=timestamp, items=[{"timestamp": timestamp, "tid": f"{prefix}-{index}"}])


def _evaluate(observer, now):
    return observer.evaluate(lifecycle_id="life", outcome_id=1, period="1d", held_coin="yes", yes_coin="yes", no_coin="no", now=now, position_size=Decimal("15"), entry_price=Decimal("0.6"))


def test_depth_failure_candidate_is_strictly_read_only():
    observer = OutcomeStructuralCollapseShadow(); _watch(observer)
    _books(observer, "yes", 700, 1_000, depth="300")
    _books(observer, "yes", 1_000, 1_900, depth="120")
    result = _evaluate(observer, 1_901)
    assert result["branch_a"]["eligible"] is True
    assert result["candidate_branches"] == ["A_DEPTH_FAILURE"]
    assert result["read_only"] is True and result["live_authority"] is False and result["execution_submitted"] is False
    assert result["promotion_boundary"]["shadow_only"] is True
    assert all(result["promotion_boundary"][key] is False for key in (
        "may_submit_order", "may_cancel_order", "may_replace_order", "may_veto_existing_safety_lane",
    ))


def test_depth_failure_does_not_trigger_when_retention_is_above_threshold():
    observer = OutcomeStructuralCollapseShadow(); _watch(observer)
    _books(observer, "yes", 700, 1_000, depth="300")
    _books(observer, "yes", 1_000, 1_900, depth="240")
    assert _evaluate(observer, 1_901)["branch_a"]["eligible"] is False


def test_participation_branch_requires_both_baselines_and_both_spreads():
    observer = OutcomeStructuralCollapseShadow(); _watch(observer)
    for coin, prefix in (("yes", "y"), ("no", "n")):
        _trades(observer, coin, 100, 1_000, 15, prefix)
        _trades(observer, coin, 1_000, 1_300, 120, prefix + "-post")
        _books(observer, coin, 1_000, 1_300, spread=250)
    result = _evaluate(observer, 1_301)
    assert result["branch_b"]["windows"]["early"]["eligible"] is True
    assert "B_EARLY_PARTICIPATION_FAILURE" in result["candidate_branches"]
    missing = OutcomeStructuralCollapseShadow(); _watch(missing)
    _trades(missing, "yes", 100, 1_000, 15, "y"); _books(missing, "yes", 1_000, 1_300, spread=250); _books(missing, "no", 1_000, 1_300, spread=250)
    assert _evaluate(missing, 1_301)["branch_b"]["windows"]["early"]["reason"] == "insufficient_bilateral_evidence"


def test_mirrored_trade_rows_deduplicate_without_using_side():
    observer = OutcomeStructuralCollapseShadow()
    observer.observe_trades(coin="yes", received_at=1_000, items=[
        {"timestamp": 1_000, "tid": "same", "side": "A"}, {"timestamp": 1_000, "tid": "same", "side": "B"},
    ])
    assert len(observer._trades["yes"]) == 1
