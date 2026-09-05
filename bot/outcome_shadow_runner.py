"""Read-only Outcome shadow runtime.

It intentionally has no execution adapter dependency.  Every network request
made by this module is an ``/info`` read through ``OutcomeClient``; the output
is journal telemetry for validating the existing risk components.
"""
from __future__ import annotations

import time
import uuid
import math
import os
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.forecast_state import ForecastState, build_forecast_state
from bot.lifecycle.outcome_lifecycle import (
    OutcomeMarketSpec,
    discover_btc_15m_markets,
    parse_period_preferences,
    select_configured_btc_market,
    select_active_or_next_btc_market,
)
from bot.outcome_daily_scope import resolve_daily_outcome_scope
from bot.models import SignalDecision
from bot.outcome_account_sync import OutcomeAccountSynchronizer
from bot.outcome_snapshot_bridge import build_outcome_market_snapshot
from bot.outcome_parity import OutcomeParityAnalyzer
from bot.outcome_event_bridge import OutcomeJournalBridge
from bot.outcome_spec_audit import OutcomeSpecAudit
from bot.outcome_ws_recorder import OutcomeWebSocketRecorder
from bot.outcome_p3_pipeline import OutcomeP3Pipeline
from bot.outcome_markout import OutcomeQuote
from bot.outcome_p2_quality import P2_SCHEMA_VERSION, build_p2_capture_quality
from bot.position_manager import PositionManager, PositionManagerConfig
from bot.pricing.outcome_pricing import OutcomePricingState
from bot.signal_engine import SignalEngine, SignalEngineConfig
from execution.exit_policy import ExitPolicy, ExitPolicyConfig
from monitoring.trade_journal_db import TradeJournalDB


@dataclass(frozen=True)
class OutcomeShadowCycle:
    market: Optional[OutcomeMarketSpec]
    account_balance_count: int
    account_open_order_count: int
    account_fill_count: int
    risk_decision_count: int
    error: Optional[str] = None


@dataclass(frozen=True)
class OutcomeShadowTelemetryConfig:
    """Existing forecast/signal policy values required by the shadow runner."""

    sigma_default: Decimal = Decimal("0.60")
    sigma_floor: Decimal = Decimal("0.20")
    sigma_ceiling: Decimal = Decimal("2.00")
    sigma_scale: Decimal = Decimal("1.00")
    time_decay_enabled: bool = True
    time_decay_ref_sec: float = 600.0
    time_decay_min: float = 0.30
    implied_sigma_enabled: bool = True
    realized_vol_window: int = 120
    realized_vol_min_points: int = 20
    min_confidence: float = 0.15
    threshold_up: float = 0.05
    threshold_down: float = 0.05
    min_entry_time_left_sec: int = 300
    btc_ema_fast_sec: float = 3.0
    btc_ema_slow_sec: float = 10.0
    mid_ema_fast_sec: float = 5.0
    mid_ema_slow_sec: float = 20.0
    btc_trend_norm_pct: float = 0.0005
    mid_velocity_reversal: float = 0.010


def build_shadow_telemetry_config(config: Any) -> OutcomeShadowTelemetryConfig:
    """Map the configured legacy strategy policy into the Outcome observer."""
    return OutcomeShadowTelemetryConfig(
        sigma_default=config.maker.digital_sigma_default,
        sigma_floor=config.maker.digital_sigma_floor,
        sigma_ceiling=config.maker.digital_sigma_ceiling,
        sigma_scale=config.maker.digital_vol_scale,
        time_decay_enabled=config.maker.digital_sigma_time_decay_enabled,
        time_decay_ref_sec=config.maker.digital_sigma_time_decay_ref_sec,
        time_decay_min=config.maker.digital_sigma_time_decay_min,
        implied_sigma_enabled=config.maker.implied_sigma_enabled,
        realized_vol_window=config.maker.digital_vol_window,
        realized_vol_min_points=config.maker.digital_vol_min_points,
        min_confidence=config.side.min_confidence,
        threshold_up=config.side.threshold_up,
        threshold_down=config.side.threshold_down,
        min_entry_time_left_sec=config.side.min_time_left_sec,
        btc_ema_fast_sec=config.side.btc_ema_fast_sec,
        btc_ema_slow_sec=config.side.btc_ema_slow_sec,
        mid_ema_fast_sec=config.side.mid_ema_fast_sec,
        mid_ema_slow_sec=config.side.mid_ema_slow_sec,
        btc_trend_norm_pct=config.side.btc_trend_norm_pct,
        mid_velocity_reversal=config.side.mid_velocity_reversal,
    )


