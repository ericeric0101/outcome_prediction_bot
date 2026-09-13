from decimal import Decimal

from bot.outcome_stress_exitability import OutcomeStressExitabilityPolicy, OutcomeStressExitabilitySizer


def test_disabled_stress_sizer_never_changes_size():
    result = OutcomeStressExitabilitySizer(OutcomeStressExitabilityPolicy(enabled=False)).evaluate(
        bid_levels=(), desired_shares=Decimal("20"), venue_minimum_shares=Decimal("10"))
    assert result.allowed and result.stress_safe_shares == Decimal("20")


def test_enabled_stress_sizer_only_reduces_and_fails_below_minimum():
    sizer = OutcomeStressExitabilitySizer(OutcomeStressExitabilityPolicy(enabled=True, depth_haircut_10pct=Decimal(".6"), depth_haircut_15pct=Decimal(".45")))
    reduced = sizer.evaluate(bid_levels=({"size": "30"},), desired_shares=Decimal("20"), venue_minimum_shares=Decimal("10"))
    assert reduced.allowed and reduced.stress_safe_shares == Decimal("13")
    blocked = sizer.evaluate(bid_levels=({"size": "10"},), desired_shares=Decimal("20"), venue_minimum_shares=Decimal("10"))
    assert not blocked.allowed and blocked.stress_safe_shares < Decimal("10")
