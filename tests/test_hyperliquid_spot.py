from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace
import json
import sqlite3

import pytest

from bot.hyperliquid_spot import (
    BTCSpotExecutionService, SpotMarket, SpotRiskLimits,
    HyperliquidSpotClient, align_spot_price, align_spot_size, build_btc_spot_testnet_service,
    resolve_btc_usdc_spot,
)
from bot.btc_spot_shadow import BTCSpotShadowCapture
from bot.btc_spot_shadow_report import build_report
from monitoring.trade_journal_db import TradeJournalDB


def _meta(pair_index=142):
    return {
        "tokens": [
            {"name": "UBTC", "index": 197, "szDecimals": 5},
            {"name": "USDC", "index": 0, "szDecimals": 8},
        ],
        "universe": [{"name": "UBTC/USDC", "index": pair_index, "tokens": [197, 0]}],
    }


MARKET = SpotMarket("UBTC/USDC", "@142", 142, 10142, "UBTC", 197, "USDC", 0, 5)


class FakeSpotClient:
    def __init__(self, *, fail_exchange=False):
        self.auth = SimpleNamespace(is_testnet=True, wallet_address="0xabc")
        self.execution_enabled = True
        self.balances = {"USDC": (Decimal("50"), Decimal("50")), "UBTC": (Decimal("0"), Decimal("0"))}
        self.orders = []
        self.actions = []
        self.fail_exchange = fail_exchange

    def spot_state(self):
        return {"balances": [{"coin": coin, "total": str(total), "hold": str(total - available)}
                              for coin, (total, available) in self.balances.items()]}

    def open_orders(self):
        return list(self.orders)

    def l2_book(self, _coin):
        return {"time": int(time.time() * 1000), "levels": [[{"px": "84800", "sz": "1"}], [{"px": "84802", "sz": "1"}]]}

    def _exchange(self, action):
        self.actions.append(action)
        if self.fail_exchange:
            raise TimeoutError("ambiguous transport")
        if action["type"] == "order":
            row = action["orders"][0]
            self.orders.append({"coin": "@142", "oid": 12, "cloid": row["c"]})
        elif action["type"] == "cancel":
            oid = str(action["cancels"][0]["o"])
            self.orders = [row for row in self.orders if str(row.get("oid")) != oid]
        return {"status": "ok"}


class FakeJournal:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    def log_durable_strategy_event(self, run_id, event_type, payload):
        if self.fail:
            return None
        self.events.append((run_id, event_type, payload))
        return len(self.events)

    def log_best_effort_strategy_event(self, run_id, event_type, payload, timeout_sec=0.05):
        self.events.append((run_id, event_type, payload))
        return len(self.events)

    def has_unresolved_spot_testnet_intent(self, wallet):
        return False

    def log_best_effort_strategy_event(self, run_id, event_type, payload, timeout_sec=0.05):
        self.events.append((run_id, event_type, payload))
        return len(self.events)


def test_resolve_btc_usdc_from_metadata_and_reject_ambiguous_pair():
    assert resolve_btc_usdc_spot(_meta()).asset_id == 10142
    with pytest.raises(ValueError, match="exactly one"):
        resolve_btc_usdc_spot({**_meta(), "universe": _meta()["universe"] * 2})


def test_spot_decimal_alignment():
    assert align_spot_size(Decimal("0.123456"), sz_decimals=5) == Decimal("0.12345")
    assert align_spot_price(Decimal("84802.123"), sz_decimals=5) == Decimal("84802")
    assert align_spot_price(Decimal("1.234567"), sz_decimals=5) == Decimal("1.2340")


def test_service_submits_only_bounded_post_only_buy():
    client = FakeSpotClient()
    journal = FakeJournal()
    service = BTCSpotExecutionService(client=client, journal=journal, limits=SpotRiskLimits())
    service.submit_alo(market=MARKET, is_buy=True, price=Decimal("84800"), size=Decimal("0.0001"))
    order = client.actions[0]["orders"][0]
    assert order["a"] == 10142 and order["b"] is True
    assert order["t"] == {"limit": {"tif": "Alo"}}
    assert journal.events[0][1] == "BTC_SPOT_TESTNET_ORDER_INTENT"
    assert journal.events[1][1] == "BTC_SPOT_TESTNET_ORDER_CONFIRMED"


