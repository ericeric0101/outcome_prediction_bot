"""Runtime safety manifest, readiness and transition-only forensic audit.

This module deliberately owns no exchange client, gateway, order controller or
strategy decision.  It documents what a live process *actually loaded* and
keeps entry fail-closed when a declared safety-critical dependency is absent.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from monitoring.trade_journal_db import TradeJournalDB


MANIFEST_EVENT = "OUTCOME_RUNTIME_STARTUP_MANIFEST"
NOT_READY_EVENT = "OUTCOME_SAFETY_COMPONENT_NOT_READY"
GATE_AUDIT_EVENT = "OUTCOME_EXIT_SAFETY_GATE_DECISION"


@dataclass(frozen=True)
class SafetyComponent:
    name: str
    enabled: bool
    authority: str
    version: str
    health: str
    ready: bool
    safety_critical: bool = False


def _git_value(repo_root: Path, *args: str, default: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), *args], text=True, stderr=subprocess.DEVNULL,
        ).strip() or default
    except (OSError, subprocess.SubprocessError):
        return default


def safe_config_fingerprint(environ: dict[str, str] | None = None) -> str:
    """Hash only non-secret Outcome safety configuration.

    Values are retained only in the hash input; the manifest never writes them
    to the journal.  Wallets, keys, credentials and arbitrary application
    secrets are excluded by name.
    """
    source = environ if environ is not None else dict(os.environ)
    safe: list[str] = []
    for key, value in source.items():
        upper = key.upper()
        if not (upper.startswith("OUTCOME_") or upper in {"HL_TESTNET", "HL_BASE_URL", "HL_WS_URL"}):
            continue
        if any(token in upper for token in ("KEY", "SECRET", "TOKEN", "WALLET", "ADDRESS", "PASSWORD")):
            continue
        safe.append(f"{key}={value.strip()}")
    return hashlib.sha256("\n".join(sorted(safe)).encode()).hexdigest()


class OutcomeRuntimeSafety:
    """Durable startup evidence plus no-spam state-transition auditing."""

    def __init__(self, *, journal: TradeJournalDB | None, run_id: str | None, repo_root: Path) -> None:
        self.journal = journal
        self.run_id = run_id
        self.repo_root = repo_root
        self._last_gate_state: dict[tuple[str, str, str], str] = {}
        self._manifest_written = False

    def startup_manifest(self, components: Iterable[SafetyComponent]) -> dict[str, Any]:
        git_sha = _git_value(self.repo_root, "rev-parse", "HEAD", default="unknown")
        dirty = _git_value(self.repo_root, "status", "--porcelain", default="unknown")
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "git_sha": git_sha,
            "git_dirty": None if dirty == "unknown" else bool(dirty),
            "deployment_id": os.environ.get("OUTCOME_DEPLOYMENT_ID") or None,
            "app_version": os.environ.get("OUTCOME_APP_VERSION") or git_sha[:12],
            "config_fingerprint_sha256": safe_config_fingerprint(),
            "components": [asdict(component) for component in components],
        }

    def write_startup_manifest(self, components: Iterable[SafetyComponent]) -> bool:
        if self._manifest_written:
            return True
        if self.journal is None or not self.run_id:
            return False
        event_id = self.journal.log_durable_strategy_event(
            self.run_id, MANIFEST_EVENT, self.startup_manifest(tuple(components)),
        )
        self._manifest_written = event_id is not None
        return self._manifest_written

    def audit_gate(
        self, *, component: str, eligible: bool, reason: str, outcome_id: int,
        lifecycle_id: str | None, position_age_sec: float | None,
        current_executable_pnl: str | None, reversal_state: str | None,
        independent_confirmation_count: int | None, loss_band_state: str | None,
        book_state: str | None,
    ) -> None:
        """Persist only changed policy states; routine ticks remain memory-only."""
        if self.journal is None or not self.run_id:
            return
        key = (component, str(outcome_id), str(lifecycle_id or ""))
        state = "|".join((str(eligible), reason, str(loss_band_state), str(book_state), str(reversal_state)))
        if self._last_gate_state.get(key) == state:
            return
        self._last_gate_state[key] = state
        self.journal.log_strategy_event(self.run_id, GATE_AUDIT_EVENT, {
            "schema_version": 1, "component": component, "eligible": eligible,
            "reason": reason, "outcome_id": outcome_id, "lifecycle_id": lifecycle_id,
            "position_age_sec": position_age_sec, "current_executable_pnl": current_executable_pnl,
            "reversal_state": reversal_state,
            "independent_confirmation_count": independent_confirmation_count,
            "loss_band_state": loss_band_state, "book_state": book_state,
            "execution_submitted": False,
        })

    def audit_not_ready(self, *, components: Iterable[SafetyComponent], outcome_id: int | None = None) -> None:
        missing = [item.name for item in components if item.safety_critical and not item.ready]
        if not missing or self.journal is None or not self.run_id:
            return
        key = ("runtime_readiness", str(outcome_id or "none"), "")
        state = ",".join(sorted(missing))
        if self._last_gate_state.get(key) == state:
            return
        self._last_gate_state[key] = state
        self.journal.log_durable_strategy_event(self.run_id, NOT_READY_EVENT, {
            "schema_version": 1, "outcome_id": outcome_id,
            "missing_safety_critical_components": missing,
            "action": "new_entry_blocked_existing_protection_unchanged",
        })
