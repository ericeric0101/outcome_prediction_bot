from decimal import Decimal

from bot.outcome_reversal import OutcomeReversalClassifier, OutcomeReversalInput, OutcomeReversalState


def _item(**changes):
    value = dict(
        side_index=0, fill_vwap=Decimal("0.70"), best_bid=Decimal("0.68"), best_ask=Decimal("0.69"),
        spot_strike_bps=Decimal("-20"), mark_return_bps=Decimal("-8"), oi_return_bps=Decimal("4"),
        book_healthy=True, consecutive_opposite_observations=3,
    )
    value.update(changes)
    return OutcomeReversalInput(**value)


def test_classifier_never_infers_reversal_from_missing_asof_inputs():
    result = OutcomeReversalClassifier().classify(_item(mark_return_bps=None))
    assert result.state is OutcomeReversalState.UNKNOWN


def test_classifier_requires_persistent_opposite_confirmation():
    assert OutcomeReversalClassifier().classify(_item(consecutive_opposite_observations=2)).state is OutcomeReversalState.WEAKENING
    assert OutcomeReversalClassifier().classify(_item()).state is OutcomeReversalState.REVERSAL_CONFIRMED


def test_classifier_holds_when_direction_and_book_are_healthy():
    result = OutcomeReversalClassifier().classify(_item(
        best_bid=Decimal("0.72"), best_ask=Decimal("0.73"), spot_strike_bps=Decimal("20"),
        mark_return_bps=Decimal("8"), oi_return_bps=Decimal("4"),
    ))
    assert result.state is OutcomeReversalState.HOLD
