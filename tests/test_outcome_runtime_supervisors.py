from types import SimpleNamespace

from bot.outcome_live_strategy import OutcomeLiveStrategyConfig
from bot.outcome_runtime_supervisors import (
    OutcomeEntrySupervisor,
    OutcomeHoldingSupervisor,
    OutcomeResearchSupervisor,
    OutcomeRuntimeTickSnapshot,
)


def _snapshot(*, active=(), side_index=0, reduce_only=False, safe=True):
    return OutcomeRuntimeTickSnapshot(
        market=SimpleNamespace(outcome_id=7),
        report=SimpleNamespace(safe_for_new_entry=safe, reason="safe" if safe else "unsafe"),
        active=tuple(active),
        pending_owned_entry=False,
        entry_side_index=side_index,
        entry_reason="confirmed",
        reduce_only=reduce_only,
        observed_monotonic=100.0,
    )


def test_research_supervisor_is_observation_only_and_preserves_order():
    calls = []
    runtime = SimpleNamespace(
        _observe_market_regime_shadow=lambda **_: calls.append("regime") or {"state": "TREND"},
        _observe_confidence_entry_shadow=lambda **_: calls.append("confidence") or {"p": 0.7},
        _observe_active_challenger_shadow=lambda **_: calls.append("challenger") or {"action": "HOLD"},
        _capture_trend_continuation_path=lambda **_: calls.append("continuation"),
    )
    admission = {}
    OutcomeResearchSupervisor().observe_entry(
        runtime, market=SimpleNamespace(outcome_id=7), entry_side_index=0,
        entry_reason="confirmed", entry_evidence={}, market_context={}, admission=admission,
    )
    assert calls == ["regime", "confidence", "challenger", "continuation"]
    assert admission["market_regime_shadow"]["state"] == "TREND"


def test_holding_supervisor_protects_before_observation_and_short_circuits_exit():
    calls = []
    finding = SimpleNamespace(coin="#7")
    fast_result = object()
    runtime = SimpleNamespace(
        _REVERSAL_RISK_MIN_INTERVAL_SEC=5.0, _HOLDING_PATH_MIN_INTERVAL_SEC=30.0,
        holding_execution_service=SimpleNamespace(
            protect_after_fill=lambda **_: calls.append("protect") or None,
        ),
        audit_safety_for_existing_holding=lambda **_: calls.append("holding_safety_audit"),
        _observe_toxic_fill_shadow=lambda **_: calls.append("toxic"),
        _observe_holding_reversal_ws=lambda **_: calls.append("reversal") or True,
        _capture_holding_path=lambda **_: calls.append("holding"),
        holding_risk_service=SimpleNamespace(
            maybe_fast_failure=lambda **_: calls.append("fast_failure") or fast_result,
            maybe_emergency=lambda **_: calls.append("s3") or None,
        ),
        entry_execution_service=SimpleNamespace(
            manage_resting_buy=lambda **_: calls.append("entry_requote") or None,
        ),
    )
    result = OutcomeHoldingSupervisor().manage_before_stream_gate(
        runtime, snapshot=_snapshot(active=(finding,)), config=OutcomeLiveStrategyConfig(),
    )
    assert result is fast_result
    assert calls == ["protect", "holding_safety_audit", "toxic", "reversal", "holding", "fast_failure"]


def test_holding_supervisor_does_not_observe_until_protection_is_resolved():
    calls = []
    protection_result = object()
    runtime = SimpleNamespace(
        holding_execution_service=SimpleNamespace(
            protect_after_fill=lambda **_: calls.append("protect") or protection_result,
        ),
    )
    result = OutcomeHoldingSupervisor().manage_before_stream_gate(
        runtime, snapshot=_snapshot(active=(SimpleNamespace(coin="#7"),)),
        config=OutcomeLiveStrategyConfig(),
    )
    assert result is protection_result
    assert calls == ["protect"]


def test_entry_supervisor_fails_closed_before_signal_or_order_construction():
    calls = []
    runtime = SimpleNamespace(
        entry_execution_service=SimpleNamespace(
            preflight=lambda **_: calls.append("entry_service") or ("blocked", "account recovery unsafe", None),
        ),
    )
    admission = {}
    result = OutcomeEntrySupervisor().preflight(
        runtime, snapshot=_snapshot(safe=False), admission=admission,
        config=OutcomeLiveStrategyConfig(),
    )
    assert result[0] == "blocked"
    assert "account recovery" in result[1]
    assert calls == ["entry_service"]


def test_post_stream_holding_order_is_requote_then_persisted_exit():
    calls = []
    persisted = object()
    runtime = SimpleNamespace(
        exit_requote_service=SimpleNamespace(
            maybe_requote=lambda **_: calls.append("requote") or None,
        ),
        holding_execution_service=SimpleNamespace(
            advance_persisted_exit=lambda **_: calls.append("persisted") or persisted,
        ),
    )
    result = OutcomeHoldingSupervisor().manage_after_stream_gate(
        runtime, snapshot=_snapshot(active=(SimpleNamespace(coin="#7"),)),
    )
    assert result is persisted
    assert calls == ["requote", "persisted"]
