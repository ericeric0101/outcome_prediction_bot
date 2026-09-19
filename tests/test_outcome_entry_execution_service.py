from types import SimpleNamespace

from bot.outcome_entry_execution_service import OutcomeEntryExecutionService
from bot.outcome_entry_requote import OutcomeEntryQuotePlanner, OutcomeEntryQuotePlannerConfig
from bot.outcome_live_strategy import OutcomeLiveStrategyConfig
from bot.outcome_runtime_types import OutcomeRuntimeTickSnapshot


class Gateway:
    def outcome_coin(self, _market, side_index):
        return "#yes" if side_index == 0 else "#no"


def snapshot(*, safe=True, active=(), side_index=0, reduce_only=False):
    return OutcomeRuntimeTickSnapshot(
        market=SimpleNamespace(outcome_id=7, yes_coin="#yes", no_coin="#no"),
        report=SimpleNamespace(safe_for_new_entry=safe, reason="unsafe"),
        active=tuple(active), pending_owned_entry=False,
        entry_side_index=side_index, entry_reason="confirmed",
        reduce_only=reduce_only, observed_monotonic=1.0,
    )


def service():
    recovery = SimpleNamespace(wallet="w", account=SimpleNamespace(get_open_orders_sync=lambda _wallet: []))
    return OutcomeEntryExecutionService(
        recovery=recovery, gateway=Gateway(), machine=SimpleNamespace(), store=None,
        planner=OutcomeEntryQuotePlanner(OutcomeEntryQuotePlannerConfig()),
    )


def test_preflight_fails_closed_before_signal_selection():
    admission = {}
    result = service().preflight(
        snapshot=snapshot(safe=False), admission=admission,
        config=OutcomeLiveStrategyConfig(),
    )
    assert result is not None and result.state == "blocked"
    assert "account recovery" in result.detail
    assert "selected_coin" not in admission


def test_preflight_separates_reduce_only_no_signal_and_selected_coin():
    result = service().preflight(
        snapshot=snapshot(reduce_only=True), admission={}, config=OutcomeLiveStrategyConfig(),
    )
    assert result is not None and result.state == "flat" and "reduce-only" in result.detail
    result = service().preflight(
        snapshot=snapshot(side_index=None), admission={}, config=OutcomeLiveStrategyConfig(),
    )
    assert result is not None and result.state == "flat" and "no entry" in result.detail
    admission = {}
    result = service().preflight(
        snapshot=snapshot(side_index=1), admission=admission, config=OutcomeLiveStrategyConfig(),
    )
    assert result is None and admission["selected_coin"] == "#no"


def test_preflight_blocks_when_durable_protective_exit_exists_but_snapshot_is_stale():
    """A locally owned SELL is sufficient to stop a second BUY race."""
    exit_store = SimpleNamespace(
        recover=lambda **_: SimpleNamespace(order_id="protective-sell", state="SELL_RESTING"),
    )
    recovery = SimpleNamespace(wallet="w", account=SimpleNamespace(get_open_orders_sync=lambda _wallet: []))
    service = OutcomeEntryExecutionService(
        recovery=recovery, gateway=Gateway(), machine=SimpleNamespace(), store=None,
        exit_store=exit_store, planner=OutcomeEntryQuotePlanner(OutcomeEntryQuotePlannerConfig()),
    )
    admission = {}
    result = service.preflight(
        snapshot=snapshot(active=()), admission=admission, config=OutcomeLiveStrategyConfig(),
    )
    assert result is not None and result.state == "blocked"
    assert result.order_id == "protective-sell"
    assert admission["owned_exit_lifecycle_fence"]["state"] == "SELL_RESTING"


def test_preflight_refuses_decision_that_started_before_owned_exit_fill():
    exit_store = SimpleNamespace(
        recover=lambda **_: None,
        latest_owned_sell_fill_at_ms=lambda **_: (200, "completed-sell"),
    )
    recovery = SimpleNamespace(wallet="w", account=SimpleNamespace(get_open_orders_sync=lambda _wallet: []))
    service = OutcomeEntryExecutionService(
        recovery=recovery, gateway=Gateway(), machine=SimpleNamespace(), store=None,
        exit_store=exit_store, planner=OutcomeEntryQuotePlanner(OutcomeEntryQuotePlannerConfig()),
    )
    old_decision = OutcomeRuntimeTickSnapshot(
        **{**snapshot().__dict__, "entry_decision_at_ms": 199},
    )
    admission = {}
    result = service.preflight(snapshot=old_decision, admission=admission, config=OutcomeLiveStrategyConfig())
    assert result is not None and result.state == "flat"
    assert result.order_id == "completed-sell"
    assert admission["post_exit_decision_fence"]["owned_exit_filled_at_ms"] == 200