def build_shadow_risk_components(config: Any) -> tuple[ExitPolicy, PositionManager, ExitPolicyEngine]:
    """Create the same position/exit policies used by the established strategy.

    The mapping is deliberately explicit so a profile change stays visible in
    the shadow runtime rather than silently falling back to new defaults.
    """
    exit_cfg = config.exit
    return (
        ExitPolicy(ExitPolicyConfig(
            aggressive_stage_sec=exit_cfg.exit_policy_aggressive_stage_sec,
            taker_stage_sec=exit_cfg.exit_policy_taker_stage_sec,
        )),
        PositionManager(PositionManagerConfig(
            early_profit_hold_enabled=exit_cfg.maker_early_profit_hold_enabled,
            early_profit_hold_min_hold_sec=exit_cfg.maker_early_profit_hold_min_hold_sec,
            early_profit_hold_max_profit_ps=exit_cfg.maker_early_profit_hold_max_profit_ps,
            early_profit_hold_min_score_abs=exit_cfg.maker_early_profit_hold_min_score_abs,
            profit_run_enabled=exit_cfg.maker_profit_run_enabled,
            profit_run_min_hold_sec=exit_cfg.maker_profit_run_min_hold_sec,
            profit_run_min_profit_ps=exit_cfg.maker_profit_run_min_profit_ps,
            profit_run_min_score_abs=exit_cfg.maker_profit_run_min_score_abs,
            profit_run_trailing_drawdown_ps=exit_cfg.maker_profit_run_trailing_drawdown_ps,
            profit_run_unlock_profit_ps=exit_cfg.maker_profit_run_unlock_profit_ps,
            profit_run_unlock_trailing_drawdown_ps=exit_cfg.maker_profit_run_unlock_trailing_drawdown_ps,
            stop_loss_entry_protection_sec=exit_cfg.taker_exit_min_hold_sec,
            continuation_entry_protection_sec=max(exit_cfg.taker_exit_min_hold_sec, 60),
            stop_loss_regime_min_sec=8,
            stop_loss_regime_confirmations=2,
            stop_loss_min_opposite_score_abs=exit_cfg.exit_stop_loss_thesis_min_score_abs,
            recycle_locked_side_min_fair_edge_ps=exit_cfg.maker_recycle_locked_side_min_fair_edge_ps,
        )),
        ExitPolicyEngine(ExitEngineConfig(
            hold_to_redeem_enabled=exit_cfg.hold_to_redeem_enabled,
            min_hold_sec=exit_cfg.taker_exit_min_hold_sec,
            stop_loss_usdc=exit_cfg.taker_exit_stop_loss_usdc,
            stop_loss_confirmations=exit_cfg.taker_exit_stop_loss_confirmations,
            stop_loss_requires_thesis_weakening=exit_cfg.exit_stop_loss_requires_thesis_weakening,
            stop_loss_thesis_min_score_abs=exit_cfg.exit_stop_loss_thesis_min_score_abs,
            stop_loss_hold_on_none_signal=exit_cfg.exit_stop_loss_hold_on_none_signal,
            conviction_band_min_price=exit_cfg.exit_conviction_band_min_price,
            hold_band_min_price=exit_cfg.exit_hold_band_min_price,
            conviction_band_min_score_abs=exit_cfg.exit_conviction_band_min_score_abs,
            hold_band_min_score_abs=exit_cfg.exit_hold_band_min_score_abs,
            hold_band_release_min_roi=exit_cfg.exit_hold_band_release_min_roi,
            conviction_stop_loss_multiplier=exit_cfg.exit_conviction_stop_loss_multiplier,
            conviction_extra_confirmations=exit_cfg.exit_conviction_extra_confirmations,
            hold_band_requires_locked=exit_cfg.exit_hold_band_requires_locked,
            early_profit_hold_enabled=exit_cfg.maker_early_profit_hold_enabled,
            early_profit_hold_min_hold_sec=exit_cfg.maker_early_profit_hold_min_hold_sec,
            early_profit_hold_max_profit_ps=exit_cfg.maker_early_profit_hold_max_profit_ps,
            profit_run_enabled=exit_cfg.maker_profit_run_enabled,
            profit_run_min_hold_sec=exit_cfg.maker_profit_run_min_hold_sec,
            profit_run_min_profit_ps=exit_cfg.maker_profit_run_min_profit_ps,
            profit_run_min_score_abs=exit_cfg.maker_profit_run_min_score_abs,
            profit_run_trailing_drawdown_ps=exit_cfg.maker_profit_run_trailing_drawdown_ps,
            profit_run_unlock_profit_ps=exit_cfg.maker_profit_run_unlock_profit_ps,
            profit_run_unlock_trailing_drawdown_ps=exit_cfg.maker_profit_run_unlock_trailing_drawdown_ps,
            recycle_locked_side_min_fair_edge_ps=exit_cfg.maker_recycle_locked_side_min_fair_edge_ps,
            catastrophic_stop_loss_enabled=exit_cfg.catastrophic_stop_loss_enabled,
            catastrophic_stop_loss_usdc=exit_cfg.catastrophic_stop_loss_usdc,
            catastrophic_stop_loss_min_score_abs=exit_cfg.catastrophic_stop_loss_min_score_abs,
            catastrophic_stop_loss_confirmations=exit_cfg.catastrophic_stop_loss_confirmations,
            absolute_max_loss_enabled=exit_cfg.absolute_max_loss_enabled,
            absolute_max_loss_usdc=exit_cfg.absolute_max_loss_usdc,
            absolute_max_loss_min_hold_sec=exit_cfg.absolute_max_loss_min_hold_sec,
        )),
    )


