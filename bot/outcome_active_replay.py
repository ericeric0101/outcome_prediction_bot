"""B4 purged market-walk-forward replay for the active challenger.

Only marketable entries receive an executable counterfactual return because
their entry ask and future exit bid are observed.  Maker actions are reported
as quote opportunities; the replay never equates a touched quote with a fill.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.outcome_active_dataset import ActiveDecisionRow, load_decision_rows
from bot.outcome_active_decision import ActiveSideInput, OutcomeActiveActionOptimizer
from bot.outcome_active_model import OutcomeActiveModel, fit_artifact


def _actual_realized_pnl(db_path: str | Path) -> str:
    path = Path(db_path)
    if not path.exists():
        return "0"
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT COALESCE(SUM(CAST(realized_net_usdc AS REAL)),0) FROM outcome_realized_pnl_lots").fetchone()
    return str(Decimal(str(row[0] or 0)))


def replay_report(db_path: str | Path, *, period: str = "1d", sample_interval_sec: int = 60) -> dict[str, Any]:
    rows = load_decision_rows(db_path, period=period, sample_interval_sec=sample_interval_sec)
    by_market: dict[int, list[ActiveDecisionRow]] = {}
    for row in rows:
        by_market.setdefault(row.outcome_id, []).append(row)
    markets = sorted(by_market, key=lambda market: min(row.timestamp_ms for row in by_market[market]))
    optimizer = OutcomeActiveActionOptimizer()
    action_counts: Counter[str] = Counter()
    marketable_returns: list[Decimal] = []
    maker_opportunity_returns: list[Decimal] = []
    fold_reports: list[dict[str, Any]] = []
    for position in range(2, len(markets)):
        train = [row for market in markets[:position] for row in by_market[market]]
        artifact = fit_artifact(train)
        if artifact is None:
            continue
        model = OutcomeActiveModel(artifact=artifact)
        pairs: dict[int, list[ActiveDecisionRow]] = {}
        for row in by_market[markets[position]]:
            pairs.setdefault(row.timestamp_ms, []).append(row)
        fold_actions: Counter[str] = Counter()
        for timestamp in sorted(pairs):
            candidates: list[ActiveSideInput] = []
            source_by_side: dict[int, ActiveDecisionRow] = {}
            for row in pairs[timestamp]:
                midpoint = (row.bid + row.ask) / 2.0
                spread_bps = (row.ask - row.bid) / midpoint * 10_000 if midpoint > 0 else None
                score = model.score(row.vector, spread_bps=spread_bps)
                if score.get("available"):
                    candidates.append(ActiveSideInput(row.side_index, Decimal(str(row.bid)), Decimal(str(row.ask)), score))
                    source_by_side[row.side_index] = row
            decision = optimizer.choose_entry(tuple(candidates), regime=None)
            action_counts[decision.action] += 1
            fold_actions[decision.action] += 1
            if decision.side_index not in source_by_side:
                continue
            source = source_by_side[decision.side_index]
            future_bid = source.targets.get("future_bid_900s")
            if future_bid is None:
                continue
            future = Decimal(str(future_bid)) * (Decimal("1") - Decimal("0.0004"))
            if decision.action == "BOUNDED_MARKETABLE_BUY":
                cost = Decimal(str(source.ask)) * (Decimal("1") + Decimal("0.0007"))
                marketable_returns.append(future / cost - Decimal("1"))
            elif decision.action in {"JOIN_BEST_BID", "IMPROVE_ONE_TICK"} and decision.quote is not None:
                # This is deliberately not a PnL claim: it is the return only
                # if a maker fill had occurred at the proposed quote.
                maker_opportunity_returns.append(future / decision.quote - Decimal("1"))
        fold_reports.append({
            "test_outcome_id": markets[position],
            "train_market_instances": position,
            "test_decision_timestamps": len(pairs),
            "actions": dict(fold_actions),
        })
    blockers = []
    if len(markets) < 5:
        blockers.append("insufficient_independent_daily_markets")
    if not fold_reports:
        blockers.append("walk_forward_replay_unavailable")
    return {
        "report": "outcome_active_challenger_walk_forward_replay",
        "schema_version": 1,
        "period": period,
        "market_instances": len(markets),
        "rows": len(rows),
        "folds": fold_reports,
        "actions": dict(action_counts),
        "marketable_entry_executable_15m": {
            "observations": len(marketable_returns),
            "mean_net_return": str(sum(marketable_returns, Decimal("0")) / len(marketable_returns)) if marketable_returns else None,
            "positive_rate": sum(value > 0 for value in marketable_returns) / len(marketable_returns) if marketable_returns else None,
        },
        "maker_quote_opportunity_not_fill": {
            "observations": len(maker_opportunity_returns),
            "mean_conditional_return": str(sum(maker_opportunity_returns, Decimal("0")) / len(maker_opportunity_returns)) if maker_opportunity_returns else None,
        },
        "actual_canonical_realized_pnl_reference_usdc": _actual_realized_pnl(db_path),
        "limitations": [
            "Every fold trains only on earlier complete daily markets.",
            "Maker quote opportunity returns are conditional diagnostics and are never counted as fills or PnL.",
            "The actual canonical PnL is context, not an apples-to-apples counterfactual allocation.",
        ],
        "ready_for_live": False,
        "blockers": blockers + ["b5_unseen_shadow_evidence_required", "b6_requires_separate_operator_authorization"],
        "live_authority": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run B4 active challenger market-walk-forward replay")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--sample-interval-sec", type=int, default=60)
    args = parser.parse_args()
    print(json.dumps(replay_report(args.db, period=args.period, sample_interval_sec=args.sample_interval_sec), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
