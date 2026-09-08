"""P1 read-only WebSocket recorder with explicit reconnect/gap evidence."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Mapping, Optional

from monitoring.trade_journal_db import TradeJournalDB
from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_stream_health import OutcomeStreamHealth


class OutcomeWebSocketRecorder:
    """Persist raw market stream messages; no trading client method is called."""

    def __init__(self, client: Any, journal: TradeJournalDB, run_id: str, *, pricing_state: Any | None = None) -> None:
        self.client, self.journal, self.run_id = client, journal, run_id
        # The recorder is the single subscription owner for the live launcher.
        # Feeding its *received* L2 snapshots into the display cache prevents a
        # slow research write from making terminal BBOs look empty.  Execution
        # still independently fetches a fresh REST book before any order.
        self.pricing_state = pricing_state
        self._market_id: Optional[int] = None
        self._coins: tuple[str, str] = ("", "")
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Coalesced execution wake-up.  The callback never trades; it only
        # shortens the serial main loop's wait after a relevant L2 update.
        self._l2_update = threading.Event()
        self.resync_required = threading.Event()
        self.health = OutcomeStreamHealth()
        self._registered = False

    @staticmethod
    def _server_timestamp(payload: Mapping[str, Any]) -> Optional[int]:
        data = payload.get("data")
        if isinstance(data, Mapping):
            for key in ("time", "timestamp"):
                if isinstance(data.get(key), int):
                    return int(data[key])
        if isinstance(data, list):
            # Hyperliquid trade subscriptions deliver a batch.  Preserve the
            # event-level latest exchange timestamp without pretending that it
            # is a contiguous sequence number.
            timestamps = [
                int(item[key])
                for item in data if isinstance(item, Mapping)
                for key in ("time", "timestamp") if isinstance(item.get(key), int)
            ]
            if timestamps:
                return max(timestamps)
        return None

    def _record(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.journal.log_strategy_event(self.run_id, event_type, {
            "venue": "hyperliquid_outcome", "read_only": True,
            "outcome_id": self._market_id, "local_received_at_ms": int(time.time() * 1000),
            "server_timestamp_ms": self._server_timestamp(payload),
            "sequence": None, "sequence_available": False,
            "raw": dict(payload),
        })

    def _on_lifecycle(self, payload: Mapping[str, Any]) -> None:
        self._record("OUTCOME_WS_LIFECYCLE", payload)
        self.health.on_lifecycle(str(payload.get("event", "")))
        if payload.get("event") in {"connected", "disconnected"}:
            self.resync_required.set()

    def _on_l2(self, payload: Mapping[str, Any]) -> None:
        data = payload.get("data")
        if isinstance(data, Mapping) and isinstance(data.get("coin"), str):
            if self.pricing_state is not None:
                try:
                    self.pricing_state.update_l2_book(str(data["coin"]), dict(data))
                except Exception:
                    # Recording and stream health must remain available even if
                    # a malformed display update is rejected.
                    pass
            self.health.on_l2_book(data["coin"], payload=dict(data))
            self._l2_update.set()
        self._record("OUTCOME_WS_L2_BOOK", payload)

    def _on_mids(self, payload: Mapping[str, Any]) -> None:
        data = payload.get("data")
        mids = data.get("mids") if isinstance(data, Mapping) else None
        if self.pricing_state is not None and isinstance(mids, Mapping):
            # BTC mark is public observation only.  It may drive signal/UI
            # when fresh, but no account/order truth is ever derived from it.
            try:
                btc = mids.get("BTC")
                if btc is not None:
                    self.pricing_state.update_btc_mark_price(str(btc))
            except Exception:
                pass
        # allMids contains every Hyperliquid asset and was previously copied
        # verbatim for each WS update.  This bot only needs BTC plus the two
        # active Outcome coins; persisting the global map made a compact
        # execution journal grow into multi-GB storage without adding a
        # decision or reconciliation fact.
        selected: dict[str, str] = {}
        if isinstance(mids, Mapping):
            for coin in ("BTC", *self._coins):
                value = mids.get(coin)
                if value is not None:
                    selected[coin] = str(value)
        compact_payload = {
            "channel": payload.get("channel", "allMids"),
            "recording_scope": "btc_and_active_outcome_only_v2",
            "data": {
                "time": data.get("time") if isinstance(data, Mapping) else None,
                "mids": selected,
            },
        }
        self._record("OUTCOME_WS_ALL_MIDS", compact_payload)

    def _on_trades(self, payload: Mapping[str, Any]) -> None:
        self._record("OUTCOME_WS_TRADES", payload)

    def start(self, *, outcome_id: int, yes_coin: str, no_coin: str) -> None:
        if self._thread and self._thread.is_alive():
            if self._market_id == outcome_id:
                return
            raise RuntimeError("market rollover requires recorder restart; stale subscriptions are unsafe")
        self._market_id, self._coins = outcome_id, (yes_coin, no_coin)
        self.health.market_id, self.health.coins = outcome_id, (yes_coin, no_coin)
        self.health.book_received_at = {}
        self.health.resync_required = True
        self._stop.clear()
        self._l2_update.clear()
        self._register_callbacks()
        self._thread = threading.Thread(target=self._run, daemon=True, name="outcome-shadow-ws")
        self._thread.start()

    def _register_callbacks(self) -> None:
        if self._registered:
            return
        self.client.register_callback("__lifecycle__", self._on_lifecycle)
        self.client.register_callback("l2Book", self._on_l2)
        self.client.register_callback("allMids", self._on_mids)
        self.client.register_callback("trades", self._on_trades)
        self._registered = True

    def _unregister_callbacks(self) -> None:
        if not self._registered:
            return
        for channel, callback in (
            ("__lifecycle__", self._on_lifecycle), ("l2Book", self._on_l2),
            ("allMids", self._on_mids), ("trades", self._on_trades),
        ):
            unregister = getattr(self.client, "unregister_callback", None)
            if unregister:
                unregister(channel, callback)
        self._registered = False

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        await self.client.subscribe_all_mids()
        for coin in self._coins:
            await self.client.subscribe_l2_book(coin)
            await self.client.subscribe_trades(coin)
        await self.client.start_ws()
        try:
            while not self._stop.wait(0.2):
                await asyncio.sleep(0)
        finally:
            await self.client.stop_ws()
            await self.client.unsubscribe_all_mids()
            for coin in self._coins:
                await self.client.unsubscribe_l2_book(coin)
                await self.client.unsubscribe_trades(coin)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._unregister_callbacks()

    def mark_rest_resynced(self) -> None:
        """Permit execution only after caller completed both REST book reads."""
        self.health.mark_rest_resynced()

    def wait_for_l2_update(self, timeout_sec: float) -> bool:
        """Wake the execution loop early while keeping mutations off the WS thread."""
        triggered = self._l2_update.wait(max(0.0, timeout_sec))
        if triggered:
            self._l2_update.clear()
        return triggered
