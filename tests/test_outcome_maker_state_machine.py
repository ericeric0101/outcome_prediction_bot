from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_maker_state_machine import OutcomeMakerStateMachine
from bot.outcome_exit_lifecycle import OutcomeExitLifecycle, OutcomeExitLifecycleStore
from bot.outcome_sdk_sidecar import OutcomeSdkAmbiguousExecutionError
from monitoring.trade_journal_db import TradeJournalDB


def market():
    return OutcomeMarketSpec(1153, "@1153", "#11530", "#11531", 1, 2, "priceBinary", "BTC", "20260824-1400", 1, 0, Decimal("1"), "15m", "")


class Account:
    def __init__(self, total="0", orders=None, fills=None): self.total, self.orders, self.fills = total, orders or [], fills or []
    def get_spot_clearinghouse_state_sync(self, _): return {"balances": [{"coin": "#11530", "total": self.total}]}
    def get_open_orders_sync(self, _): return self.orders
    def get_user_fills_sync(self, _): return self.fills


class Gateway:
    def __init__(self): self.calls = []
    def outcome_coin(self, _, side): return "#11530" if side == 0 else "#11531"
    def fetch_order_book(self, **_): return {"bids": [{"price": "0.77"}], "asks": [{"price": "0.78"}]}
    def place_alo(self, **kwargs): self.calls.append(("place", kwargs)); return {"orderId": "9"}
    def cancel_owned_order(self, **kwargs): self.calls.append(("cancel", kwargs)); return {"ok": True}


def test_tick_places_one_buy_without_waiting():
    gateway = Gateway()
    result = OutcomeMakerStateMachine(account=Account(), gateway=gateway, wallet="w").tick(market=market(), side_index=0, entry_permitted=True)
    assert result.state == "buy_placed"
    assert [kind for kind, _ in gateway.calls] == ["place"]


def test_tick_forces_rest_immediately_before_submit_and_blocks_newly_visible_order():
    class LaggingAccount(Account):
        def __init__(self):
            super().__init__()
            self.forced_reads = 0

        def force_open_orders_reconciliation_sync(self, _wallet):
            self.forced_reads += 1
            return [{"oid": "already-there", "coin": "#11530", "side": "B", "sz": "10"}]

    account, gateway = LaggingAccount(), Gateway()
    result = OutcomeMakerStateMachine(account=account, gateway=gateway, wallet="w").tick(
        market=market(), side_index=0, entry_permitted=True,
    )
    assert result.state == "blocked"
    assert "pre-submit REST" in result.detail
    assert account.forced_reads == 1
    assert gateway.calls == []


def test_entry_submit_rechecks_minimum_price_after_fresh_book_read():
    class LowerBidGateway(Gateway):
        def fetch_order_book(self, **_):
            return {"bids": [{"price": "0.545"}], "asks": [{"price": "0.546"}]}

    gateway = LowerBidGateway()
    result = OutcomeMakerStateMachine(account=Account(), gateway=gateway, wallet="w").tick(
        market=market(), side_index=0, entry_permitted=True,
        entry_min_submit_price=Decimal("0.55"),
        entry_audit={"entry_bid_at_decision": "0.55"},
    )
    assert result.state == "flat"
    assert result.detail == "entry submit price fell below configured minimum"
    assert gateway.calls == []
    assert result.audit["entry_submit_bid"] == "0.545"


def test_tick_refuses_best_ask_fallback_for_inventory_without_exit_policy():
    gateway = Gateway()
    result = OutcomeMakerStateMachine(account=Account("13"), gateway=gateway, wallet="w").tick(market=market(), side_index=0, entry_permitted=True)
    assert result.state == "blocked"
    assert "refusing best-ask fallback sell" in result.detail
    assert not gateway.calls


