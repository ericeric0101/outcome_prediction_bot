"""B5 unseen-live shadow report for the active challenger.

The report reads immutable shadow decisions and later derived BBO observations.
It never treats a maker quote as a fill, never reconstructs hypothetical PnL,
and never grants B6 live authority.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from bot.outcome_oi_features import FEATURE_SCHEMA_VERSION


HORIZONS_SEC = (300, 900, 3600)
TAKER_OPEN_FEE = Decimal("0.0007")
TAKER_CLOSE_FEE = Decimal("0.0004")
EARLY_PROPOSAL_MAX_GAP_SEC = 90
EARLY_PROPOSAL_LOOKBACK_SEC = 15 * 60


def _timestamp_ms(value: object) -> int | None:
    try:
        text = str(value).replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None


def _decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _load_future_bbo(conn: sqlite3.Connection, *, period: str) -> dict[int, list[tuple[int, dict[str, Any]]]]:
    rows = conn.execute(
        """SELECT outcome_id,snapshot_timestamp_ms,features_json
           FROM outcome_oi_feature_rows
           WHERE feature_schema_version=? AND period=? AND oi_backfilled=0
           ORDER BY outcome_id,snapshot_timestamp_ms""",
        (FEATURE_SCHEMA_VERSION, period),
    ).fetchall()
    output: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for raw_outcome, raw_timestamp, raw_features in rows:
        try:
            features = json.loads(raw_features)
            outcome_id, timestamp_ms = int(raw_outcome), int(raw_timestamp)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(features, dict):
            output.setdefault(outcome_id, []).append((timestamp_ms, features))
    return output


def _future_row(rows: list[tuple[int, dict[str, Any]]], target_ms: int, *, tolerance_ms: int) -> dict[str, Any] | None:
    timestamps = [row[0] for row in rows]
    position = bisect.bisect_left(timestamps, target_ms)
    candidates = rows[max(0, position - 1) : min(len(rows), position + 2)]
    if not candidates:
        return None
    timestamp, features = min(candidates, key=lambda row: abs(row[0] - target_ms))
    return features if abs(timestamp - target_ms) <= tolerance_ms else None


def _selected_trained_through(payload: Mapping[str, Any], side_index: int | None) -> int | None:
    scores = payload.get("scores")
    score = scores.get(str(side_index)) if isinstance(scores, Mapping) and side_index in (0, 1) else None
    try:
        return int(score.get("artifact_trained_through_ms")) if isinstance(score, Mapping) else None
    except (TypeError, ValueError):
        return None


def _side_ask(observed: Mapping[str, Any], side_index: int) -> Decimal | None:
    return _decimal(observed.get("yes_ask" if side_index == 0 else "no_ask"))


def _side_future_bid(future: Mapping[str, Any], side_index: int) -> Decimal | None:
    return _decimal(future.get("yes_bid" if side_index == 0 else "no_bid"))


def _taker_return(*, ask: Decimal | None, future_bid: Decimal | None) -> Decimal | None:
    """Strict executable counterfactual: buy now at ask, sell later at bid.

    It is intentionally usable for a shadow ``JOIN_BEST_BID`` proposal only
    when answering the separate question "what if we crossed now?"  It never
    re-labels the original maker quote as a fill.
    """
    if ask is None or future_bid is None or ask <= 0 or future_bid <= 0:
        return None
    return future_bid * (Decimal("1") - TAKER_CLOSE_FEE) / (ask * (Decimal("1") + TAKER_OPEN_FEE)) - Decimal("1")


def shadow_report(
    db_path: str | Path,
    *,
    period: str = "1d",
    tolerance_sec: int = 120,
) -> dict[str, Any]:
    path = Path(db_path)
    empty = {
        "report": "outcome_active_unseen_shadow",
        "schema_version": 1,
        "entry_events": 0,
        "unseen_entry_events": 0,
        "holding_events": 0,
        "ready_for_live": False,
        "live_authority": False,
        "blockers": ["b5_unseen_shadow_evidence_required", "b6_requires_separate_operator_authorization"],
    }
    if not path.exists():
        return empty
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"strategy_events", "outcome_oi_feature_rows"}.issubset(tables):
            return empty
        raw_events = conn.execute(
            """SELECT ts,event_type,payload_json FROM strategy_events
               WHERE event_type IN ('OUTCOME_ACTIVE_CHALLENGER_SHADOW','OUTCOME_ACTIVE_HOLDING_CHALLENGER_SHADOW')
               ORDER BY id"""
        ).fetchall()
        future_bbo = _load_future_bbo(conn, period=period)

    action_counts: Counter[str] = Counter()
    all_action_counts: Counter[str] = Counter()
    action_counts_by_day_type: dict[str, Counter[str]] = {"weekday": Counter(), "weekend": Counter(), "unknown": Counter()}
    unseen_by_day_type: Counter[str] = Counter()
    production_reason_counts: Counter[str] = Counter()
    holding_action_counts: Counter[str] = Counter()
    unseen_markets: set[int] = set()
    horizon_returns: dict[int, list[Decimal]] = {horizon: [] for horizon in HORIZONS_SEC}
    horizon_returns_by_day_type: dict[int, dict[str, list[Decimal]]] = {
        horizon: {"weekday": [], "weekend": [], "unknown": []} for horizon in HORIZONS_SEC
    }
    # A JOIN quote is maker-only in the original challenger proposal.  Keep a
    # separate, explicitly taker-at-ask calculation for the user's execution
    # question; never merge it into maker opportunity returns above.
    taker_returns_by_action: dict[int, dict[str, list[Decimal]]] = {
        horizon: defaultdict(list) for horizon in HORIZONS_SEC
    }
    same_time_returns: dict[int, dict[str, dict[str, list[Decimal]]]] = {
        horizon: {
            "same_side": {"production": [], "challenger": []},
            "opposite_side": {"production": [], "challenger": []},
        }
        for horizon in HORIZONS_SEC
    }
    unseen_records: list[dict[str, Any]] = []
    horizon_coverage: Counter[int] = Counter()
    entry_events = unseen_entry_events = holding_events = 0
    for raw_ts, event_type, raw_payload in raw_events:
        try:
            payload = json.loads(raw_payload or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or str(payload.get("period")) != period:
            continue
        challenger = payload.get("challenger")
        challenger = challenger if isinstance(challenger, Mapping) else {}
        action = str(challenger.get("action") or "UNKNOWN")
        if event_type == "OUTCOME_ACTIVE_HOLDING_CHALLENGER_SHADOW":
            holding_events += 1
            holding_action_counts[action] += 1
            continue
        entry_events += 1
        all_action_counts[action] += 1
        try:
            outcome_id = int(payload.get("outcome_id"))
            side_index = int(challenger.get("side_index"))
        except (TypeError, ValueError):
            continue
        decision_ms = _timestamp_ms(raw_ts)
        if decision_ms is None or side_index not in (0, 1):
            continue
        trained_through = _selected_trained_through(payload, side_index)
        if trained_through is None or decision_ms <= trained_through:
            continue
        unseen_entry_events += 1
        unseen_markets.add(outcome_id)
        action_counts[action] += 1
        production_reason_counts[str(payload.get("production_reason") or "unknown")] += 1
        observed = payload.get("observed_context")
        observed = observed if isinstance(observed, Mapping) else {}
        try:
            production_side = int(payload.get("production_side_index"))
        except (TypeError, ValueError):
            production_side = None
        if production_side not in (0, 1):
            production_side = None
        raw_weekend = observed.get("market_session_is_weekend")
        day_type = "weekend" if raw_weekend is True else "weekday" if raw_weekend is False else "unknown"
        unseen_by_day_type[day_type] += 1
        action_counts_by_day_type[day_type][action] += 1
        prefix = "yes" if side_index == 0 else "no"
        ask = _decimal(observed.get(f"{prefix}_ask"))
        quote = _decimal(challenger.get("quote"))
        record: dict[str, Any] = {
            "outcome_id": outcome_id, "timestamp_ms": decision_ms,
            "challenger_side": side_index, "challenger_action": action,
            "production_side": production_side,
            "production_reason": str(payload.get("production_reason") or "unknown"),
            "ask": ask, "observed": observed, "taker_returns": {},
        }
        unseen_records.append(record)
        for horizon in HORIZONS_SEC:
            future = _future_row(future_bbo.get(outcome_id, []), decision_ms + horizon * 1000, tolerance_ms=max(1, tolerance_sec) * 1000)
            future_bid = _decimal(future.get(f"{prefix}_bid")) if future is not None else None
            if future_bid is None or future_bid <= 0:
                continue
            # This is the requested JOIN-as-taker calculation.  It always
            # crosses at the observed ask, regardless of the shadow's maker
            # quote, then closes at the observed future bid after both fees.
            taker_return = _taker_return(ask=ask, future_bid=future_bid)
            if taker_return is not None:
                record["taker_returns"][horizon] = taker_return
                taker_returns_by_action[horizon][action].append(taker_return)
            if production_side in (0, 1) and action != "WAIT":
                production_return = _taker_return(
                    ask=_side_ask(observed, production_side),
                    future_bid=_side_future_bid(future, production_side),
                )
                if taker_return is not None and production_return is not None:
                    comparison = "same_side" if production_side == side_index else "opposite_side"
                    same_time_returns[horizon][comparison]["production"].append(production_return)
                    same_time_returns[horizon][comparison]["challenger"].append(taker_return)

            # Marketable actions use the observed ask; WAIT uses that same
            # executable counterfactual basis.  Maker actions remain
            # conditional quote opportunities and are never classified as
            # fills.  This legacy maker-path output is intentionally
            # separate from the taker calculation above.
            basis = ask if action in {"WAIT", "BOUNDED_MARKETABLE_BUY"} else quote
            if basis is None or basis <= 0:
                continue
            open_fee = Decimal("0.0007") if action in {"WAIT", "BOUNDED_MARKETABLE_BUY"} else Decimal("0")
            cost = basis * (Decimal("1") + open_fee)
            horizon_coverage[horizon] += 1
            net_return = future_bid * (Decimal("1") - Decimal("0.0004")) / cost - Decimal("1")
            horizon_returns[horizon].append(net_return)
            horizon_returns_by_day_type[horizon][day_type].append(net_return)

    path_summary: dict[str, Any] = {}
    for horizon, values in horizon_returns.items():
        path_summary[str(horizon)] = {
            "observations": len(values),
            "mean_fee_adjusted_return": str(sum(values, Decimal("0")) / len(values)) if values else None,
            "positive_rate": sum(value > 0 for value in values) / len(values) if values else None,
            "by_market_session_day_type": {
                day_type: {
                    "observations": len(day_values),
                    "mean_fee_adjusted_return": str(sum(day_values, Decimal("0")) / len(day_values)) if day_values else None,
                    "positive_rate": sum(value > 0 for value in day_values) / len(day_values) if day_values else None,
                }
                for day_type, day_values in horizon_returns_by_day_type[horizon].items()
            },
        }

    def return_summary(values: list[Decimal]) -> dict[str, Any]:
        return {
            "observations": len(values),
            "mean_fee_adjusted_return": str(sum(values, Decimal("0")) / len(values)) if values else None,
            "positive_rate": sum(value > 0 for value in values) / len(values) if values else None,
        }

    # An "early" proposal is a contiguous (<=90-second event gap) active
    # non-WAIT run in the same side immediately before a new S0 confirmation.
    # We do not claim that an intermittent quote hours earlier was a viable
    # faster entry.  Both legs below are taker-at-ask counterfactuals.
    early_head_to_head: list[dict[str, Any]] = []
    by_market: dict[int, list[dict[str, Any]]] = {}
    for record in unseen_records:
        by_market.setdefault(int(record["outcome_id"]), []).append(record)
    active_actions = {"JOIN_BEST_BID", "IMPROVE_ONE_TICK", "BOUNDED_MARKETABLE_BUY"}
    for outcome_id, records in by_market.items():
        records.sort(key=lambda item: int(item["timestamp_ms"]))
        previous_production_side: int | None = None
        for position, production in enumerate(records):
            production_side = production["production_side"]
            if production_side not in (0, 1):
                previous_production_side = None
                continue
            if previous_production_side == production_side:
                continue
            previous_production_side = production_side
            candidates = [
                item for item in records[:position]
                if item["challenger_side"] == production_side
                and item["challenger_action"] in active_actions
                and 0 < int(production["timestamp_ms"]) - int(item["timestamp_ms"]) <= EARLY_PROPOSAL_LOOKBACK_SEC * 1000
            ]
            if not candidates:
                continue
            # The last active observation must itself be close to the S0
            # onset.  A proposal made many minutes before confirmation is not
            # evidence that the challenger was still actionable at the onset.
            if int(production["timestamp_ms"]) - int(candidates[-1]["timestamp_ms"]) > EARLY_PROPOSAL_MAX_GAP_SEC * 1000:
                continue
            # Keep only the last continuous run leading into the S0 onset.
            run = [candidates[-1]]
            for item in reversed(candidates[:-1]):
                if int(run[0]["timestamp_ms"]) - int(item["timestamp_ms"]) > EARLY_PROPOSAL_MAX_GAP_SEC * 1000:
                    break
                run.insert(0, item)
            early = run[0]
            comparison: dict[str, Any] = {
                "outcome_id": outcome_id,
                "side_index": production_side,
                "active_first_ts_ms": early["timestamp_ms"],
                "production_s0_ts_ms": production["timestamp_ms"],
                "lead_sec": (int(production["timestamp_ms"]) - int(early["timestamp_ms"])) / 1000,
                "active_action": early["challenger_action"],
                "active_run_observations": len(run),
                "taker_return_by_horizon": {},
            }
            for horizon in HORIZONS_SEC:
                active_return = early["taker_returns"].get(horizon)
                production_return = production["taker_returns"].get(horizon)
                if active_return is not None and production_return is not None:
                    comparison["taker_return_by_horizon"][str(horizon)] = {
                        "active": str(active_return), "production": str(production_return),
                        "active_minus_production": str(active_return - production_return),
                    }
            early_head_to_head.append(comparison)

    early_leads = [Decimal(str(item["lead_sec"])) for item in early_head_to_head]
    same_time_summary: dict[str, Any] = {}
    for horizon, comparisons in same_time_returns.items():
        same_time_summary[str(horizon)] = {}
        for relationship, legs in comparisons.items():
            production_values = legs["production"]
            challenger_values = legs["challenger"]
            differences = [challenger - production for challenger, production in zip(challenger_values, production_values)]
            same_time_summary[str(horizon)][relationship] = {
                "production_taker": return_summary(production_values),
                "challenger_taker": return_summary(challenger_values),
                "challenger_minus_production": return_summary(differences),
            }

    early_head_to_head_summary: dict[str, Any] = {
        "episodes": len(early_head_to_head),
        "lead_sec": {
            "mean": str(sum(early_leads, Decimal("0")) / len(early_leads)) if early_leads else None,
            "median": str(sorted(early_leads)[len(early_leads) // 2]) if early_leads else None,
            "min": str(min(early_leads)) if early_leads else None,
            "max": str(max(early_leads)) if early_leads else None,
        },
        "by_horizon_sec": {},
    }
    for horizon in HORIZONS_SEC:
        comparisons = [item["taker_return_by_horizon"].get(str(horizon), {}) for item in early_head_to_head]
        active_values = [_decimal(item.get("active")) for item in comparisons]
        production_values = [_decimal(item.get("production")) for item in comparisons]
        differences = [_decimal(item.get("active_minus_production")) for item in comparisons]
        early_head_to_head_summary["by_horizon_sec"][str(horizon)] = {
            "active_taker": return_summary([value for value in active_values if value is not None]),
            "production_taker": return_summary([value for value in production_values if value is not None]),
            "active_minus_production": return_summary([value for value in differences if value is not None]),
        }
    blockers: list[str] = []
    if len(unseen_markets) < 5:
        blockers.append("fewer_than_5_unseen_daily_markets")
    if unseen_entry_events < 200:
        blockers.append("fewer_than_200_unseen_shadow_decisions")
    if not any(horizon_coverage.values()):
        blockers.append("future_bbo_outcome_coverage_unavailable")
    blockers.extend(["b5_evidence_review_not_completed", "b6_requires_separate_operator_authorization"])
    return {
        "report": "outcome_active_unseen_shadow",
        "schema_version": 2,
        "period": period,
        "entry_events": entry_events,
        "unseen_entry_events": unseen_entry_events,
        "holding_events": holding_events,
        "entry_actions": dict(action_counts),
        "unseen_entry_market_session_day_type_counts": dict(unseen_by_day_type),
        "unseen_entry_actions_by_market_session_day_type": {
            day_type: dict(counts) for day_type, counts in action_counts_by_day_type.items()
        },
        "all_entry_actions_operational": dict(all_action_counts),
        "holding_actions": dict(holding_action_counts),
        "production_reason_counts": dict(production_reason_counts),
        "independent_unseen_daily_markets": len(unseen_markets),
        "unseen_outcome_ids": sorted(unseen_markets),
        "future_path_by_horizon_sec": path_summary,
        "taker_at_observed_ask_counterfactual_by_action_by_horizon_sec": {
            str(horizon): {
                action: return_summary(values)
                for action, values in sorted(actions.items())
            }
            for horizon, actions in taker_returns_by_action.items()
        },
        "same_timestamp_s0_vs_active_taker_counterfactual_by_horizon_sec": same_time_summary,
        "early_same_side_active_to_s0_taker_head_to_head": early_head_to_head_summary,
        "head_to_head_semantics": (
            "read_only_bbo_counterfactual: buy at observed ask plus open fee, "
            "then sell at future same-side bid less close fee; it assumes an "
            "immediate BBO fill and excludes displayed-depth, queue, and "
            "market-impact uncertainty. same_side is the only directional "
            "comparison; opposite_side is reported separately."
        ),
        "maker_semantics": "conditional_quote_opportunity_only_never_fill_or_pnl",
        "wait_semantics": "observed_ask_counterfactual_not_an_executed_trade",
        "evidence_floor_met": not any(blocker.startswith("fewer_than_") or blocker.endswith("unavailable") for blocker in blockers),
        "ready_for_live": False,
        "live_authority": False,
        "blockers": blockers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Report B5 unseen active-challenger observations")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--tolerance-sec", type=int, default=120)
    args = parser.parse_args()
    print(json.dumps(shadow_report(args.db, period=args.period, tolerance_sec=args.tolerance_sec), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
