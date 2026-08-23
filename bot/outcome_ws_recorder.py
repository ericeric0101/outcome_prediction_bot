"""P1 read-only WebSocket recorder with explicit reconnect/gap evidence."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Mapping, Optional

from monitoring.trade_journal_db import TradeJournalDB


class OutcomeWebSocketRecorder:
    """Persist raw market stream messages; no trading client method is called."""

    def __init__(self, client: Any, journal: TradeJournalDB, run_id: str) -> None:
        self.client, self.journal, self.run_id = client, journal, run_id
        self._market_id: Optional[int] = None
        self._coins: tuple[str, str] = ("", "")
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.resync_required = threading.Event()
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
        if payload.get("event") in {"connected", "disconnected"}:
            self.resync_required.set()

    def _on_l2(self, payload: Mapping[str, Any]) -> None:
        self._record("OUTCOME_WS_L2_BOOK", payload)

    def _on_mids(self, payload: Mapping[str, Any]) -> None:
        self._record("OUTCOME_WS_ALL_MIDS", payload)

    def _on_trades(self, payload: Mapping[str, Any]) -> None:
        self._record("OUTCOME_WS_TRADES", payload)

    def start(self, *, outcome_id: int, yes_coin: str, no_coin: str) -> None:
        if self._thread and self._thread.is_alive():
            if self._market_id == outcome_id:
                return
            raise RuntimeError("market rollover requires recorder restart; stale subscriptions are unsafe")
        self._market_id, self._coins = outcome_id, (yes_coin, no_coin)
        self._stop.clear()
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