def test_tick_recognizes_hyperliquid_plus_prefixed_spot_inventory():
    gateway = Gateway()
    account = Account("0")
    account.get_spot_clearinghouse_state_sync = lambda _: {"balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}]}
    result = OutcomeMakerStateMachine(account=account, gateway=gateway, wallet="w").tick(
        market=market(), side_index=0, entry_permitted=False,
    )
    assert result.state == "blocked"
    assert "no explicit verified exit policy" in result.detail


def test_calibration_inventory_uses_net_ten_percent_take_profit():
    gateway = Gateway()
    account = Account("0")
    account.get_spot_clearinghouse_state_sync = lambda _: {"balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}]}
    account.get_user_fills_sync = lambda _: [{"coin": "#11530", "side": "B", "px": "0.80", "sz": "13", "time": 1}]
    result = OutcomeMakerStateMachine(account=account, gateway=gateway, wallet="w").tick(
        market=market(), side_index=0, entry_permitted=False,
        minimum_return_pct=Decimal("0.10"), maker_close_fee_rate=Decimal("0.0004"),
    )
    assert result.state == "sell_placed"
    assert gateway.calls[0][1]["price"] == Decimal("0.80") * Decimal("1.10") / Decimal("0.9996")
    assert result.audit == {
        "inventory": "13", "account_entry_vwap": str(Decimal("10") / Decimal("13")), "fill_entry_vwap": "0.80",
        "pricing_basis": "exchange_fill_vwap", "requested_price": str(Decimal("0.80") * Decimal("1.10") / Decimal("0.9996")),
        "take_profit_price": str(Decimal("0.80") * Decimal("1.10") / Decimal("0.9996")),
        "loss_reprice_floor": "unavailable", "exit_mode": "take_profit",
    }


def test_calibration_refuses_unverifiable_account_entry_notional():
    gateway = Gateway()
    account = Account("0")
    account.get_spot_clearinghouse_state_sync = lambda _: {"balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}]}
    result = OutcomeMakerStateMachine(account=account, gateway=gateway, wallet="w").tick(
        market=market(), side_index=0, entry_permitted=False,
        minimum_return_pct=Decimal("0.05"), maker_close_fee_rate=Decimal("0.0004"),
    )
    assert result.state == "blocked"
    assert "cannot verify fill VWAP" in result.detail
    assert not gateway.calls


def test_calibration_uses_exact_durable_fill_fallback_when_userfills_is_incomplete(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    journal.log_outcome_fill_once(
        "run", trade_id="verified-entry", side="BUY", price=0.80, qty=13,
        status="FILLED", instrument_id="#11530", commission_usdc=0,
        payload={"venue": "hyperliquid_outcome", "actual_fill": True, "timestamp_ms": 1},
    )
    gateway = Gateway()
    account = Account("0")
    account.get_spot_clearinghouse_state_sync = lambda _: {"balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}]}
    # Simulate a transient userFills window that no longer contains the buy.
    account.get_user_fills_sync = lambda _: []
    result = OutcomeMakerStateMachine(account=account, gateway=gateway, wallet="w", journal=journal).tick(
        market=market(), side_index=0, entry_permitted=False,
        minimum_return_pct=Decimal("0.05"), maker_close_fee_rate=Decimal("0.0004"),
    )
    assert result.state == "sell_placed"
    assert result.audit is not None and Decimal(result.audit["fill_entry_vwap"]) == Decimal("0.80")


def test_durable_fill_fallback_preserves_timestamp_fifo_across_partial_fills(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    # Deliberately ingest out of exchange order: the durable fallback must use
    # the same timestamp/id FIFO chronology as before streaming was introduced.
    for trade_id, side, price, qty, timestamp in (
        ("sell", "SELL", "0.90", 4, 300),
        ("buy-new", "BUY", "0.70", 5, 200),
        ("buy-old", "BUY", "0.60", 5, 100),
    ):
        assert journal.log_outcome_fill_once(
            "run", trade_id=trade_id, side=side, price=float(price), qty=float(qty),
            status="FILLED", instrument_id="#11530", commission_usdc=0,
            payload={"venue": "hyperliquid_outcome", "actual_fill": True, "timestamp_ms": timestamp},
        )

    # FIFO leaves one $0.60 share and five $0.70 shares: 4.1 / 6.
    assert journal.verified_outcome_fill_vwap_for_inventory(
        coin="#11530", inventory=Decimal("6"),
    ) == Decimal("4.1") / Decimal("6")


def test_calibration_loss_band_cancels_old_profit_sell_without_taking():
    class LossGateway(Gateway):
        def fetch_order_book(self, **_): return {"bids": [{"price": "0.70"}], "asks": [{"price": "0.71"}]}
    gateway = LossGateway()
    account = Account("0", [{"coin": "#11530", "side": "A", "oid": 9, "sz": "13", "limitPx": "0.85"}])
    account.get_spot_clearinghouse_state_sync = lambda _: {"balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}]}
    account.get_user_fills_sync = lambda _: [{"coin": "#11530", "side": "B", "px": "0.80", "sz": "13", "time": 1}]
    result = OutcomeMakerStateMachine(account=account, gateway=gateway, wallet="w").tick(
        market=market(), side_index=0, entry_permitted=False,
        minimum_return_pct=Decimal("0.05"), maker_close_fee_rate=Decimal("0.0004"), loss_reprice_pct=Decimal("0.05"),
        loss_band_authorized=True,
    )
    assert result.state == "blocked"
    assert gateway.calls[0][0] == "cancel"
    assert gateway.calls[0][1]["order_id"] == "9"


def test_calibration_never_cancels_external_sell_when_lifecycle_ownership_is_ambiguous(tmp_path):
    gateway = Gateway()
    account = Account("0", [
        {"coin": "#11530", "side": "A", "oid": "bot-sell", "sz": "13", "limitPx": "0.85"},
        {"coin": "#11530", "side": "A", "oid": "manual-sell", "sz": "13", "limitPx": "0.86"},
    ])
    account.get_spot_clearinghouse_state_sync = lambda _: {"balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}]}
    account.get_user_fills_sync = lambda _: [{"coin": "#11530", "side": "B", "px": "0.80", "sz": "13", "time": 1}]
    store = OutcomeExitLifecycleStore(TradeJournalDB(tmp_path / "journal.db"), "run")
    store.record(OutcomeExitLifecycle("w", 1153, "#11530", "bot-sell", Decimal("13"), Decimal("0.85"), 0, "SELL_RESTING"), reason="fixture")
    result = OutcomeMakerStateMachine(
        account=account, gateway=gateway, wallet="w", exit_lifecycle_store=store,
    ).tick(
        market=market(), side_index=0, entry_permitted=False,
        minimum_return_pct=Decimal("0.05"), maker_close_fee_rate=Decimal("0.0004"),
        loss_reprice_pct=Decimal("0.05"), loss_band_authorized=True,
    )
    assert result.state == "blocked"
    assert result.detail == "multiple same-coin sells require explicit reconciliation"
    assert gateway.calls == []


def test_tier_b_submit_drift_guard_refuses_late_higher_bid_without_placing():
    class ChasingGateway(Gateway):
        def fetch_order_book(self, **_): return {"bids": [{"price": "0.805"}], "asks": [{"price": "0.806"}]}
    gateway = ChasingGateway()
    result = OutcomeMakerStateMachine(account=Account(), gateway=gateway, wallet="w").tick(
        market=market(), side_index=0, entry_permitted=True,
        entry_audit={"entry_bid_at_decision": "0.800"}, entry_max_submit_price=Decimal("0.802"),
    )
    assert result.state == "flat"
    assert "drift exceeds" in result.detail
    assert gateway.calls == []
    assert result.audit["entry_submit_bid"] == "0.805"


def test_tick_cancels_partial_buy_before_sale():
    gateway = Gateway()
    result = OutcomeMakerStateMachine(account=Account("3", [{"coin": "#11530", "side": "B", "oid": 7, "sz": "10"}]), gateway=gateway, wallet="w").tick(market=market(), side_index=0, entry_permitted=True)
    assert result.state == "blocked"
    assert gateway.calls[0][0] == "cancel"


def test_tick_observes_existing_order_without_second_submission():
    gateway = Gateway()
    result = OutcomeMakerStateMachine(account=Account("0", [{"coin": "#11530", "side": "B", "oid": 7, "sz": "13"}]), gateway=gateway, wallet="w").tick(market=market(), side_index=0, entry_permitted=True)
    assert result.state == "buy_resting"
    assert not gateway.calls


def test_initial_protective_sell_ambiguity_is_durably_fenced(tmp_path):
    class AmbiguousGateway(Gateway):
        def place_alo(self, **kwargs):
            self.calls.append(("place", kwargs))
            raise OutcomeSdkAmbiguousExecutionError(
                command="place_limit_order", request_id="protect-1", detail="response timeout",
            )

    journal = TradeJournalDB(tmp_path / "journal.db")
    store = OutcomeExitLifecycleStore(journal, "run")
    gateway = AmbiguousGateway()
    account = Account("0")
    account.get_spot_clearinghouse_state_sync = lambda _: {
        "balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}],
    }
    account.get_user_fills_sync = lambda _: [
        {"coin": "#11530", "side": "B", "px": "0.80", "sz": "13", "time": 1},
    ]
    result = OutcomeMakerStateMachine(
        account=account, gateway=gateway, wallet="w", journal=journal,
        exit_lifecycle_store=store,
    ).tick(
        market=market(), side_index=0, entry_permitted=False,
        minimum_return_pct=Decimal("0.05"), maker_close_fee_rate=Decimal("0.0004"),
    )
    assert result.state == "blocked" and "ambiguous protective SELL" in result.detail
    pending = store.pending_ambiguous_submit(wallet="w", outcome_id=1153, coin="#11530")
    assert pending is not None and pending["order_kind"] == "initial_protective_alo"


def test_initial_protective_sell_adopts_unique_account_truth_oid_when_ack_is_stale(tmp_path):
    class RotatingGateway(Gateway):
        def place_alo(self, **kwargs):
            self.calls.append(("place", kwargs))
            account.orders.append({
                "coin": "#11530", "side": "A", "oid": "rotated-sell",
                "sz": str(kwargs["requested_shares"]), "limitPx": str(kwargs["price"]),
            })
            return {"orderId": "stale-sell"}

    journal = TradeJournalDB(tmp_path / "journal.db")
    store = OutcomeExitLifecycleStore(journal, "run")
    account = Account("0")
    account.get_spot_clearinghouse_state_sync = lambda _: {
        "balances": [{"coin": "+11530", "total": "13", "entryNtl": "10"}],
    }
    account.get_user_fills_sync = lambda _: [
        {"coin": "#11530", "side": "B", "px": "0.80", "sz": "13", "time": 1},
    ]
    result = OutcomeMakerStateMachine(
        account=account, gateway=RotatingGateway(), wallet="w", journal=journal,
        exit_lifecycle_store=store,
    ).tick(
        market=market(), side_index=0, entry_permitted=False,
        minimum_return_pct=Decimal("0.05"), maker_close_fee_rate=Decimal("0.0004"),
    )
    assert result.state == "sell_resting"
    assert result.order_id == "rotated-sell"
    lifecycle = store.recover(wallet="w", outcome_id=1153, coin="#11530")
    assert lifecycle is not None and lifecycle.order_id == "rotated-sell"
