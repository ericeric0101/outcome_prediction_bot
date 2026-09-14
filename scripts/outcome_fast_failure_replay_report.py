import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.outcome_fast_failure_replay_report import main

if __name__ == "__main__":
    main()
