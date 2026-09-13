"""Durable, bounded risk-episode accounting for existing exit controllers.

An episode is not a new execution authority.  It only replaces the unsafe
notion that an IOC attempt permanently consumes every later severe-risk
opportunity in the same daily market.  Existing controllers still own every
cancel, intent, ambiguity fence and IOC mutation.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from monitoring.trade_journal_db import TradeJournalDB


EVENT = "OUTCOME_RISK_EPISODE"


@dataclass(frozen=True)
class RiskEpisode:
    episode_id: str
    wallet: str
    outcome_id: int
    coin: str
    opened_at: float
    trigger_family: str
    peak_severity: str | None
    attempts: int
    state: str


class OutcomeRiskEpisodeStore:
    """Journal-backed bounded attempt budget, with explicit recovery close."""

    def __init__(self, journal: TradeJournalDB, run_id: str, *, max_attempts: int = 2) -> None:
        self.journal, self.run_id, self.max_attempts = journal, run_id, max(1, max_attempts)
        self._uncertain: set[tuple[str, int, str]] = set()

    @staticmethod
    def _parse(raw: object) -> dict[str, Any] | None:
        try:
            value = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def active(self, *, wallet: str, outcome_id: int, coin: str) -> RiskEpisode | None:
        if (wallet, int(outcome_id), coin) in self._uncertain:
            return RiskEpisode("journal_write_uncertain", wallet, int(outcome_id), coin, 0, "unknown", None, self.max_attempts, "OPEN")
        try:
            with sqlite3.connect(f"file:{self.journal.db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """SELECT id,ts,payload_json FROM strategy_events WHERE event_type=?
                       AND json_extract(payload_json,'$.wallet')=?
                       AND CAST(json_extract(payload_json,'$.outcome_id') AS INTEGER)=?
                       AND json_extract(payload_json,'$.coin')=? ORDER BY id DESC LIMIT 1""",
                    (EVENT, wallet, int(outcome_id), coin),
                ).fetchone()
                # If a process dies after an accepted/ambiguous mutation but
                # before `record_attempt` returns, durable controller evidence
                # still consumes this *same* episode on restart.
                evidence = conn.execute(
                    """SELECT COUNT(*) FROM strategy_events WHERE id>? AND
                       ((event_type='OUTCOME_EXIT_LIFECYCLE' AND json_extract(payload_json,'$.state')='EMERGENCY_EXIT_SUBMITTED')
                        OR event_type='OUTCOME_EXIT_ORDER_AMBIGUOUS_SUBMIT')
                       AND json_extract(payload_json,'$.wallet')=?
                       AND CAST(json_extract(payload_json,'$.outcome_id') AS INTEGER)=?
                       AND json_extract(payload_json,'$.coin')=?""",
                    (int(row[0]), wallet, int(outcome_id), coin),
                ).fetchone() if row else (0,)
            payload = self._parse(row[2]) if row else None
            if not payload or payload.get("state") != "OPEN":
                return None
            return RiskEpisode(
                episode_id=str(payload["episode_id"]), wallet=wallet, outcome_id=int(outcome_id), coin=coin,
                opened_at=float(payload.get("opened_at") or 0), trigger_family=str(payload.get("trigger_family") or "unknown"),
                peak_severity=str(payload.get("peak_severity")) if payload.get("peak_severity") is not None else None,
                attempts=max(int(payload.get("attempts") or 0), int(evidence[0] or 0)), state="OPEN",
            )
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            # Journal ambiguity must never reopen a mutation budget.
            return RiskEpisode("journal_unreadable", wallet, int(outcome_id), coin, 0, "unknown", None, self.max_attempts, "OPEN")

    def open_or_resume(self, *, wallet: str, outcome_id: int, coin: str, trigger_family: str,
                       severity: str | None) -> RiskEpisode | None:
        existing = self.active(wallet=wallet, outcome_id=outcome_id, coin=coin)
        if existing is not None:
            return existing
        episode = RiskEpisode(
            episode_id=f"risk:{outcome_id}:{coin}:{uuid.uuid4().hex}", wallet=wallet, outcome_id=int(outcome_id),
            coin=coin, opened_at=time.time(), trigger_family=trigger_family, peak_severity=severity, attempts=0, state="OPEN",
        )
        event = self.journal.log_durable_strategy_event(self.run_id, EVENT, {
            "schema_version": 1, "state": "OPEN", "episode_id": episode.episode_id, "wallet": wallet,
            "outcome_id": int(outcome_id), "coin": coin, "opened_at": episode.opened_at,
            "trigger_family": trigger_family, "peak_severity": severity, "attempts": 0,
        })
        return episode if event is not None else None

    def record_attempt(self, episode: RiskEpisode, *, execution_state: str) -> RiskEpisode | None:
        next_attempts = episode.attempts + 1
        event = self.journal.log_durable_strategy_event(self.run_id, EVENT, {
            "schema_version": 1, "state": "OPEN", "episode_id": episode.episode_id, "wallet": episode.wallet,
            "outcome_id": episode.outcome_id, "coin": episode.coin, "opened_at": episode.opened_at,
            "trigger_family": episode.trigger_family, "peak_severity": episode.peak_severity,
            "attempts": next_attempts, "last_execution_state": execution_state,
        })
        if event is None:
            # Do not let a best-effort failure reopen an already-mutated risk
            # budget inside this process.  Cross-restart reconciliation also
            # falls back to the existing durable IOC/ambiguity evidence.
            self._uncertain.add((episode.wallet, episode.outcome_id, episode.coin))
            return None
        return RiskEpisode(**{**episode.__dict__, "attempts": next_attempts})

    def close_recovered(self, *, wallet: str, outcome_id: int, coin: str, reason: str) -> bool:
        episode = self.active(wallet=wallet, outcome_id=outcome_id, coin=coin)
        if episode is None:
            return False
        return self.journal.log_durable_strategy_event(self.run_id, EVENT, {
            "schema_version": 1, "state": "CLOSED_RECOVERED", "episode_id": episode.episode_id,
            "wallet": wallet, "outcome_id": int(outcome_id), "coin": coin, "opened_at": episode.opened_at,
            "trigger_family": episode.trigger_family, "peak_severity": episode.peak_severity,
            "attempts": episode.attempts, "closed_at": time.time(), "reason": reason,
        }) is not None

    def exhausted(self, episode: RiskEpisode) -> bool:
        return episode.attempts >= self.max_attempts
