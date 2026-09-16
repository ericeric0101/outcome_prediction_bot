"""CLI wrapper for the read-only Outcome exit decision replay."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.outcome_exit_decision_replay_report import main


if __name__ == "__main__":
    main()
