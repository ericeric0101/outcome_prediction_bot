from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_exit_quote_planner import ExitQuoteAction, ExitQuotePlan
from bot.outcome_exit_requote_service import OutcomeExitRequoteService


class _Journal:
    def __init__(self):
        self.events = []

    def log_strategy_event(self, run_id, event_type, payload):
        self.events.append((run_id, event_type, payload))


class _Store:
    run_id = "run"

    def __init__(self):
        self.journal = _Journal()


class _Lifecycle:
    order_id = "oid"
    coin = "#1230"
    target_price = Decimal("0.60")
    inventory = Decimal("10")


def test_modify_order_shadow_records_price_requeue_but_has_no_execution_authority():
    service = object.__new__(OutcomeExitRequoteService)
    service.store = _Store()
    service._last_modify_shadow = {}
    market = OutcomeMarketSpec(
        outcome_id=123, coin_name="@123", yes_coin="#1230", no_coin="#1231",
        yes_asset_id=1, no_asset_id=2, market_class="priceBinary", underlying="BTC",
        expiry_str="20260918-0000", expiry_timestamp=1, start_timestamp=0,
        target_price=Decimal("1"), period="1d", raw_spec="test",
    )
    plan = ExitQuotePlan(ExitQuoteAction.CANCEL_REPLACE, "target_reprice", Decimal("0.61"))
    service._record_modify_order_shadow(
        market=market, lifecycle=_Lifecycle(), plan=plan, inventory=Decimal("10"),
        bid=Decimal("0.59"), ask=Decimal("0.60"),
    )
    _, event_type, payload = service.store.journal.events[0]
    assert event_type == "OUTCOME_MODIFY_ORDER_SHADOW"
    assert payload["price_change_requeues_expected"] is True
    assert payload["live_authority"] is False