def test_service_fences_ambiguous_mutation_and_never_retries():
    client = FakeSpotClient(fail_exchange=True)
    service = BTCSpotExecutionService(client=client, journal=FakeJournal())
    with pytest.raises(TimeoutError):
        service.submit_alo(market=MARKET, is_buy=True, price=Decimal("84800"), size=Decimal("0.0001"))
    with pytest.raises(RuntimeError, match="fenced"):
        service.submit_alo(market=MARKET, is_buy=True, price=Decimal("84800"), size=Decimal("0.0001"))
    assert len(client.actions) == 1


def test_service_rejects_position_and_balance_overruns_and_marketable_price():
    client = FakeSpotClient()
    service = BTCSpotExecutionService(client=client, journal=FakeJournal())
    with pytest.raises(ValueError, match="notional"):
        service.submit_alo(market=MARKET, is_buy=True, price=Decimal("84800"), size=Decimal("1"))
    with pytest.raises(RuntimeError, match="crossed/stale"):
        service.submit_alo(market=MARKET, is_buy=True, price=Decimal("84802"), size=Decimal("0.0001"))
    client.balances["UBTC"] = (Decimal("0.001"), Decimal("0"))
    with pytest.raises(RuntimeError, match="available BTC"):
        service.submit_alo(market=MARKET, is_buy=False, price=Decimal("84803"), size=Decimal("0.0001"))


def test_testnet_builder_requires_both_environment_gates_and_testnet_auth():
    with pytest.raises(RuntimeError, match="not explicitly enabled"):
        build_btc_spot_testnet_service(SimpleNamespace(is_testnet=True), journal=FakeJournal(), environ={})
    with pytest.raises(RuntimeError, match="HL_TESTNET"):
        build_btc_spot_testnet_service(SimpleNamespace(is_testnet=False), journal=FakeJournal(),
                                       environ={"BTC_SPOT_TESTNET_EXECUTION_ENABLED": "1"})


def test_spot_client_refuses_mainnet_mutation_even_when_flag_is_set():
    auth = SimpleNamespace(is_testnet=False, base_url="https://api.hyperliquid.xyz", wallet_address="0xabc")
    with pytest.raises(RuntimeError, match="testnet-only"):
        HyperliquidSpotClient(auth, execution_enabled=True)
    misconfigured = SimpleNamespace(is_testnet=True, base_url="https://api.hyperliquid.xyz", wallet_address="0xabc")
    with pytest.raises(RuntimeError, match="official Hyperliquid testnet"):
        HyperliquidSpotClient(misconfigured, execution_enabled=True)


def test_service_requires_durable_intent_and_confirmed_cancel():
    client = FakeSpotClient()
    with pytest.raises(RuntimeError, match="durable BTC spot order intent"):
        BTCSpotExecutionService(client=client, journal=FakeJournal(fail=True)).submit_alo(
            market=MARKET, is_buy=True, price=Decimal("84800"), size=Decimal("0.0001"))
    service = BTCSpotExecutionService(client=client, journal=FakeJournal())
    placed = service.submit_alo(market=MARKET, is_buy=True, price=Decimal("84800"), size=Decimal("0.0001"))
    assert service.cancel_owned_order(market=MARKET, order_id=placed["venue_order_id"])["status"] == "ok"


def test_spot_testnet_journal_recovery_fences_unresolved_and_releases_confirmed(tmp_path):
    journal = TradeJournalDB(str(tmp_path / "spot.db"))
    intent = journal.log_durable_strategy_event("spot", "BTC_SPOT_TESTNET_ORDER_INTENT", {"wallet": "0xabc"})
    assert intent is not None
    assert journal.has_unresolved_spot_testnet_intent("0xAbC")
    journal.log_durable_strategy_event("spot", "BTC_SPOT_TESTNET_ORDER_CONFIRMED", {
        "wallet": "0xabc", "intent_event_id": intent,
    })
    assert not journal.has_unresolved_spot_testnet_intent("0xabc")


