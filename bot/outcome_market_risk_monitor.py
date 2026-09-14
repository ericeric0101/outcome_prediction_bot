"""Read-only market-risk state for an already-owned Outcome lifecycle.

It deliberately has no account, gateway, SDK, controller, or mutation import.
Its output is research authorization only; the existing exit lifecycle remains
the sole owner of cancel/rebook/IOC execution.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class OutcomeMarketRiskObservation:
    lifecycle_id: str
    outcome_id: int
    coin: str
    period: str
    timestamp: float
    entry_price: Decimal
    position_size: Decimal
    best_bid: Decimal
    best_ask: Decimal
    top1_depth: Decimal
    top3_depth: Decimal
    side_index: int
    time_left_sec: float | None
    entry_time_left_sec: float | None
    spot_strike_bps: Decimal | None
    mark_return_bps: Decimal | None
    oi_return_bps: Decimal | None
    oi_age_ms: int | None
    reversal_state: str | None
    independent_confirmation_count: int
    full_inventory_executable_vwap: Decimal | None = None


class OutcomeMarketRiskMonitor:
    """Bounded in-memory monitor; every returned decision is shadow-only."""

    _LOOKBACK_SEC = 90.0
    _HORIZONS = (5, 10, 30)

    def __init__(self) -> None:
        self._samples: dict[str, deque[tuple[float, Decimal, Decimal]]] = {}
        self._dislocation_started: dict[str, float] = {}
        self._warning_started: dict[str, float] = {}
        self._latest_lane: dict[str, dict[str, Any]] = {}
        self._latest_full_depth_lane: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _prior(samples: deque[tuple[float, Decimal, Decimal]], now: float, horizon: int) -> tuple[Decimal, Decimal] | None:
        candidates = [row for row in samples if row[0] <= now - horizon]
        if not candidates:
            return None
        _, bid, depth = candidates[-1]
        return bid, depth

    @staticmethod
    def _bps(current: Decimal, prior: Decimal | None) -> Decimal | None:
        if prior is None or prior <= 0:
            return None
        return (current / prior - Decimal("1")) * Decimal("10000")

    def observe(self, item: OutcomeMarketRiskObservation) -> dict[str, Any]:
        samples = self._samples.setdefault(item.lifecycle_id, deque())
        samples.append((item.timestamp, item.best_bid, item.top3_depth))
        while samples and samples[0][0] < item.timestamp - self._LOOKBACK_SEC:
            samples.popleft()
        velocity, ratios = {}, {}
        for horizon in self._HORIZONS:
            previous = self._prior(samples, item.timestamp, horizon)
            prior_bid, prior_depth = previous if previous is not None else (None, None)
            velocity[str(horizon)] = self._bps(item.best_bid, prior_bid)
            ratios[str(horizon)] = item.top3_depth / prior_depth if prior_depth and prior_depth > 0 else None
        executable_price = item.full_inventory_executable_vwap or item.best_bid
        executable_return = executable_price / item.entry_price - Decimal("1") if item.entry_price > 0 else None
        direction = Decimal("1") if item.side_index == 0 else Decimal("-1")
        thesis_still_same_side = (
            item.spot_strike_bps is not None and direction * item.spot_strike_bps > 0
        )
        v30, d30 = velocity["30"], ratios["30"]
        dislocated = bool(executable_return is not None and executable_return <= Decimal("-0.05") and thesis_still_same_side)
        if dislocated:
            started = self._dislocation_started.setdefault(item.lifecycle_id, item.timestamp)
        else:
            self._dislocation_started.pop(item.lifecycle_id, None)
            started = None
        severe = bool(executable_return is not None and executable_return <= Decimal("-0.10")
                      and v30 is not None and v30 <= Decimal("-250")
                      and d30 is not None and d30 <= Decimal("0.70"))
        lane_signals: list[str] = []
        if executable_return is not None and executable_return <= Decimal("-0.05"):
            lane_signals.append("executable_drawdown")
        if v30 is not None and v30 <= Decimal("-250"):
            lane_signals.append("bid_velocity")
        if d30 is not None and d30 <= Decimal("0.70"):
            lane_signals.append("top3_depth_depletion")
        if item.reversal_state == "REVERSAL_CONFIRMED":
            lane_signals.append("reversal_confirmed")
        multi_signal = len(lane_signals) >= 2
        if multi_signal:
            warning_started = self._warning_started.setdefault(item.lifecycle_id, item.timestamp)
        else:
            self._warning_started.pop(item.lifecycle_id, None)
            warning_started = None
        warning_persistent = bool(warning_started is not None and item.timestamp - warning_started >= 10.0)
        lane_state = "WARNING_CANDIDATE" if warning_persistent else (
            "WARNING_BUILDING" if multi_signal else "NORMAL"
        )
        # This fixed research bucket has not passed tail/winner validation;
        # avoid naming it like a production protection authorization.
        state = "SEVERE_DISLOCATION_RESEARCH" if severe else (
            "RISK_COMPRESSION_SHADOW" if dislocated else "NORMAL"
        )
        result = {
            "schema_version": 1, "read_only": True, "live_authority": False,
            "execution_submitted": False, "outcome_id": item.outcome_id,
            "period": item.period, "coin": item.coin, "entry_lifecycle_id": item.lifecycle_id,
            "timestamp": item.timestamp,
            "entry_price": str(item.entry_price), "position_size": str(item.position_size),
            "entry_time_left_sec": item.entry_time_left_sec, "current_time_left_sec": item.time_left_sec,
            "full_inventory_executable_vwap": str(item.full_inventory_executable_vwap) if item.full_inventory_executable_vwap is not None else None,
            "executable_return_pct": str(executable_return) if executable_return is not None else None,
            "best_bid": str(item.best_bid), "best_ask": str(item.best_ask),
            "spread_bps": str((item.best_ask / item.best_bid - Decimal("1")) * Decimal("10000")) if item.best_bid > 0 else None,
            "top1_depth": str(item.top1_depth), "top3_depth": str(item.top3_depth),
            "bid_velocity_bps": {key: str(value) if value is not None else None for key, value in velocity.items()},
            "depth_ratio_vs_baseline": {key: str(value) if value is not None else None for key, value in ratios.items()},
            "spot_strike_bps": str(item.spot_strike_bps) if item.spot_strike_bps is not None else None,
            "mark_return_bps": str(item.mark_return_bps) if item.mark_return_bps is not None else None,
            "oi_return_bps": str(item.oi_return_bps) if item.oi_return_bps is not None else None,
            "oi_age_ms": item.oi_age_ms, "reversal_state": item.reversal_state,
            "independent_confirmation_count": item.independent_confirmation_count,
            "dislocation_start_ts": started,
            "dislocation_duration_sec": (item.timestamp - started) if started is not None else 0.0,
            "state": state,
            "would_compress_target": state != "NORMAL",
            "would_aggressively_reprice": state != "NORMAL",
            "would_freeze_additional_exposure": state != "NORMAL",
            "fast_failure_lane_shadow": {
                "state": lane_state,
                "signals": lane_signals,
                "signal_count": len(lane_signals),
                "persistence_sec": (item.timestamp - warning_started) if warning_started is not None else 0.0,
                "hard_drawdown_reached": bool(executable_return is not None and executable_return <= Decimal("-0.10")),
                "requires_fresh_full_inventory_depth": True,
                "live_authority": False,
            },
            "limits": ["shadow only", "no mutation authority", "full depth is null when WS only exposes top-of-book"],
        }
        self._latest_lane[item.lifecycle_id] = result
        return result

    def assess_full_depth(
        self, *, lifecycle_id: str, timestamp: float, entry_price: Decimal,
        full_inventory_vwap: Decimal | None, full_inventory: bool,
        taker_close_fee_rate: Decimal | None,
    ) -> dict[str, Any]:
        """Attach fresh REST full-depth facts to the latest WS shadow lane.

        This has no controller reference.  It is deliberately the last
        read-only step before any future live-canary proposal can be judged.
        """
        latest = self._latest_lane.get(lifecycle_id)
        lane = dict(latest.get("fast_failure_lane_shadow") or {}) if isinstance(latest, dict) else {}
        if not lane:
            return {"state": "NO_WS_LANE_CONTEXT", "live_authority": False, "execution_submitted": False}
        net_return = None
        if full_inventory and full_inventory_vwap is not None and taker_close_fee_rate is not None and entry_price > 0:
            net_return = full_inventory_vwap * (Decimal("1") - taker_close_fee_rate) / entry_price - Decimal("1")
        within_cap = bool(net_return is not None and net_return >= Decimal("-0.15"))
        hard = bool(lane.get("state") == "WARNING_CANDIDATE" and lane.get("hard_drawdown_reached")
                    and full_inventory and within_cap)
        result = {
            "state": "HARD_CANDIDATE_WITHIN_CAP" if hard else (
                "HARD_CANDIDATE_DEPTH_OR_CAP_BLOCKED" if lane.get("hard_drawdown_reached") else str(lane.get("state"))
            ),
            "ws_lane": lane,
            "full_inventory": full_inventory,
            "full_depth_net_return_pct": str(net_return) if net_return is not None else None,
            "within_minus_15pct_cap": within_cap,
            "observed_at": timestamp,
            "live_authority": False,
            "execution_submitted": False,
            "limits": ["shadow only", "does_not_cancel_protection", "does_not_submit_ioc"],
        }
        self._latest_full_depth_lane[lifecycle_id] = result
        return result

    def latest_hard_candidate(self, *, lifecycle_id: str, now: float, max_age_sec: float = 15.0) -> dict[str, Any] | None:
        """Return only a fresh, cap-eligible shadow fact; never an order intent."""
        item = self._latest_full_depth_lane.get(lifecycle_id)
        if item is None:
            return None
        try:
            age = now - float(item["observed_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if age < 0 or age > max_age_sec or item.get("state") != "HARD_CANDIDATE_WITHIN_CAP":
            return None
        return dict(item)

    def latest_persistent_lane(self, *, lifecycle_id: str, now: float, max_age_sec: float = 15.0) -> dict[str, Any] | None:
        """Return a recent WS-qualified lane before a fresh L2 depth read.

        Holding-path rows arrive at a deliberately low cadence, so an old
        full-depth observation cannot satisfy the live canary's freshness
        boundary.  The caller must still fetch and walk fresh L2 depth.
        """
        item = self._latest_lane.get(lifecycle_id)
        if item is None:
            return None
        try:
            age = now - float(item["timestamp"])
            lane = item["fast_failure_lane_shadow"]
        except (KeyError, TypeError, ValueError):
            return None
        if age < 0 or age > max_age_sec or not isinstance(lane, dict):
            return None
        if lane.get("state") != "WARNING_CANDIDATE" or not lane.get("hard_drawdown_reached"):
            return None
        return dict(lane)
