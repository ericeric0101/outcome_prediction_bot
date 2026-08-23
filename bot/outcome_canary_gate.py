"""P4 canary readiness audit.  This module intentionally cannot submit orders."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from monitoring.trade_journal_db import TradeJournalDB


class OutcomeCanaryDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class OutcomeCanaryReadiness:
    official_resolutions: int
    ws_resyncs: int
    parity_snapshots: int
    actual_fills: int
    reasons: tuple[str, ...]

    @property
    def ready_for_live(self) -> bool:
        return False  # P4 is deliberately a non-live implementation.

    @classmethod
    def from_journal(cls, db_path: str) -> "OutcomeCanaryReadiness":
        if not Path(db_path).exists():
            return cls(0, 0, 0, 0, ("journal_missing", "canary_hard_disabled"))
        with sqlite3.connect(db_path) as conn:
            events = conn.execute("SELECT event_type FROM strategy_events").fetchall()
            fills = conn.execute("SELECT payload_json FROM order_events WHERE event_type='ORDER_FILLED'").fetchall()
        event_types = [row[0] for row in events]
        actual_fills = sum(
            1 for (payload,) in fills
            if json.loads(payload or "{}").get("venue") == "hyperliquid_outcome"
        )
        resolutions = event_types.count("OUTCOME_RESOLUTION_CONFIRMED")
        resyncs = event_types.count("OUTCOME_WS_REST_RESYNC")
        parity = event_types.count("OUTCOME_P2_PARITY_SNAPSHOT")
        reasons = []
        if resolutions < 20:
            reasons.append(f"P0 requires 20 official resolutions; found {resolutions}")
        if resyncs < 1:
            reasons.append("P1 requires observed WebSocket reconnect/resync evidence")
        if parity < 1:
            reasons.append("P2 requires parity snapshots")
        if actual_fills < 30:
            reasons.append(f"P3 requires 30 actual fills before calibration; found {actual_fills}")
        reasons.append("canary_hard_disabled: manual approval and a separate implementation are required")
        return cls(resolutions, resyncs, parity, actual_fills, tuple(reasons))


class OutcomeCanaryGate:
    """Writes an auditable block event but never owns an exchange client."""

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id

    def block(self, readiness: OutcomeCanaryReadiness) -> None:
        self.journal.log_strategy_event(self.run_id, "OUTCOME_CANARY_BLOCKED", {
            "venue": "hyperliquid_outcome", "live_submission_attempted": False,
            "reasons": list(readiness.reasons), "official_resolutions": readiness.official_resolutions,
            "ws_resyncs": readiness.ws_resyncs, "parity_snapshots": readiness.parity_snapshots,
            "actual_fills": readiness.actual_fills,
        })

    def authorize_live_submission(self, readiness: OutcomeCanaryReadiness) -> None:
        self.block(readiness)
        raise OutcomeCanaryDisabled("Outcome P4 canary is hard-disabled; no exchange action exists in this gate")