class FakeCaptureClient:
    def get_spot_meta_sync(self):
        return _meta()

    def get_l2_book_sync(self, _coin, ttl_sec=0):
        return {"time": int(time.time() * 1000), "levels": [
            [{"px": "84800", "sz": "1"}], [{"px": "84802", "sz": "2"}],
        ]}

    def get_all_mids_sync(self, ttl_sec=0):
        return {"BTC": "84801"}

    def get_meta_and_asset_ctxs_sync(self):
        return [{"universe": [{"name": "BTC"}]}, [{"markPx": "84801", "openInterest": "123"}]]


def test_btc_spot_shadow_capture_is_read_only_and_records_spot_perp_context():
    journal = FakeJournal()
    capture = BTCSpotShadowCapture(client=FakeCaptureClient(), journal=journal)
    assert capture.run_once(now_ms=int(time.time() * 1000))
    event_type, payload = journal.events[0][1], journal.events[0][2]
    assert event_type == "BTC_SPOT_SHADOW_SNAPSHOT"
    assert payload["coin"] == "@142" and payload["spot_asset_id"] == 10142
    assert payload["bid"] == "84800" and payload["ask"] == "84802"
    assert payload["perp_context"]["openInterest"] == "123"
    assert payload["read_only"] is True and payload["live_authority"] is False


def test_btc_spot_shadow_probability_waits_for_valid_history_then_scores_both_sides():
    capture = BTCSpotShadowCapture(client=FakeCaptureClient(), journal=FakeJournal())
    now = int(time.time() * 1000)
    capture._spot_history = [
        (now - 1_500_000 + i * 5_000, 84_000.0 + ((i % 5) - 2) * 0.2)
        for i in range(301)
    ]
    result = capture.settlement_probability_shadow(strike=84_000, time_left_sec=10_800, as_of_ms=now)
    assert result["status"] == "available"
    assert result["source"] == "hyperliquid_spot_l2_mid"
    assert result["probability_up"] + result["probability_down"] == 1.0
    assert result["live_authority"] is False


def test_btc_spot_shadow_probability_restores_recent_persisted_history(tmp_path):
    journal = TradeJournalDB(str(tmp_path / "spot-history.db"))
    now = int(time.time() * 1000)
    for i in range(301):
        timestamp = now - 1_500_000 + i * 5_000
        journal.log_strategy_event("spot", "BTC_SPOT_SHADOW_SNAPSHOT", {
            "read_only": True, "snapshot_timestamp_ms": timestamp,
            "server_timestamp_ms": timestamp, "mid": str(84_000 + ((i % 5) - 2) * 0.2),
        })
    capture = BTCSpotShadowCapture(client=FakeCaptureClient(), journal=journal)
    result = capture.settlement_probability_shadow(strike=84_000, time_left_sec=10_800, as_of_ms=now)
    assert len(capture._spot_history) == 301
    assert result["status"] == "available"


def test_btc_spot_report_uses_same_journal_without_interpolation(tmp_path):
    path = tmp_path / "journal.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, event_type TEXT, payload_json TEXT)")
    start = 1_800_000_000_000
    rows = []
    for i in range(1000):
        payload = {"snapshot_timestamp_ms": start + i * 5000, "mid": str(80_000 + i), "read_only": True}
        rows.append((i + 1, "BTC_SPOT_SHADOW_SNAPSHOT", json.dumps(payload)))
    conn.executemany("INSERT INTO strategy_events VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    result = build_report(path)
    assert result["snapshots"] == 1000
    assert result["status"] == "insufficient_data"
    assert result["horizons"]["5m"]["per_predictor_complete_cases"]["spot_5m_momentum"]["n"] > 0
    assert result["horizons"]["5m"]["per_predictor_complete_cases"]["spot_5m_momentum"]["direction_hit_rate_pct"] == 100.0
    assert result["authority"]["live_authority"] is False
