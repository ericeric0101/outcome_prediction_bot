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
