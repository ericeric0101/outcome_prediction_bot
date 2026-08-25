"""Fail-closed bridge from P2/P3 research readiness to automated entry."""
from __future__ import annotations

import os
from dataclasses import dataclass

from bot.outcome_research_report import p2_report, p3_report


@dataclass(frozen=True)
class OutcomeResearchGateResult:
    allowed: bool
    reason: str


class OutcomeResearchGate:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.environ.get("OUTCOME_RESEARCH_JOURNAL_PATH", "logs/outcome_shadow.db")

    def check(self, period: str) -> OutcomeResearchGateResult:
        p2 = p2_report(self.db_path, periods=(period,))[0]
        p3 = p3_report(self.db_path, periods=(period,))
        if not p2.ready:
            return OutcomeResearchGateResult(False, f"P2 research gate: {','.join(p2.blockers)}")
        failed = [item for item in p3 if not item.ready]
        if failed:
            blockers = sorted({blocker for item in failed for blocker in item.blockers})
            return OutcomeResearchGateResult(False, f"P3 research gate: {','.join(blockers)}")
        return OutcomeResearchGateResult(True, "P2/P3 research gates passed")
