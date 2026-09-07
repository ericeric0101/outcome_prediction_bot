"""Read-only outcome report for compact trend-continuation candidate paths."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any


_UPSIDE = (Decimal("0.01"), Decimal("0.02"), Decimal("0.05"))
_DOWNSIDE = (Decimal("-0.05"), Decimal("-0.10"))


def report(db_path: str | Path, *, period: str = "1d") -> dict[str, Any]:
    """Summarise compact candidate BBO paths without inferring maker fills."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            "SELECT ts, payload_json FROM strategy_events "
            "WHERE event_type='OUTCOME_TREND_CONTINUATION_PATH' ORDER BY id"
        ).fetchall()
    paths: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    excluded = 0
    for ts, raw in rows:
        try:
            payload = json.loads(raw or "{}")
            if payload.get("period") != period:
                continue
            episode_id = str(payload["episode_id"])
            Decimal(str(payload["gross_bid_return_pct"]))
        except (KeyError, TypeError, ValueError, ArithmeticError, json.JSONDecodeError):
            excluded += 1
            continue
        paths.setdefault(episode_id, []).append((str(ts), payload))

    episodes: list[dict[str, Any]] = []
    buckets: Counter[str] = Counter()
    for episode_id, observations in paths.items():
        observations.sort(key=lambda item: item[0])
        first = observations[0][1]
        returns = [Decimal(str(payload["gross_bid_return_pct"])) for _, payload in observations]
        age = max(float(payload.get("age_sec", 0)) for _, payload in observations)
        terminal_observed = any(bool(payload.get("terminal_observation")) for _, payload in observations)
        row: dict[str, Any] = {
            "episode_id": episode_id, "outcome_id": first.get("outcome_id"),
            "side_index": first.get("side_index"), "observations": len(observations),
            "observed_horizon_sec": age, "terminal_observation": terminal_observed,
            "mae_gross_bid_pct": str(min(returns)),
            "mfe_gross_bid_pct": str(max(returns)),
            "hit_upside": {f"{int(value * 100)}%": any(item >= value for item in returns) for value in _UPSIDE},
            "hit_downside": {f"{int(abs(value) * 100)}%": any(item <= value for item in returns) for value in _DOWNSIDE},
        }
        episodes.append(row)
        # Do not infer a completed horizon merely because an old row happened
        # to be close to 7200 seconds.  Schema-v2 terminal rows are explicit
        # evidence that a valid BBO was captured after the horizon.
        buckets["observed_2h" if terminal_observed else "partial_under_2h"] += 1
    return {
        "report": "outcome_trend_continuation_counterfactual_bbo_path",
        "schema_version": 2,
        "period": period,
        "episodes": episodes,
        "episode_count": len(episodes),
        "coverage": dict(buckets),
        "excluded_invalid_rows": excluded,
        "limits": [
            "This is a public BBO counterfactual, not a maker-fill simulation or realised PnL.",
            "Only post-deployment compact candidate paths are included; legacy periods are not backfilled.",
            "A live continuation-policy promotion requires completed, independent daily-market episodes and fill-quality evidence.",
        ],
        "ready_for_policy_promotion": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome trend-continuation candidate report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(report(args.db, period=args.period), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
