from decimal import Decimal
from types import SimpleNamespace

from bot.outcome_entry_execution_service import OutcomeEntryExecutionService
from bot.outcome_entry_lifecycle import OutcomeEntryLifecycleStore
from bot.outcome_entry_requote import OutcomeEntryQuotePlanner, OutcomeEntryQuotePlannerConfig, OutcomeEntryRequoteController
from bot.outcome_live_strategy import OutcomeLiveStrategyConfig
from bot.outcome_runtime_types import OutcomeRuntimeTickSnapshot
from monitoring.trade_journal_db import TradeJournalDB


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


class StaleAccount:
    def __init__(self):
        self.orders = [{"coin": "#yes", "side": "B", "oid": "buy-1", "limitPx": "0.60", "sz": "16"}]

    def get_open_orders_sync(self, _wallet): return list(self.orders)
    def get_spot_clearinghouse_state_sync(self, _wallet): return {"balances": []}


class StaleGateway(Gateway):
    def __init__(self, account): self.account, self.cancel_calls = account, []
    def cancel_owned_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        self.account.orders = []
        return {}


def _stale_service(tmp_path):
    journal = TradeJournalDB(tmp_path / "stale-entry.db")
    store = OutcomeEntryLifecycleStore(journal, "run")
    journal.log_order_event("run", "ORDER_SUBMIT", venue_order_id="buy-1", side="BUY", status="RESTING", instrument_id="#yes", payload={
        "venue": "hyperliquid_outcome", "outcome_id": 7, "coin": "#yes",
        "audit": {"entry_policy_schema_version": 1, "entry_policy_kind": "s0_oi_spot_mark_confirmation",
                  "entry_bid_at_decision": "0.60", "target_decision_at_ms": 100},
    })
    account = StaleAccount(); gateway = StaleGateway(account)
    lifecycle = store.recover_or_adopt_audited_submit(wallet="w", outcome_id=7, coin="#yes", open_orders=account.orders)
    assert lifecycle is not None
    service = OutcomeEntryExecutionService(
        recovery=SimpleNamespace(wallet="w", account=account), gateway=gateway, machine=SimpleNamespace(), store=store,
        planner=OutcomeEntryQuotePlanner(OutcomeEntryQuotePlannerConfig()),
        controller=OutcomeEntryRequoteController(account=account, gateway=gateway, store=store, wallet="w"),
    )
    return service, store, account, gateway, lifecycle


def test_stale_zero_fill_60s_cancels_expires_and_requires_rearm(monkeypatch, tmp_path):
    service, store, _account, gateway, lifecycle = _stale_service(tmp_path)
    monkeypatch.setattr("bot.outcome_entry_execution_service.time.time", lambda: float(lifecycle.updated_at_ts) + 60.1)
    resting = SimpleNamespace(coin="#yes", inventory="0", state="buy_resting", buy_order_ids=("buy-1",))
    result = service.manage_resting_buy(
        snapshot=snapshot(active=(resting,)), config=OutcomeLiveStrategyConfig(stale_entry_cancel_enabled=True),
    )
    assert result is not None and result.state == "cancelled"
    assert gateway.cancel_calls and gateway.cancel_calls[0]["order_id"] == "buy-1"
    blocked = service.preflight(
        snapshot=snapshot(side_index=0), admission={}, config=OutcomeLiveStrategyConfig(stale_entry_cancel_enabled=True),
    )
    assert blocked is not None and "requires_ineligible" in blocked.detail
    rearm = service.preflight(
        snapshot=snapshot(side_index=None), admission={}, config=OutcomeLiveStrategyConfig(stale_entry_cancel_enabled=True),
    )
    assert rearm is not None and "rearmed_waiting" in rearm.detail
    fresh = OutcomeRuntimeTickSnapshot(**{**snapshot(side_index=0).__dict__, "entry_decision_at_ms": int(__import__("time").time() * 1000) + 1})
    assert service.preflight(snapshot=fresh, admission={}, config=OutcomeLiveStrategyConfig(stale_entry_cancel_enabled=True)) is None


def test_stale_zero_fill_does_not_cancel_before_60_or_when_official_fill_exists(monkeypatch, tmp_path):
    service, store, _account, gateway, lifecycle = _stale_service(tmp_path)
    resting = SimpleNamespace(coin="#yes", inventory="0", state="buy_resting", buy_order_ids=("buy-1",))
    monkeypatch.setattr("bot.outcome_entry_execution_service.time.time", lambda: float(lifecycle.updated_at_ts) + 59.9)
    result = service.manage_resting_buy(snapshot=snapshot(active=(resting,)), config=OutcomeLiveStrategyConfig(stale_entry_cancel_enabled=True))
    assert result is not None and result.state == "buy_resting" and not gateway.cancel_calls
    monkeypatch.setattr("bot.outcome_entry_execution_service.time.time", lambda: float(lifecycle.updated_at_ts) + 60.1)
    service.ledger = SimpleNamespace()  # no effect; fill evidence comes from immutable journal
    store.journal.log_order_event("run", "ORDER_FILLED", venue_order_id="buy-1", side="BUY", status="FILLED", instrument_id="#yes", payload={
        "outcome_id": 7, "coin": "#yes", "actual_fill": True, "trade_id": "official-fill",
    })
    result = service.manage_resting_buy(snapshot=snapshot(active=(resting,)), config=OutcomeLiveStrategyConfig(stale_entry_cancel_enabled=True))
    assert result is not None and result.state == "reconcile_required" and not gateway.cancel_calls


def test_stale_zero_fill_ambiguous_cancel_fails_closed_without_replacement(monkeypatch, tmp_path):
    service, store, account, gateway, lifecycle = _stale_service(tmp_path)
    # Simulate a transport ACK whose authoritative account read still shows
    # the order.  The common cancel primitive must leave the re-arm fence shut.
    def leave_order_open(**kwargs):
        gateway.cancel_calls.append(kwargs)
        return {}
    gateway.cancel_owned_order = leave_order_open
    monkeypatch.setattr("bot.outcome_entry_execution_service.time.time", lambda: float(lifecycle.updated_at_ts) + 60.1)
    resting = SimpleNamespace(coin="#yes", inventory="0", state="buy_resting", buy_order_ids=("buy-1",))
    result = service.manage_resting_buy(
        snapshot=snapshot(active=(resting,)), config=OutcomeLiveStrategyConfig(stale_entry_cancel_enabled=True),
    )
    assert result is not None and result.state == "reconcile_required"
    assert account.orders
    blocked = service.preflight(
        snapshot=snapshot(side_index=0), admission={}, config=OutcomeLiveStrategyConfig(stale_entry_cancel_enabled=True),
    )
    assert blocked is not None and "stale_cancel_pending_terminal_reconciliation" in blocked.detail
