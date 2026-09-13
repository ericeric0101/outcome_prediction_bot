from decimal import Decimal

from bot.outcome_stress_exitability import OutcomeStressExitabilityPolicy, OutcomeStressExitabilitySizer


def test_disabled_stress_sizer_never_changes_size():
    result = OutcomeStressExitabilitySizer(OutcomeStressExitabilityPolicy(enabled=False)).evaluate(
        bid_levels=(), desired_shares=Decimal("20"), venue_minimum_shares=Decimal("10"))
    assert result.allowed and result.stress_safe_shares == Decimal("20")


def test_price_capped_stress_walk_excludes_deep_bids_below_loss_floor():
    sizer = OutcomeStressExitabilitySizer(OutcomeStressExitabilityPolicy(
        enabled=True, depth_haircut_10pct=Decimal("1"), depth_haircut_15pct=Decimal("1"),
    ))
    # At entry .90, -15% floor is .765: the 1,000 shares at .60 must not
    # masquerade as exitability for a 20-share proposed entry.
    result = sizer.evaluate(
        bid_levels=(
            {"price": ".89", "size": "2"}, {"price": ".80", "size": "8"},
            {"price": ".60", "size": "1000"},
        ), desired_shares=Decimal("20"), venue_minimum_shares=Decimal("10"), entry_price=Decimal(".90"),
    )
    assert not result.allowed
    assert result.stress_safe_shares == Decimal("2")
    assert result.audit["full_depth_shares_at_15pct_floor"] == "10"


def test_price_capped_stress_sizer_fails_below_venue_minimum_and_never_increases():
    sizer = OutcomeStressExitabilitySizer(OutcomeStressExitabilityPolicy(enabled=True, depth_haircut_10pct=Decimal(".6"), depth_haircut_15pct=Decimal(".45")))
    blocked = sizer.evaluate(
        bid_levels=({"price": ".80", "size": "10"},), desired_shares=Decimal("20"),
        venue_minimum_shares=Decimal("10"), entry_price=Decimal(".90"),
    )
    assert not blocked.allowed and blocked.stress_safe_shares < Decimal("10")


def test_invalid_or_missing_depth_fails_closed_when_stress_gate_enabled():
    sizer = OutcomeStressExitabilitySizer(OutcomeStressExitabilityPolicy(enabled=True))
    result = sizer.evaluate(
        bid_levels=None, desired_shares=Decimal("20"), venue_minimum_shares=Decimal("10"), entry_price=Decimal(".90"),
    )
    assert not result.allowed
    assert result.stress_safe_shares == Decimal("0")