class OutcomeShadowRunner:
    """Collect one or more real Outcome snapshots without planned orders."""

    def __init__(
        self,
        *,
        client: Any,
        wallet_address: str,
        journal: TradeJournalDB,
        exit_policy: ExitPolicy,
        position_manager: PositionManager,
        exit_engine: ExitPolicyEngine,
        fee_rate: Decimal = Decimal("0"),
        slippage_buffer_pct: Decimal = Decimal("0"),
        telemetry_config: OutcomeShadowTelemetryConfig = OutcomeShadowTelemetryConfig(),
        run_id: Optional[str] = None,
        spec_audit: Optional[OutcomeSpecAudit] = None,
        ws_recorder: Optional[OutcomeWebSocketRecorder] = None,
    ) -> None:
        self.client = client
        self.journal = journal
        self.account = OutcomeAccountSynchronizer(client, wallet_address)
        self.pricing = OutcomePricingState()
        self.exit_policy = exit_policy
        self.position_manager = position_manager
        self.exit_engine = exit_engine
        self.fee_rate = fee_rate
        self.slippage_buffer_pct = slippage_buffer_pct
        self.telemetry_config = telemetry_config
        self.signal_engine = SignalEngine(SignalEngineConfig(
            btc_ema_fast_sec=telemetry_config.btc_ema_fast_sec,
            btc_ema_slow_sec=telemetry_config.btc_ema_slow_sec,
            mid_ema_fast_sec=telemetry_config.mid_ema_fast_sec,
            mid_ema_slow_sec=telemetry_config.mid_ema_slow_sec,
            min_confidence=telemetry_config.min_confidence,
            btc_trend_norm_pct=telemetry_config.btc_trend_norm_pct,
            mid_velocity_reversal_threshold=telemetry_config.mid_velocity_reversal,
        ))
        self._spot_history: deque[tuple[float, Decimal]] = deque(maxlen=max(2, telemetry_config.realized_vol_window))
        self._signal_market_id: Optional[int] = None
        self.run_id = run_id or f"outcome-shadow-{uuid.uuid4().hex[:12]}"
        self.spec_audit = spec_audit
        self.ws_recorder = ws_recorder
        self._last_market: Optional[OutcomeMarketSpec] = None
        self.parity_analyzer = OutcomeParityAnalyzer()
        self.fill_bridge = OutcomeJournalBridge(journal, self.run_id)
        self._recorded_fill_ids: set[str] = set()
        self.p3_pipeline = OutcomeP3Pipeline(journal, self.run_id)

    @staticmethod
    def _raw_meta_for_market(meta: dict[str, Any], outcome_id: int) -> Optional[dict[str, Any]]:
        for raw in meta.get("outcomes") or meta.get("universe") or []:
            if not isinstance(raw, dict):
                continue
            raw_id = raw.get("outcome", raw.get("outcomeId"))
            try:
                if int(raw_id) == outcome_id:
                    return raw
            except (TypeError, ValueError):
                continue
        return None

    def _realized_sigma(self) -> Optional[Decimal]:
        if len(self._spot_history) < max(2, self.telemetry_config.realized_vol_min_points):
            return None
        returns = []
        intervals = []
        for (previous_ts, previous), (current_ts, current) in zip(self._spot_history, list(self._spot_history)[1:]):
            if previous <= 0 or current <= 0 or current_ts <= previous_ts:
                continue
            returns.append(math.log(float(current / previous)))
            intervals.append(current_ts - previous_ts)
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        avg_interval = sum(intervals) / len(intervals)
        annualized = math.sqrt(variance) * math.sqrt((365 * 24 * 3600) / avg_interval)
        return Decimal(str(annualized))

    def _forecast_and_signal(
        self, market: OutcomeMarketSpec, up_mid: Decimal, now_ts: float,
    ) -> tuple[ForecastState, SignalDecision, dict[str, Any]]:
        if self._signal_market_id != market.outcome_id:
            self.signal_engine.reset()
            self._spot_history.clear()
            self._signal_market_id = market.outcome_id
        spot = self.pricing.get_btc_mark_price()
        if spot is None:
            raise ValueError("BTC mark is unavailable")
        self._spot_history.append((now_ts, spot))
        self.signal_engine.update_btc_price(spot, now_ts)
        self.signal_engine.update_market_mid(up_mid, now_ts)
        forecast = build_forecast_state(
            spot=spot, strike=market.strike, time_left_sec=market.time_to_expiry_sec(int(now_ts)),
            reference_source="hyperliquid_btc_mark", market_mid=up_mid, outcome="up",
            sigma_default=self.telemetry_config.sigma_default, sigma_raw_realized=self._realized_sigma(),
            sigma_scale=self.telemetry_config.sigma_scale, sigma_floor=self.telemetry_config.sigma_floor,
            sigma_ceiling=self.telemetry_config.sigma_ceiling,
            time_decay_enabled=self.telemetry_config.time_decay_enabled,
            time_decay_ref_sec=self.telemetry_config.time_decay_ref_sec,
            time_decay_min=self.telemetry_config.time_decay_min,
            implied_sigma_enabled=self.telemetry_config.implied_sigma_enabled,
            twap_window_sec=0, observed_twap_average=None, observed_twap_seconds=0,
        )
        signals = self.signal_engine.compute(
            spot=spot, strike=market.strike, sigma=forecast.sigma_final,
            time_left_sec=forecast.time_left_sec, market_mid=up_mid,
        )
        score = Decimal(str(round(signals.composite_score, 6)))
        side = "NONE"
        if signals.confidence >= self.telemetry_config.min_confidence:
            if float(score) >= self.telemetry_config.threshold_up:
                side = "UP"
            elif float(score) <= -self.telemetry_config.threshold_down:
                side = "DOWN"
        entry_eligible = side != "NONE" and forecast.time_left_sec >= self.telemetry_config.min_entry_time_left_sec
        signal = SignalDecision(side, score, side != "NONE", "outcome_shadow_signal", False)
        telemetry = {
            "signal": signals.to_dict(), "proposed_side": side, "entry_eligible": entry_eligible,
            "would_submit_entry": entry_eligible, "execution_blocked": True,
            "forecast": forecast.diagnostics(market_mid=up_mid, outcome="up"),
            "fair_up": forecast.probability_for_outcome("up"),
            "fair_down": forecast.probability_for_outcome("down"),
        }
        return forecast, signal, telemetry

    def cycle(self) -> OutcomeShadowCycle:
        """Perform one read-only market/account collection cycle."""
        try:
            return self._cycle_once()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.journal.log_strategy_event(self.run_id, "OUTCOME_SHADOW_CYCLE_ERROR", {
                "venue": "hyperliquid_outcome", "read_only": True,
                "error_type": type(exc).__name__, "error": str(exc),
                "action": "cycle_skipped_will_retry_next_interval",
            })
            return OutcomeShadowCycle(None, 0, 0, 0, 0, error=error)

    def _cycle_once(self) -> OutcomeShadowCycle:
        """One collection attempt; exceptions are isolated by ``cycle``."""
        now_ts = int(time.time())
        if self.spec_audit and self._last_market:
            self.spec_audit.mark_pending_resolution(self._last_market, now_ts)
        meta = self.client.get_outcome_meta_sync(ttl_sec=0)
        preferences, allow_fallback = resolve_daily_outcome_scope(os.environ)
        market, status, _, _ = select_configured_btc_market(meta, period_preferences=preferences, allow_fallback=allow_fallback)
        account = self.account.fetch_snapshot()
        if market is None:
            self.journal.log_strategy_event(self.run_id, "OUTCOME_SHADOW_CYCLE", {
                "venue": "hyperliquid_outcome", "read_only": True, "market_status": status,
                "account_balance_count": len(account.balances), "account_open_order_count": len(account.open_orders),
                "account_fill_count": len(account.fills),
            })
            return OutcomeShadowCycle(None, len(account.balances), len(account.open_orders), len(account.fills), 0)

        raw_market = self._raw_meta_for_market(meta, market.outcome_id)
        if self.spec_audit and raw_market is not None:
            self.spec_audit.observe(market, raw_market)

        for fill in account.fills:
            observed_period = market.period if fill.outcome_id == market.outcome_id else "unknown"
            self.p3_pipeline.record_actual_fill(
                fill, period=observed_period, observed_at_ms=int(time.time() * 1000),
            )

        mids = self.client.get_all_mids_sync(ttl_sec=0)
        self.pricing.update_btc_mark_price(mids["BTC"])
        raw_books = {}
        local_received_at_ms: dict[str, int] = {}
        for coin in (market.yes_coin, market.no_coin):
            raw_books[coin] = self.client.get_l2_book_sync(coin, ttl_sec=0)
            local_received_at_ms[coin] = int(time.time() * 1000)
            self.pricing.update_l2_book(coin, raw_books[coin])
        capture_complete_at_ms = int(time.time() * 1000)
        capture_quality = build_p2_capture_quality(
            yes_book=raw_books[market.yes_coin], no_book=raw_books[market.no_coin],
            yes_local_received_at_ms=local_received_at_ms[market.yes_coin],
            no_local_received_at_ms=local_received_at_ms[market.no_coin],
            capture_complete_at_ms=capture_complete_at_ms,
        )
        try:
            user_fees = self.client.get_user_fees_sync(self.account.wallet_address)
            maker_close_fee_rate = Decimal(str(user_fees["userSpotAddRate"]))
            taker_close_fee_rate = Decimal(str(user_fees["userSpotCrossRate"]))
            fee_evidence: dict[str, Any] = {
                "source": "hyperliquid_userFees",
                "open_fee_rate": "0",
                "user_spot_cross_rate": str(taker_close_fee_rate),
                "user_spot_add_rate": str(maker_close_fee_rate),
                # userFees does not establish every market's conversion or
                # settlement cost, so it cannot unlock P2 economics by itself.
                "status": "observed_settlement_conversion_unverified",
            }
        except Exception as exc:
            maker_close_fee_rate = None
            taker_close_fee_rate = None
            fee_evidence = {"source": "hyperliquid_userFees", "status": "unavailable", "error_type": type(exc).__name__}
        parity = OutcomeParityAnalyzer(
            self.parity_analyzer.requested_shares,
            maker_close_fee_rate=maker_close_fee_rate,
            taker_close_fee_rate=taker_close_fee_rate,
        ).analyze(market, raw_books[market.yes_coin], raw_books[market.no_coin])
        snapshot_event_id = self.journal.log_strategy_event(self.run_id, "OUTCOME_P2_PARITY_SNAPSHOT", {
            "venue": "hyperliquid_outcome", "period": market.period,
            "p2_schema_version": P2_SCHEMA_VERSION,
            "snapshot_timestamp_ms": capture_complete_at_ms,
            "outcome_id": market.outcome_id, "yes_coin": market.yes_coin, "no_coin": market.no_coin,
            "yes_l2": raw_books[market.yes_coin], "no_l2": raw_books[market.no_coin],
            "capture_quality": capture_quality,
            "fee_evidence": fee_evidence,
            **parity.as_dict(),
        })
        p3_written = 0
        if capture_quality.get("status") == "accepted" and snapshot_event_id is not None:
            def _top(book: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
                levels = book.get("levels", [[], []])
                bids = levels[0] if isinstance(levels, list) and levels else []
                asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else []
                bid = Decimal(str(bids[0]["px"])) if bids else None
                ask = Decimal(str(asks[0]["px"])) if asks else None
                depth = Decimal(str(bids[0]["sz"])) if bids else None
                return bid, ask, depth

            yes_bid, yes_ask, yes_depth = _top(raw_books[market.yes_coin])
            no_bid, no_ask, no_depth = _top(raw_books[market.no_coin])
            time_left_sec = market.time_to_expiry_sec(current_timestamp=capture_complete_at_ms // 1000)
            p3_written = self.p3_pipeline.record_quote_snapshot(
                snapshot_event_id=snapshot_event_id,
                outcome_id=market.outcome_id,
                period=market.period,
                snapshot_timestamp_ms=capture_complete_at_ms,
                quotes=(
                    OutcomeQuote(market.yes_coin, capture_complete_at_ms, yes_bid, yes_ask, snapshot_event_id),
                    OutcomeQuote(market.no_coin, capture_complete_at_ms, no_bid, no_ask, snapshot_event_id),
                ),
                quote_contexts={
                    market.yes_coin: {
                        "time_left_sec": time_left_sec,
                        "spread": str(yes_ask - yes_bid) if yes_ask is not None and yes_bid is not None else None,
                        "depth": str(yes_depth) if yes_depth is not None else None,
                        "volatility_regime": "unknown",
                    },
                    market.no_coin: {
                        "time_left_sec": time_left_sec,
                        "spread": str(no_ask - no_bid) if no_ask is not None and no_bid is not None else None,
                        "depth": str(no_depth) if no_depth is not None else None,
                        "volatility_regime": "unknown",
                    },
                },
            )

        if self.ws_recorder:
            if self._last_market and self._last_market.outcome_id != market.outcome_id:
                self.ws_recorder.stop()
                self.ws_recorder = OutcomeWebSocketRecorder(self.client, self.journal, self.run_id)
            self.ws_recorder.start(
                outcome_id=market.outcome_id, yes_coin=market.yes_coin, no_coin=market.no_coin,
            )
            if self.ws_recorder.resync_required.is_set():
                self.journal.log_strategy_event(self.run_id, "OUTCOME_WS_REST_RESYNC", {
                    "venue": "hyperliquid_outcome", "read_only": True,
                    "outcome_id": market.outcome_id, "reason": "websocket_lifecycle_transition",
                    "rest_mids_refreshed": True, "rest_l2_refreshed": True,
                })
                self.ws_recorder.mark_rest_resynced()
                self.ws_recorder.resync_required.clear()

        up_mid = self.pricing.get_outcome_mid(market.yes_coin)
        if up_mid is None:
            raise ValueError(f"Outcome UP book has no valid mid: {market.yes_coin}")
        forecast, signal, telemetry = self._forecast_and_signal(market, up_mid, time.time())

        decisions = []
        market_snapshots = []
        for side_index in (0, 1):
            try:
                snapshot = build_outcome_market_snapshot(
                    market=market, side_index=side_index, pricing=self.pricing, exit_policy=self.exit_policy,
                    fee_rate=self.fee_rate, slippage_buffer_pct=self.slippage_buffer_pct,
                    fair=forecast.probability_for_outcome("up" if side_index == 0 else "down"),
                )
            except ValueError:
                continue
            position = account.position_state_for(market, side_index)
            runtime = account.sync_position_manager(
                self.position_manager, market, side_index, opened_ts=0,
            )
            position_signal = SignalDecision(
                active_side=signal.active_side,
                score=signal.score,
                locked=signal.locked,
                reason=signal.reason,
                matches_position=(signal.active_side == position.held_side),
            )
            decision = self.exit_engine.evaluate(snapshot, position, position_signal)
            market_snapshots.append({
                "side": position.held_side, "instrument_id": snapshot.instrument_id,
                "phase": snapshot.phase, "time_left_sec": snapshot.time_left_sec,
                "best_bid": snapshot.best_bid, "best_ask": snapshot.best_ask,
                "spread": snapshot.spread, "spread_pct": snapshot.spread_pct,
                "fair": snapshot.fair, "fair_edge_ps": snapshot.fair_edge_ps,
                "spot_minus_strike_bps": snapshot.spot_minus_strike_bps,
            })
            decisions.append({
                "side": position.held_side, "instrument_id": snapshot.instrument_id,
                "qty": position.qty, "sellable_qty": position.sellable_qty,
                "avg_entry_price": position.avg_entry_price, "position_lifecycle": runtime.lifecycle.value,
                "exit_decision": decision.decision_type.value, "exit_reason": decision.reason,
            })
            self.journal.log_strategy_event(self.run_id, "OUTCOME_P3_PASSIVE_QUOTE_CANDIDATE", {
                "venue": "hyperliquid_outcome", "read_only": True,
                "outcome_id": market.outcome_id, "coin": snapshot.instrument_id,
                "side_index": side_index, "candidate_side": "BUY",
                "candidate_price": snapshot.best_bid, "candidate_size": None,
                "fill_assumption": "UNKNOWN_NO_QUEUE_MODEL",
                "simulated_fill": False, "research_only": True,
            })
        self.journal.log_strategy_event(self.run_id, "OUTCOME_SHADOW_CYCLE", {
            "venue": "hyperliquid_outcome", "read_only": True, "market_status": status or "active",
            "outcome_id": market.outcome_id, "period": market.period, "yes_coin": market.yes_coin,
            "no_coin": market.no_coin, "account_balance_count": len(account.balances),
            "account_open_order_count": len(account.open_orders), "account_fill_count": len(account.fills),
            "market_snapshots": market_snapshots,
            "p2_parity": parity.as_dict(),
            "p3_markouts_written": p3_written,
            "strategy_telemetry": telemetry,
            "risk_decisions": decisions,
        })
        self._last_market = market
        return OutcomeShadowCycle(market, len(account.balances), len(account.open_orders), len(account.fills), len(decisions))

    def run(self, *, cycles: Optional[int], interval_sec: float) -> None:
        self.journal.log_run_start(self.run_id, "OUTCOME_SHADOW", True, True, notes={"read_only": True})
        try:
            count = 0
            while cycles is None or count < cycles:
                result = self.cycle()
                count += 1
                market_label = (
                    f"#{result.market.outcome_id} ({result.market.period})"
                    if result.market is not None
                    else "none"
                )
                print(
                    f"[OUTCOME_SHADOW_CYCLE {count}] market={market_label} "
                    f"balances={result.account_balance_count} "
                    f"open_orders={result.account_open_order_count} "
                    f"fills={result.account_fill_count} "
                    f"risk_inputs={result.risk_decision_count}",
                    flush=True,
                )
                if result.error:
                    print(f"[OUTCOME_SHADOW_RETRY] {result.error}", flush=True)
                if cycles is None or count < cycles:
                    time.sleep(max(0.1, interval_sec))
        finally:
            if self.ws_recorder:
                self.ws_recorder.stop()
            self.journal.log_run_stop(self.run_id, {"read_only": True})
