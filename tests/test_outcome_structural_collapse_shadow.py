from decimal import Decimal

from bot.outcome_structural_collapse_shadow import OutcomeStructuralCollapseShadow


def _book(mid="0.50", depth="300", spread_bps=100):
    mid = Decimal(mid); half = mid * Decimal(str(spread_bps)) / Decimal("20000")
    return {"levels": [
        [{"px": str(mid - half), "sz": str(Decimal(depth) / 3)} for _ in range(3)],
        [{"px": str(mid + half), "sz": "100"}],
    ]}


def _natural_watch(observer, coin="yes", start=0.0):
    # Peak .80 -> initial .69 -> deeper trough .50 -> .62 recovery -> .53
    # renewed decline.  A final bucket closes the .53 bar without lookahead.
    for index, mid in enumerate(("0.80", "0.69", "0.50", "0.62", "0.53", "0.52")):
        observer.observe_l2(coin=coin, timestamp=start + index * 300 + 299, payload=_book(mid=mid))
    return start + 1_500


def _books(observer, coin, lo, hi, *, depth="300", spread=100):
    for timestamp in range(int(lo + 30), int(hi + 1), 30):
        observer.observe_l2(coin=coin, timestamp=float(timestamp), payload=_book(depth=depth, spread_bps=spread))


def _trades(observer, coin, lo, hi, every, prefix):
    for index, timestamp in enumerate(range(int(lo + every), int(hi + 1), every)):
        observer.observe_trades(coin=coin, received_at=timestamp, items=[{"timestamp": timestamp, "tid": f"{prefix}-{index}"}])


def _evaluate(observer, now, *, entry=0.0):
    return observer.evaluate(lifecycle_id="life", outcome_id=1, period="1d", held_coin="yes", yes_coin="yes", no_coin="no", now=now, entry_filled_at=entry, position_size=Decimal("15"), entry_price=Decimal("0.6"))


def test_depth_failure_candidate_is_strictly_read_only():
    observer = OutcomeStructuralCollapseShadow(); trigger = _natural_watch(observer)
    _books(observer, "yes", trigger - 300, trigger, depth="300")
    _books(observer, "yes", trigger, trigger + 900, depth="120")
    result = _evaluate(observer, trigger + 901)
    assert result["branch_a"]["eligible"] is True
    assert result["candidate_branches"] == ["A_DEPTH_FAILURE"]
    assert result["read_only"] is True and result["live_authority"] is False and result["execution_submitted"] is False
    assert result["promotion_boundary"]["shadow_only"] is True
    assert all(result["promotion_boundary"][key] is False for key in (
        "may_submit_order", "may_cancel_order", "may_replace_order", "may_veto_existing_safety_lane",
    ))


def test_depth_failure_does_not_trigger_when_retention_is_above_threshold():
    observer = OutcomeStructuralCollapseShadow(); trigger = _natural_watch(observer)
    _books(observer, "yes", trigger - 300, trigger, depth="300")
    _books(observer, "yes", trigger, trigger + 900, depth="240")
    assert _evaluate(observer, trigger + 901)["branch_a"]["eligible"] is False


def test_participation_branch_requires_both_baselines_and_both_spreads():
    observer = OutcomeStructuralCollapseShadow(); trigger = _natural_watch(observer)
    for coin, prefix in (("yes", "y"), ("no", "n")):
        _trades(observer, coin, trigger - 900, trigger, 15, prefix)
        _trades(observer, coin, trigger, trigger + 300, 120, prefix + "-post")
        _books(observer, coin, trigger - 900, trigger, spread=100)
        _books(observer, coin, trigger, trigger + 300, spread=250)
    result = _evaluate(observer, trigger + 301)
    assert result["branch_b"]["windows"]["early"]["eligible"] is True
    assert "B_EARLY_PARTICIPATION_FAILURE" in result["candidate_branches"]
    missing = OutcomeStructuralCollapseShadow(); trigger = _natural_watch(missing)
    _trades(missing, "yes", trigger - 900, trigger, 15, "y"); _books(missing, "yes", trigger - 900, trigger + 300, spread=250); _books(missing, "no", trigger - 900, trigger + 300, spread=250)
    assert _evaluate(missing, trigger + 301)["branch_b"]["windows"]["early"]["reason"] == "insufficient_bilateral_evidence"


def test_mirrored_trade_rows_deduplicate_without_using_side():
    observer = OutcomeStructuralCollapseShadow()
    observer.observe_trades(coin="yes", received_at=1_000, items=[
        {"timestamp": 1_000, "tid": "same", "side": "A"}, {"timestamp": 1_000, "tid": "same", "side": "B"},
    ])
    assert len(observer._trades["yes"]) == 1


def test_pre_entry_watch_does_not_leak_into_later_lifecycle():
    observer = OutcomeStructuralCollapseShadow(); trigger = _natural_watch(observer)
    result = _evaluate(observer, trigger + 901, entry=trigger + 1)
    assert result["failed_recovery"]["active"] is False


def test_natural_failed_recovery_updates_deepest_trough_and_late_branch_b():
    observer = OutcomeStructuralCollapseShadow(); trigger = _natural_watch(observer)
    # The natural path's .50, not first .69, must define recovery threshold.
    watched = _evaluate(observer, trigger + 1)
    assert watched["failed_recovery"]["active"] is True
    assert Decimal(watched["failed_recovery"]["trough"]) == Decimal("0.50")
    for coin, prefix in (("yes", "y"), ("no", "n")):
        _trades(observer, coin, trigger - 900, trigger, 15, prefix)
        _trades(observer, coin, trigger + 300, trigger + 900, 120, prefix + "late")
        _books(observer, coin, trigger - 900, trigger, spread=100)
        _books(observer, coin, trigger + 300, trigger + 900, spread=250)
    assert "B_LATE_PARTICIPATION_FAILURE" in _evaluate(observer, trigger + 901)["candidate_branches"]


def test_partial_coverage_cannot_form_candidate_and_exitability_is_fresh_only():
    observer = OutcomeStructuralCollapseShadow(); trigger = _natural_watch(observer)
    _books(observer, "yes", trigger - 30, trigger, depth="300")
    _books(observer, "yes", trigger + 870, trigger + 900, depth="120")
    result = _evaluate(observer, trigger + 901)
    assert result["branch_a"]["reason"] == "insufficient_depth_coverage"
    observer.observe_exitability(lifecycle_id="life", timestamp=trigger + 880, inventory=Decimal("15"), executable_vwap=Decimal("0.40"), taker_fee_rate=Decimal("0.001"))
    fresh = _evaluate(observer, trigger + 901)["fresh_full_depth_exitability"]
    assert fresh["full_inventory_executable"] is True and fresh["fresh"] is True and fresh["sell_vwap"] == "0.40"
    assert _evaluate(observer, trigger + 1_000)["fresh_full_depth_exitability"]["fresh"] is False
