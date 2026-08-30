from decimal import Decimal

from bot.outcome_exit_quote_planner import ExitQuoteAction, ExitQuoteInput, OutcomeExitQuotePlanner, OutcomeExitQuotePlannerConfig


def _input(**changes):
    values = dict(inventory=Decimal("13"), fill_vwap=Decimal("0.80"), maker_close_fee_rate=Decimal("0.0004"),
                  minimum_return_pct=Decimal("0.05"), loss_reprice_pct=Decimal("0.05"), existing_order_id="old-1",
                  existing_price=Decimal("0.84034"), best_bid=Decimal("0.79"), best_ask=Decimal("0.80"),
                  book_age_sec=1.0, now_ts=1000.0)
    values.update(changes)
    return ExitQuoteInput(**values)


def test_planner_keeps_matching_fee_adjusted_target():
    plan = OutcomeExitQuotePlanner().plan(_input())
    assert plan.action is ExitQuoteAction.KEEP
    assert plan.reason == "requote_hysteresis"


def test_planner_proposes_passive_loss_band_replacement():
    plan = OutcomeExitQuotePlanner().plan(_input(best_bid=Decimal("0.70"), best_ask=Decimal("0.71"), existing_price=Decimal("0.85")))
    assert plan.action is ExitQuoteAction.CANCEL_REPLACE
    assert plan.exit_mode == "loss_band"
    assert plan.target_price > Decimal("0.70")
    assert plan.target_price >= plan.floor_price


def test_zero_loss_policy_only_reprices_to_fee_inclusive_break_even():
    plan = OutcomeExitQuotePlanner().plan(_input(
        loss_reprice_pct=Decimal("0"), best_bid=Decimal("0.70"), best_ask=Decimal("0.71"), existing_price=Decimal("0.85"),
    ))
    break_even = Decimal("0.80") / Decimal("0.9996")
    assert plan.action is ExitQuoteAction.CANCEL_REPLACE
    assert plan.exit_mode == "loss_band"
    assert plan.target_price >= break_even


def test_planner_blocks_missing_policy_and_stale_book():
    assert OutcomeExitQuotePlanner().plan(_input(minimum_return_pct=None)).reason == "missing_verified_exit_policy"
    assert OutcomeExitQuotePlanner().plan(_input(book_age_sec=16)).reason == "stale_book"


def test_planner_enforces_interval_and_attempt_cap():
    config = OutcomeExitQuotePlannerConfig(min_requote_interval_sec=60, max_replacements=1)
    planner = OutcomeExitQuotePlanner(config)
    assert planner.plan(_input(last_requote_ts=990, existing_price=Decimal("0.90"))).reason == "requote_interval_not_elapsed"
    assert planner.plan(_input(replacement_count=1, existing_price=Decimal("0.90"))).reason == "replacement_attempt_cap"


def test_default_planner_has_no_arbitrary_replacement_quota():
    plan = OutcomeExitQuotePlanner().plan(_input(replacement_count=999, existing_price=Decimal("0.90")))
    assert plan.action is ExitQuoteAction.CANCEL_REPLACE


def test_planner_never_proposes_a_crossing_sell():
    plan = OutcomeExitQuotePlanner(OutcomeExitQuotePlannerConfig(tick_size=Decimal("0.01"))).plan(
        _input(best_bid=Decimal("0.999"), best_ask=Decimal("0.9995"), existing_price=Decimal("0.90"))
    )
    assert plan.action is ExitQuoteAction.BLOCK
    assert plan.reason == "target_outside_outcome_bounds"
