"""Execution-quality guard for the relaxed (spot+mark) Tier-B entry.

Tier B deliberately relaxes OI direction, not market quality.  Its limits are
derived from compact prior Tier-B submit audits once enough observations exist;
until then the fixed bootstrap ceilings are conservative and require no human
environment tuning.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Iterable


def _decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result >= 0 else None
    except (ArithmeticError, ValueError):
        return None


def _level_size(level: object) -> Decimal | None:
    if not isinstance(level, dict):
        return None
    for key in ("size", "sz", "amount", "quantity"):
        value = _decimal(level.get(key))
        if value is not None:
            return value
    return None


def _percentile(values: list[Decimal], fraction: Decimal) -> Decimal:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * float(fraction))))
    return ordered[index]


@dataclass(frozen=True)
class TierBExecutionPolicy:
    max_spread_bps: Decimal
    min_top_depth_shares: Decimal
    max_submit_drift_bps: Decimal
    sample_count: int
    source: str


@dataclass(frozen=True)
class TierBExecutionDecision:
    allowed: bool
    reason: str
    policy: TierBExecutionPolicy
    spread_bps: Decimal | None
    top_depth_shares: Decimal | None
    decision_bid: Decimal | None
    max_submit_bid: Decimal | None
    safe_max_shares: Decimal | None = None
    recent_trade_shares: Decimal | None = None


class OutcomeTierBExecutionGate:
    """Bounded all-tier entry quality and capacity gate.

    The historical class name remains for journal compatibility.  The caller
    now uses the same strict bootstrap bounds for Tier A and Tier B.
    """

    BOOTSTRAP_MAX_SPREAD_BPS = Decimal("125")
    BOOTSTRAP_MAX_SUBMIT_DRIFT_BPS = Decimal("25")
    BOOTSTRAP_DEPTH_MULTIPLE = Decimal("1.25")
    MIN_CALIBRATION_SAMPLES = 20

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _recent_trade_shares(self, coin: str | None) -> Decimal | None:
        """Deduplicate observed public trades in the latest compact 5m window.

        A missing WS trade feed is not fabricated as zero liquidity; fresh L2
        remains the hard capacity source in that case and the absence is kept
        auditable as ``None``.
        """
        if not coin:
            return None
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                rows = conn.execute(
                    """SELECT payload_json FROM strategy_events
                       WHERE event_type='OUTCOME_WS_TRADES' AND julianday(ts)>=julianday('now','-5 minutes')
                       ORDER BY id DESC LIMIT 300"""
                ).fetchall()
        except sqlite3.Error:
            return None
        seen: set[str] = set(); total = Decimal("0"); found = False
        for (raw,) in rows:
            try:
                payload = json.loads(raw or "{}")
                data = payload.get("raw", {}).get("data", [])
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict) or str(item.get("coin")) != coin:
                        continue
                    key = str(item.get("tid") or item.get("tradeId") or item.get("hash") or "")
                    key = key or f"{item.get('time')}:{item.get('px')}:{item.get('sz')}:{item.get('side')}"
                    if key in seen:
                        continue
                    size = _level_size(item)
                    if size is not None and size > 0:
                        seen.add(key); total += size; found = True
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return total if found else None

    def policy(self, *, requested_shares: Decimal) -> TierBExecutionPolicy:
        # Historical audit rows are intentionally small.  They contain only
        # the BBO/depth/drift facts of a Tier-B attempt, never all-market WS.
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                rows = conn.execute(
                    """SELECT payload_json FROM order_events
                       WHERE event_type='ORDER_SUBMIT'
                         AND (
                           json_extract(payload_json, '$.audit.entry_execution_tier') IN ('tier_a_spot_mark_oi', 'tier_b_spot_mark')
                           OR json_extract(payload_json, '$.audit.entry_tier')='tier_b_spot_mark'
                         )
                       ORDER BY id DESC LIMIT 200"""
                ).fetchall()
        except sqlite3.Error:
            rows = []
        spreads: list[Decimal] = []
        drifts: list[Decimal] = []
        depths: list[Decimal] = []
        for (raw,) in rows:
            try:
                audit = json.loads(raw).get("audit", {})
                spread, drift, depth = (_decimal(audit.get("entry_spread_bps", audit.get("tier_b_spread_bps"))),
                                        _decimal(audit.get("entry_submit_drift_bps", audit.get("tier_b_submit_drift_bps"))),
                                        _decimal(audit.get("entry_top3_depth_shares", audit.get("tier_b_top_depth_shares"))))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if spread is not None and drift is not None and depth is not None:
                spreads.append(spread); drifts.append(drift); depths.append(depth)
        if len(spreads) < self.MIN_CALIBRATION_SAMPLES:
            return TierBExecutionPolicy(
                self.BOOTSTRAP_MAX_SPREAD_BPS,
                requested_shares * self.BOOTSTRAP_DEPTH_MULTIPLE,
                self.BOOTSTRAP_MAX_SUBMIT_DRIFT_BPS,
                len(spreads), "bootstrap_bounded_until_20_tier_b_submits",
            )
        # Calibration can tighten a limit but never relax the bootstrap hard
        # ceiling.  This prevents a run of poor fills from normalising thin
        # books or delayed price chasing.
        return TierBExecutionPolicy(
            min(self.BOOTSTRAP_MAX_SPREAD_BPS, _percentile(spreads, Decimal("0.90"))),
            max(requested_shares, _percentile(depths, Decimal("0.10"))),
            min(self.BOOTSTRAP_MAX_SUBMIT_DRIFT_BPS, _percentile(drifts, Decimal("0.90"))),
            len(spreads), "bounded_p10_p90_tier_b_submit_history",
        )

    def evaluate(self, *, bid: Decimal | None, ask: Decimal | None,
                 bid_levels: Iterable[object], requested_shares: Decimal,
                 coin: str | None = None) -> TierBExecutionDecision:
        policy = self.policy(requested_shares=requested_shares)
        if bid is None or ask is None or bid <= 0 or ask <= bid:
            return TierBExecutionDecision(False, "tier_b_invalid_bbo", policy, None, None, bid, None)
        midpoint = (bid + ask) / Decimal("2")
        spread_bps = (ask - bid) / midpoint * Decimal("10000")
        sizes = [_level_size(level) for level in list(bid_levels)[:3]]
        if any(size is None for size in sizes):
            return TierBExecutionDecision(False, "tier_b_depth_unavailable", policy, spread_bps, None, bid, None)
        depth = sum((size for size in sizes if size is not None), Decimal("0"))
        # A visible L2 level is not all executable capacity.  Retain the
        # existing 1.25x safety multiple and round down to whole shares.
        safe_max_shares = (depth / self.BOOTSTRAP_DEPTH_MULTIPLE).to_integral_value(rounding=ROUND_FLOOR)
        recent_trade_shares = self._recent_trade_shares(coin)
        if recent_trade_shares is not None:
            # At most one quarter of recently observed, de-duplicated public
            # flow may be treated as complementary capacity.  This cannot
            # increase the book-derived ceiling, only tighten it.
            flow_cap = (recent_trade_shares / Decimal("4")).to_integral_value(rounding=ROUND_FLOOR)
            safe_max_shares = min(safe_max_shares, flow_cap)
        max_submit_bid = bid * (Decimal("1") + policy.max_submit_drift_bps / Decimal("10000"))
        if spread_bps > policy.max_spread_bps:
            return TierBExecutionDecision(False, "entry_spread_exceeds_calibrated_ceiling", policy, spread_bps, depth, bid, max_submit_bid, safe_max_shares, recent_trade_shares)
        if safe_max_shares <= 0:
            return TierBExecutionDecision(False, "tier_b_safe_capacity_zero", policy, spread_bps, depth, bid, max_submit_bid, safe_max_shares, recent_trade_shares)
        return TierBExecutionDecision(True, "tier_b_execution_quality_confirmed", policy, spread_bps, depth, bid, max_submit_bid, safe_max_shares, recent_trade_shares)
