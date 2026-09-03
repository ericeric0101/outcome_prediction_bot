"""Read-only comparison of pre-registered Outcome S0 entry-gate variants.

The report intentionally does not manufacture fills or PnL for a gate that
wasn't live.  It measures opportunity/frequency only and separately reports
the small set of real baseline submissions and official fills.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


VARIANTS = ("spot_mark_oi", "spot_mark", "spot_mark_or_oi")
BASELINE = "spot_mark_oi"


def _empty_variant() -> dict[str, Any]:
    return {"eligible_observations": 0, "up": 0, "down": 0, "added_vs_baseline": 0}


def as_dict(db_path: str | Path, *, period: str = "1d", recent_event_limit: int = 50_000) -> dict[str, Any]:
    """Summarise stored counterfactual gate decisions without exchange access."""
    path = Path(db_path).resolve()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        latest_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM strategy_events").fetchone()[0])
        first_id = max(1, latest_id - recent_event_limit + 1)
        rows = conn.execute(
            """
            SELECT payload_json FROM strategy_events
            WHERE id >= ? AND event_type='OUTCOME_ENTRY_ADMISSION_DECISION'
            ORDER BY id
            """, (first_id,)
        ).fetchall()
        fill_order_ids = {
            str(row[0]) for row in conn.execute(
                "SELECT venue_order_id FROM order_events WHERE event_type='ORDER_FILLED' AND side='BUY'"
            ).fetchall() if row[0] is not None
        }

    variants = {name: _empty_variant() for name in VARIANTS}
    final_reasons: Counter[str] = Counter()
    observed = baseline_eligible = baseline_submitted = baseline_filled = 0
    missing_variant_payload = 0
    for (raw,) in rows:
        try:
            payload = json.loads(raw or "{}")
            if payload.get("period") != period:
                continue
            evidence = payload.get("raw_signal_evidence")
            alternatives = evidence.get("gate_variants") if isinstance(evidence, dict) else None
            if not isinstance(alternatives, dict):
                missing_variant_payload += 1
                continue
        except (TypeError, ValueError, json.JSONDecodeError):
            missing_variant_payload += 1
            continue
        observed += 1
        baseline = alternatives.get(BASELINE)
        baseline_ok = isinstance(baseline, dict) and bool(baseline.get("eligible"))
        for name in VARIANTS:
            item = alternatives.get(name)
            if not isinstance(item, dict) or not bool(item.get("eligible")):
                continue
            variants[name]["eligible_observations"] += 1
            if item.get("side_index") == 0:
                variants[name]["up"] += 1
            elif item.get("side_index") == 1:
                variants[name]["down"] += 1
            if name != BASELINE and not baseline_ok:
                variants[name]["added_vs_baseline"] += 1
        if not baseline_ok:
            continue
        baseline_eligible += 1
        final_reasons[str(payload.get("final_reason") or "unknown")] += 1
        if bool(payload.get("execution_submitted")):
            baseline_submitted += 1
            if str(payload.get("order_id") or "") in fill_order_ids:
                baseline_filled += 1

    return {
        "report": "outcome_s0_entry_gate_ablation",
        "schema_version": 1,
        "period": period,
        "strategy_event_id_window": {"first_id": first_id, "last_id": latest_id},
        "admission_observations_with_variants": observed,
        "admission_observations_missing_variants": missing_variant_payload,
        "variants": variants,
        "live_baseline": {
            "eligible_observations": baseline_eligible,
            "submitted_orders": baseline_submitted,
            "official_buy_fills": baseline_filled,
            "final_reasons_for_baseline_eligible": dict(sorted(final_reasons.items())),
        },
        # Counterfactual gates never sent an order.  This is a hard report
        # boundary, not an inference about their profitability.
        "counterfactual_limits": [
            "Non-baseline variants have no actual maker-fill probability, markout, holding time, or PnL.",
            "Observations from the same daily contract are correlated and do not authorize a live gate change.",
        ],
        "ready_for_live_gate_change": False,
    }
