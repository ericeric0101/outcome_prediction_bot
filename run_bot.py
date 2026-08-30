"""
Complete BTC 15-Min Trading Bot - FIXED VERSION
- Uses time-based filtering (proven to work from test)
- $1 per trade maximum
- Reloads instruments every 12 minutes
- Pre-loads price history on startup
- Full P&L tracking in simulation
"""

import asyncio
import hashlib
import json
import os
import sys
from collections import deque
from pathlib import Path
from datetime import datetime, timezone, timedelta
import math
from decimal import Decimal
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
import random
import re
import threading
import uuid
import subprocess

# Add project to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def _loaded_source_fingerprint(repo_root: Path) -> str:
    """Fingerprint strategy modules when this Python process imports them.

    Node rollovers create a new strategy instance but do not reload Python
    modules.  Recording this import-time fingerprint distinguishes a genuine
    process restart from a later working-tree edit.
    """
    paths = (
        "run_bot.py",
        "bot/quote_service.py",
        "bot/pricing_runtime.py",
        "bot/spot_pricer.py",
        "bot/side_decision.py",
        "bot/forecast_state.py",
        "bot/strong_directional_regime.py",
        "bot/db_runtime.py",
        "bot/order_events.py",
        "bot/trade_telemetry.py",
        "monitoring/trade_journal_db.py",
        "bot/lead_lag_observation.py",
        "bot/order_submission.py",
        "bot/taker_exit.py",
        "bot/recovery_exit_ladder.py",
        "bot/db_runtime.py",
        "monitoring/trade_journal_db.py",
    )
    digest = hashlib.sha256()
    for relative_path in paths:
        path = repo_root / relative_path
        digest.update(relative_path.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()[:16]


RUNTIME_SOURCE_FINGERPRINT = _loaded_source_fingerprint(project_root)

# Now import Nautilus
from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket.factories import get_polymarket_http_client
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProvider
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProviderConfig
from nautilus_trader.common.component import LiveClock
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.identifiers import InstrumentId, ClientOrderId
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.objects import Price, Quantity
from loguru import logger

# Import our phases
from bot.inventory import InventoryLedger
from bot.runtime_env import load_runtime_env
from bot.edge_state import build_edge_state
from bot.edge_observation import build_quote_age_telemetry
from bot.entry_decision import EntryDecision
from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.position_manager import PositionManager, PositionManagerConfig
from bot.enums import ActiveSide, MarketPhase
from bot.side_decision import SideDecisionMixin
from bot.spot_pricer import SpotPricerMixin
from bot.taker_exit import TakerExitMixin
from bot.fill_ledger import FillLedgerMixin
from bot.db_runtime import StrategyDBRuntimeMixin
from bot.order_runtime import OrderRuntimeMixin
from bot.order_events import (
    handle_order_canceled,
    handle_order_cancel_rejected,
    handle_order_filled,
    handle_order_rejection_like_event,
)
from bot.order_submission import submit_maker_quote
from bot.market_runtime import (
    align_price_to_tick,
    find_btc_instrument,
    handle_generic_event,
    handle_quote_tick,
    handle_stop,
    maker_quote_sync,
    start_maker_worker,
    wait_for_btc_instrument,
)
from bot.pricing_runtime import PricingRuntimeMixin
from bot.quote_runtime import QuoteRuntimeMixin
from bot.recovery import StrategyRecoveryMixin
from bot.shadow_simulation import ShadowSimulationMixin
from bot.lead_lag_observation import LeadLagObservationMixin
from bot.lifecycle_runtime import StrategyLifecycleMixin
from bot.lifecycle import (
    evaluate_market_phase,
)
from bot.ops import (
    adjust_inventory_after_merge,
    dedupe_price_history,
    extend_synthetic_history,
    handle_quote_watchdog_recovery,
    log_strategy_run_start,
    run_auto_redeem_script,
    start_background_thread,
    should_attempt_quote_watchdog_recovery,
    should_run_quote_watchdog,
    should_skip_auto_redeem_run,
)
from bot.market_data import (
    estimate_external_spot_sigma_annualized,
    extract_market_start_ts_from_slug,
    extract_price_to_beat_from_market_payload,
    extract_strike_from_question,
    fetch_binance_open_price_sync,
    fetch_coinbase_spot_sync,
    record_external_spot_observation,
    resolve_opening_strike_from_history,
)
from bot.models import DecisionPhase, ExitDecisionType, MarketSnapshot, PositionState, QuoteIntentState, QuoteMode, SignalDecision
from bot.quoting import (
    apply_quote_plan_guards,
)
from bot.quote_service import (
    apply_entry_quality_quote_placement,
    parse_quote_plan,
    should_emit_edge_observation,
    apply_forced_exit_sell_pricing,
    apply_fractional_kelly_sizing,
    apply_high_entry_price_size_adjustment,
    apply_locked_side_recycle_sell_pricing,
    apply_shadow_entry_veto,
    apply_weak_pfair_size_adjustment,
    attach_desired_entry_runtime_metadata,
    apply_confirmed_inventory_sell_guard,
    build_active_maker_order_state,
    build_desired_quote_entry,
    build_directional_snapshot,
    build_quote_instrument_context,
    confirmed_adverse_exit,
    compute_requote_target_version,
    evaluate_buy_entry_controls,
    extract_instrument_tick,
    locked_side_signal_invalidated,
    log_no_quote_diagnostics,
    maybe_apply_continuation_entry,
    maybe_apply_trapped_inventory_recovery,
    synchronize_desired_buy_economics_to_quantity,
    preserve_profitable_existing_sell_order,
    preserve_recent_loss_sell_order,
    reconcile_unwanted_quotes,
    resolve_quote_intent_state,
    should_requote_existing_order,
)
from bot.entry_confirmation import apply_entry_confirmation_adjustment
from bot.smart_money import (
    apply_smart_money_adjustment,
    extract_condition_id_from_instrument_id,
    extract_token_id_from_instrument_id,
)
from bot.shadow_signal import (
    attach_forecast_snapshot_telemetry,
    build_entry_regime_observation_payload,
    build_live_signal_compare_payload,
)
from bot.strong_directional_regime import apply_strong_directional_regime_economics
from bot.settings import initialize_strategy_settings
from bot.market_cycle_state import MarketCycleState, bind_market_cycle_state
from bot.market_discovery import (
    resolve_best_btc_15m_market,
    resolve_btc_15m_market_slugs,
    resolve_primary_btc_15m_instrument_ids,
)
from bot.merge_ops import try_merge_yes_no_positions
from alert_watcher import AlertWatcher
from dashboard_state import DashboardState, TradeRecord
from telegram_notifier import TelegramNotifier

load_runtime_env(repo_root=project_root)


def detect_runtime_git_revision(repo_root: Path) -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if head.returncode != 0:
            return "unknown"
        commit = head.stdout.strip() or "unknown"
        dirty = subprocess.run(
            ["git", "diff", "--quiet", "--exit-code"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if dirty.returncode == 1:
            return f"{commit}-dirty"
        return commit
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class LockedSideRuntimeState:
    signal_invalidated: bool = False
    spot_supports: bool = False
    invalidation_confirmed: bool = False
    entry_blocked: bool = False
    entry_block_reason: str = ""


class IntegratedBTCStrategy(
    SideDecisionMixin,
    SpotPricerMixin,
    TakerExitMixin,
    FillLedgerMixin,
    StrategyDBRuntimeMixin,
    OrderRuntimeMixin,
    PricingRuntimeMixin,
    QuoteRuntimeMixin,
    StrategyRecoveryMixin,
    ShadowSimulationMixin,
    LeadLagObservationMixin,
    StrategyLifecycleMixin,
    Strategy,
):
    """
    Integrated BTC Strategy combining:
    - Nautilus trading framework
    - Our 7-phase system
    - Paper trading tracking
    - Auto-reload instruments every 12 minutes
    - Pre-loaded price history for immediate trading
    """
    
    def __init__(
        self,
        test_mode=False,
        selected_slug: Optional[str] = None,
        enable_terminal_dashboard: bool = False,
        dashboard_state: Optional[DashboardState] = None,
        telegram_notifier: Optional[TelegramNotifier] = None,
        alert_watcher: Optional[AlertWatcher] = None,
    ):
        super().__init__()
        
        # Nautilus
        self.instrument_id = None
        self.selected_slug = selected_slug
        self.dashboard_state = dashboard_state
        self.telegram_notifier = telegram_notifier
        self.alert_watcher = alert_watcher
        self.runtime_source_fingerprint = RUNTIME_SOURCE_FINGERPRINT
        self._last_dashboard_sync_ts = 0.0
        self._last_dashboard_pause_log_ts = 0.0
        initialize_strategy_settings(
            self,
            test_mode=test_mode,
            enable_terminal_dashboard=enable_terminal_dashboard,
            project_root=project_root,
            detect_runtime_git_revision_fn=detect_runtime_git_revision,
        )

        if test_mode:
            logger.warning("DRY-RUN MODE ACTIVE - orders will be recorded but not submitted.")
        logger.info("Integrated BTC strategy initialized.")

    def _record_dashboard_error(self, message: str) -> None:
        state = getattr(self, "dashboard_state", None)
        if state is None:
            return
        try:
            recent_errors = list(state.recent_errors)[-19:]
            recent_errors.append((datetime.now(timezone.utc), str(message)))
            state.update(recent_errors=recent_errors)
        except Exception:
            logger.debug("Failed to record dashboard error", exc_info=True)

    def _sync_dashboard_state(self) -> None:
        state = getattr(self, "dashboard_state", None)
        if state is None:
            return
        now_dt = datetime.now(timezone.utc)
        try:
            slug = str(self.current_market_slug or self.selected_slug or "")
            strike = self.market_strike_cache_by_slug.get(slug)
            spot = self._capture_market_open_spot()
            if spot is None and getattr(self, "_binance_ws_price", None) is not None:
                spot = Decimal(str(self._binance_ws_price))

            position_side: Optional[str] = None
            position_entry: Optional[float] = None
            position_qty: Optional[float] = None
            position_ask: Optional[float] = None
            position_hold_sec: Optional[float] = None
            current_market_price = 0.0
            for inst_key, inv_state in list(getattr(self, "live_inventory_cost", {}).items()):
                qty = Decimal(str(inv_state.get("qty", "0")))
                if qty <= 0:
                    continue
                inst_id = self._normalize_instrument_id(inst_key)
                side = self._side_for_instrument_id(inst_id) if inst_id is not None else ActiveSide.NONE
                position_side = getattr(side, "value", str(side)).upper()
                position_entry = float(inv_state.get("avg_entry_price", 0.0) or 0.0)
                position_qty = float(qty)
                opened_ts = float(inv_state.get("opened_ts", 0.0) or 0.0)
                position_hold_sec = max(0.0, time.time() - opened_ts) if opened_ts > 0 else None
                quote = self._get_quote_for_instrument(inst_id) if inst_id is not None else None
                if quote is not None:
                    bid, ask = quote
                    current_market_price = float((bid + ask) / Decimal("2"))
                sell_key = self._order_key_for("sell", inst_id) if inst_id is not None else ""
                sell_order = self.active_maker_orders.get(sell_key, {}) if sell_key else {}
                if sell_order:
                    position_ask = float(sell_order.get("limit_price", 0.0) or 0.0)
                break

            recent_fill_pnls = list(getattr(self, "recent_fill_pnl_results", []) or [])
            consecutive_losses = 0
            for pnl in reversed(recent_fill_pnls):
                if float(pnl) < 0:
                    consecutive_losses += 1
                    continue
                break

            state.update(
                strike_price=float(strike) if strike is not None else 0.0,
                spot_price=float(spot) if spot is not None else 0.0,
                position_side=position_side,
                position_entry=position_entry,
                position_qty=position_qty,
                position_ask=position_ask,
                position_hold_sec=position_hold_sec,
                current_market_price=current_market_price,
                market_slug=slug or None,
                cumulative_pnl=float(getattr(self, "_live_cumulative_pnl", 0.0) or 0.0),
                visible_trades_pnl=float(getattr(self, "market_cycle_realized_net_usdc", Decimal("0")) or 0.0),
                usdc_balance=float(getattr(self, "_cached_usdc_balance", 0.0) or 0.0),
                pol_balance=float(getattr(self, "_cached_pol_balance", 0.0) or 0.0),
                account_last_updated=now_dt,
                market_phase=getattr(self.market_phase, "value", str(self.market_phase)),
                active_side=getattr(self.active_side, "value", str(self.active_side)),
                time_left_sec=(
                    float(self.current_market_end_timestamp - time.time())
                    if getattr(self, "current_market_end_timestamp", None) is not None
                    else None
                ),
                decision_updated_at=(
                    datetime.fromtimestamp(float(self.side_decision_ts), tz=timezone.utc)
                    if float(getattr(self, "side_decision_ts", 0.0) or 0.0) > 0
                    else None
                ),
                side_score=float(getattr(self, "side_decision_score", 0.0) or 0.0),
                book_bid=float(self.latest_market_bid) if self.latest_market_bid is not None else None,
                book_ask=float(self.latest_market_ask) if self.latest_market_ask is not None else None,
                book_mid=(
                    float((self.latest_market_bid + self.latest_market_ask) / Decimal("2"))
                    if self.latest_market_bid is not None and self.latest_market_ask is not None
                    else None
                ),
                robust_net_usdc=None,
                last_block_reason=getattr(self, "side_decision_reason", None),
                open_exposure_usdc=float(getattr(self, "inventory_delta_shares", Decimal("0")) or 0.0),
                last_heartbeat=now_dt,
                consecutive_losses=consecutive_losses,
            )
            self._last_dashboard_sync_ts = time.time()
        except Exception as e:
            logger.debug(f"Failed to sync Telegram dashboard state: {e}")

    def _handle_dashboard_flatten_request(self) -> None:
        state = getattr(self, "dashboard_state", None)
        if state is None or not bool(getattr(state, "flatten_requested", False)):
            return
        state.update(flatten_requested=False)
        submitted = 0
        for inst_key, inv_state in list(getattr(self, "live_inventory_cost", {}).items()):
            try:
                qty = Decimal(str(inv_state.get("qty", "0")))
                if qty <= 0:
                    continue
                inst_id = self._normalize_instrument_id(inst_key)
                if inst_id is None:
                    continue
                quote = self._get_quote_for_instrument(inst_id)
                if quote is None:
                    logger.warning(f"Telegram flatten skipped: no quote for {inst_key}")
                    continue
                best_bid, _ = quote
                sellable_qty = self._get_effective_sellable_qty(instrument_id=inst_id)
                qty_to_exit = min(qty, sellable_qty)
                avg_entry = Decimal(str(inv_state.get("avg_entry_price", "0")))
                est_net = (best_bid - avg_entry) * qty_to_exit
                ok = self._submit_taker_exit_order(
                    instrument_id=inst_id,
                    quantity=qty_to_exit,
                    reason="telegram_flatten",
                    est_net_if_exit=est_net,
                    best_bid=best_bid,
                    fee_rate=self._infer_market_fee_rate_default(),
                    decision_payload={"source": "telegram"},
                )
                if ok:
                    submitted += 1
            except Exception as e:
                self._record_dashboard_error(f"Telegram flatten failed for {inst_key}: {e}")
                logger.exception(f"Telegram flatten failed for {inst_key}: {e}")
        logger.warning(f"Telegram flatten request processed; submitted={submitted}")

    def _telegram_cycle_tick(self) -> None:
        self._sync_dashboard_state()
        self._handle_dashboard_flatten_request()
        if self.dashboard_state is not None and self.alert_watcher is not None and self.telegram_notifier is not None:
            try:
                self.alert_watcher.check_and_alert(self.dashboard_state, self.telegram_notifier)
            except Exception as e:
                logger.debug(f"Alert watcher failed: {e}")

    def _current_thesis_epoch(self, slug: str) -> int:
        slug_key = str(slug or "")
        if not slug_key:
            return 0
        return int(self._thesis_epoch_by_slug.get(slug_key, 0))

    def _market_buy_budget_key(self, slug: str) -> str:
        slug_key = str(slug or "")
        if not slug_key:
            return ""
        return f"{slug_key}:{self._current_thesis_epoch(slug_key)}"

    def _bump_thesis_epoch(self, slug: str) -> int:
        slug_key = str(slug or "")
        if not slug_key:
            return 0
        next_epoch = int(self._thesis_epoch_by_slug.get(slug_key, 0)) + 1
        self._thesis_epoch_by_slug[slug_key] = next_epoch
        return next_epoch

    @property
    def inventory_delta_shares(self) -> Decimal:
        return self._inventory_delta_shares

    @inventory_delta_shares.setter
    def inventory_delta_shares(self, value: Decimal):
        old_val = getattr(self, "_inventory_delta_shares", Decimal("0"))
        if value != old_val:
            self.inventory_last_update_ts = time.time()
            self._inventory_delta_shares = value

    def _log_strategy_config_summary(self) -> None:
        logger.info(
            "Config summary: "
            f"mode={'maker' if self.maker_mode else 'signal'} "
            f"quote_sides={self.maker_quote_sides} "
            f"pricer={self.maker_fair_pricer_mode} "
            f"ref={'twap' if getattr(self, 'polymarket_chainlink_twap_enabled', True) else 'spot'} "
            f"twap_window={int(getattr(self, 'polymarket_chainlink_twap_window_sec', 60) or 60)}s "
            f"require_twap={'on' if getattr(self, 'require_twap_reference_spot', True) else 'off'} "
            f"bi_side={'on' if self.bi_side_enabled else 'off'} "
            f"hold_to_redeem={'on' if getattr(self, 'hold_to_redeem_enabled', False) else 'off'} "
            f"tail_tp={'on' if getattr(self, 'tail_protect_tp_enabled', False) else 'off'} "
            f"tail_tp_px={float(getattr(self, 'tail_protect_tp_price', Decimal('0'))):.2f} "
            f"tail_tp_frac={float(getattr(self, 'tail_protect_tp_fraction', Decimal('0'))):.2f} "
            f"tail_tp_min_entry={float(getattr(self, 'tail_protect_tp_min_entry_price', Decimal('0'))):.2f} "
            f"post_only={'on' if self.maker_use_post_only else 'off'} "
            f"auto_tune={'on' if self.auto_tune_enabled else 'off'} "
            f"auto_redeem={'on' if self.auto_redeem_enabled else 'off'} "
            f"trade_db={'on' if self.trade_db_enabled else 'off'}"
        )
        logger.info(
            "Risk/ops: "
            f"max_order_usdc={float(self.maker_max_order_usdc):.2f} "
            f"reduce_only_cutoff_min={self.maker_min_minutes_to_close:.1f} "
            f"watchdog_stale={self.quote_stale_sec}s "
            f"requote_min_age={self.maker_requote_min_age_sec:.1f}s"
        )
        if self.startup_verbose:
            logger.info(
                "Verbose config: "
                f"fee_interval={self.fee_rate_fetch_interval_sec}s "
                f"balance_guard={self.conditional_balance_check_interval_sec}s/"
                f"{float(self.conditional_balance_safety_buffer_pct)*100:.2f}% "
                f"taker_exit={'on' if self.taker_exit_enabled else 'off'} "
                f"taker_max_spread={float(self.taker_exit_max_spread_pct):.3f} "
                f"early_sell_only={self.maker_early_sell_only_sec}s "
                "directional_edge_metric=telemetry_only "
                f"regime_guard={'on' if self.regime_guard_enabled else 'off'}"
            )

    def _bootstrap_btc_instruments_into_cache(self) -> int:
        """
        Prime the strategy cache if startup races ahead of the data client.

        This uses the same BTC 15m slug discovery and Polymarket instrument provider
        configuration as the launcher, then inserts any loaded instruments into the
        strategy cache so the normal startup path can continue.
        """
        result: Dict[str, Any] = {"loaded": 0, "error": None}

        def _worker() -> None:
            try:
                load_slug_count = max(1, int(os.getenv("BTC_MARKET_LOAD_SLUG_COUNT", "3")))
                btc_slugs = resolve_btc_15m_market_slugs()
                ordered_slugs: List[str] = []
                if self.selected_slug:
                    ordered_slugs.append(str(self.selected_slug))
                ordered_slugs.extend([slug for slug in btc_slugs if slug not in ordered_slugs])
                slugs_to_load = ordered_slugs[:load_slug_count]

                seen_ids: Set[str] = set()
                instrument_ids: List[InstrumentId] = []
                for slug in slugs_to_load:
                    ids = resolve_primary_btc_15m_instrument_ids(slug)
                    if not ids:
                        continue
                    for inst_id in ids:
                        if inst_id.value in seen_ids:
                            continue
                        seen_ids.add(inst_id.value)
                        instrument_ids.append(inst_id)

                if not instrument_ids:
                    result["error"] = "Instrument bootstrap fallback found no BTC 15m instrument IDs."
                    return

                now_utc = datetime.now(timezone.utc)
                window_back_minutes = int(os.getenv("BTC_MARKET_END_WINDOW_BACK_MINUTES", "5"))
                window_forward_minutes = int(os.getenv("BTC_MARKET_END_WINDOW_FORWARD_MINUTES", "120"))
                instrument_cfg = PolymarketInstrumentProviderConfig(
                    load_all=False,
                    load_ids=frozenset(instrument_ids),
                    filters={
                        "active": True,
                        "closed": False,
                        "archived": False,
                        "end_date_min": (now_utc - timedelta(minutes=window_back_minutes)).isoformat(),
                        "end_date_max": (now_utc + timedelta(minutes=window_forward_minutes)).isoformat(),
                        "limit": 25,
                    },
                    use_gamma_markets=True,
                )

                provider = PolymarketInstrumentProvider(
                    client=get_polymarket_http_client(
                        private_key=os.getenv("POLYMARKET_PK"),
                        signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
                        funder=os.getenv("POLYMARKET_FUNDER") or None,
                        api_key=os.getenv("POLYMARKET_API_KEY"),
                        api_secret=os.getenv("POLYMARKET_API_SECRET"),
                        passphrase=os.getenv("POLYMARKET_PASSPHRASE"),
                        base_url=os.getenv("POLYMARKET_CLOB_BASE_URL", "https://clob.polymarket.com"),
                    ),
                    clock=LiveClock(),
                    config=instrument_cfg,
                )
                asyncio.run(provider.initialize(reload=True))

                loaded = 0
                for instrument in provider.get_all().values():
                    self.cache.add_instrument(instrument)
                    loaded += 1
                result["loaded"] = loaded
            except Exception as exc:
                result["error"] = str(exc)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join(timeout=30)
        if thread.is_alive():
            logger.warning("Instrument bootstrap fallback timed out after 30s.")
            return 0
        if result["error"]:
            logger.warning(str(result["error"]))
            return 0
        if result["loaded"]:
            logger.warning(
                f"Instrument bootstrap fallback loaded {result['loaded']} BTC 15m instruments into cache.",
            )
        else:
            logger.warning("Instrument bootstrap fallback completed but returned zero instruments.")
        return int(result["loaded"])

    def _max_inventory_avg_entry(self) -> Decimal:
        return InventoryLedger.max_avg_entry(self.live_inventory_cost)

    def _clear_profit_run_state(self, instrument_id: Any) -> None:
        inst_key = self._instrument_key(instrument_id)
        if not inst_key:
            return
        self.maker_profit_run_peak_bid_by_inst.pop(inst_key, None)
        self.maker_profit_run_peak_fair_by_inst.pop(inst_key, None)

    def _update_profit_run_peaks(
        self,
        instrument_id: Any,
        *,
        best_bid: Optional[Decimal],
        fair: Optional[Decimal],
    ) -> None:
        inst_key = self._instrument_key(instrument_id)
        if not inst_key:
            return
        state = self.live_inventory_cost.get(inst_key)
        if not state:
            self._clear_profit_run_state(instrument_id)
            return
        try:
            qty = Decimal(str(state.get("qty", "0")))
        except Exception:
            qty = Decimal("0")
        if qty <= 0:
            self._clear_profit_run_state(instrument_id)
            return
        if best_bid is not None and best_bid > 0:
            prev_peak_bid = self.maker_profit_run_peak_bid_by_inst.get(inst_key)
            if prev_peak_bid is None or best_bid > prev_peak_bid:
                self.maker_profit_run_peak_bid_by_inst[inst_key] = best_bid
        if fair is not None and fair > 0:
            prev_peak_fair = self.maker_profit_run_peak_fair_by_inst.get(inst_key)
            if prev_peak_fair is None or fair > prev_peak_fair:
                self.maker_profit_run_peak_fair_by_inst[inst_key] = fair

    def _should_hold_profitable_position(
        self,
        *,
        instrument_id: Any,
        best_bid: Decimal,
        fair: Optional[Decimal],
        avg_entry: Decimal,
        time_left_sec: Optional[float],
        thesis_weakened: bool,
        offside_confirmed: bool,
    ) -> tuple[bool, str]:
        inst_key = self._instrument_key(instrument_id)
        state = self.live_inventory_cost.get(inst_key)
        if not inst_key or not state:
            return False, ""
        try:
            qty = Decimal(str(state.get("qty", "0")))
        except Exception:
            qty = Decimal("0")
        peak_bid = self.maker_profit_run_peak_bid_by_inst.get(inst_key, best_bid)
        peak_fair = self.maker_profit_run_peak_fair_by_inst.get(inst_key, fair or best_bid)
        try:
            opened_ts = float(state.get("opened_ts", 0.0))
        except Exception:
            opened_ts = 0.0
        return self.position_manager.should_hold_profitable_position(
            inst_key=inst_key,
            qty=qty,
            best_bid=best_bid,
            fair=fair,
            avg_entry=avg_entry,
            active_side_locked=self.active_side_locked,
            active_side=self.active_side.value,
            instrument_matches_active_side=(self._instrument_for_side(self.active_side) == instrument_id),
            side_decision_score=self.side_decision_score,
            exit_stage_value=self.exit_policy.stage(time_left_sec).value,
            thesis_weakened=thesis_weakened,
            offside_confirmed=offside_confirmed,
            opened_ts=opened_ts,
            peak_bid=peak_bid,
            peak_fair=peak_fair,
        )



    def _is_emergency_exit_window(self, time_left_sec: Optional[float]) -> bool:
        if time_left_sec is None:
            return False
        if self.maker_sell_cost_protect_emergency_last_sec <= 0:
            return False
        return time_left_sec <= float(self.maker_sell_cost_protect_emergency_last_sec)

    def _assess_thesis_weakened(
        self,
        *,
        inst_id: Any,
        now_ts: float,
        side_score: Decimal,
    ) -> bool:
        inst_key = self._instrument_key(inst_id)
        if not inst_key:
            return False

        raw_thesis_weakened = False
        score = float(side_score)
        opposite_score_abs = float(self.side_thesis_weak_opposite_score_abs_new)
        requires_opposite_side = bool(self.side_thesis_weak_requires_opposite_side_new)
        if self.active_side == ActiveSide.UP and score <= -opposite_score_abs:
            raw_thesis_weakened = True
        elif self.active_side == ActiveSide.DOWN and score >= opposite_score_abs:
            raw_thesis_weakened = True
        elif (
            not requires_opposite_side
            and self.active_side != ActiveSide.NONE
            and abs(score) < float(self.side_thesis_weak_score_abs)
        ):
            raw_thesis_weakened = True

        recent_buy_ts = float(self.recent_buy_fill_ts_by_inst.get(inst_key, 0.0))
        if (
            raw_thesis_weakened
            and recent_buy_ts > 0
            and self.side_thesis_weak_min_hold_sec_new > 0
            and (now_ts - recent_buy_ts) < float(self.side_thesis_weak_min_hold_sec_new)
        ):
            self.side_thesis_weak_hits_by_inst[inst_key] = 0
            return False

        hits = int(self.side_thesis_weak_hits_by_inst.get(inst_key, 0))
        hits = hits + 1 if raw_thesis_weakened else 0
        self.side_thesis_weak_hits_by_inst[inst_key] = hits
        return raw_thesis_weakened and hits >= int(self.side_thesis_weak_confirmations_new)

    def _normalize_active_side(self, side: Any) -> ActiveSide:
        txt = str(side or "").strip().upper()
        if txt == ActiveSide.UP.value:
            return ActiveSide.UP
        if txt == ActiveSide.DOWN.value:
            return ActiveSide.DOWN
        return ActiveSide.NONE

    def _primary_instrument_for_market(self) -> Optional[InstrumentId]:
        return self.current_up_instrument_id or self.current_down_instrument_id or self.instrument_id

    def _instrument_for_side(self, side: ActiveSide) -> Optional[InstrumentId]:
        if side == ActiveSide.UP:
            return self.current_up_instrument_id or self._primary_instrument_for_market()
        if side == ActiveSide.DOWN:
            return self.current_down_instrument_id
        return None

    def _side_for_instrument_id(self, instrument_id: Optional[Any]) -> ActiveSide:
        inst = self._normalize_instrument_id(instrument_id)
        if inst is None:
            return ActiveSide.NONE
        if self.current_up_instrument_id is not None and inst == self.current_up_instrument_id:
            return ActiveSide.UP
        if self.current_down_instrument_id is not None and inst == self.current_down_instrument_id:
            return ActiveSide.DOWN
        return ActiveSide.NONE

    def _sync_active_instrument(self) -> None:
        target = self._instrument_for_side(self.active_side)
        if target is None:
            target = self._primary_instrument_for_market()
        self.instrument_id = target
        self.current_token_id = self._extract_token_id_from_instrument(str(target)) if target is not None else None

    def _capture_market_open_spot_detail(self, *, now_ts: Optional[float] = None) -> tuple[Optional[Decimal], str, Optional[float]]:
        now = float(now_ts or time.time())
        fresh_sec = 10.0

        def _candidate(price: Optional[Decimal], ts: float, source: str) -> tuple[Optional[Decimal], str, Optional[float]]:
            if price is None or price <= 0:
                return (None, source, None)
            age = max(0.0, now - float(ts or 0.0)) if ts and ts > 0 else None
            return (price, source, age)

        poly = _candidate(
            getattr(self, "_polymarket_chainlink_price", None),
            float(getattr(self, "_polymarket_chainlink_price_ts", 0.0) or 0.0),
            "polymarket_chainlink_ws",
        )
        twap_window = int(
            getattr(self, "_polymarket_chainlink_twap_window_sec", 0)
            or getattr(self, "polymarket_chainlink_twap_window_sec", 60)
            or 60
        )
        twap = _candidate(
            getattr(self, "_polymarket_chainlink_twap_price", None),
            float(getattr(self, "_polymarket_chainlink_twap_price_ts", 0.0) or 0.0),
            f"polymarket_chainlink_twap_{twap_window}s_ws",
        )
        binance = _candidate(
            getattr(self, "_binance_ws_price", None),
            float(getattr(self, "_binance_ws_price_ts", 0.0) or 0.0),
            "binance_ws",
        )
        latest_src = str(getattr(self, "latest_external_spot_source", "") or "latest_external")
        latest = _candidate(
            getattr(self, "latest_external_spot", None),
            float(getattr(self, "latest_external_spot_source_ts", 0.0) or 0.0),
            latest_src,
        )

        for cand in (twap, poly, binance, latest):
            price, source, age = cand
            if price is not None and age is not None and age < fresh_sec:
                return cand

        stale_candidates = [cand for cand in (twap, poly, binance, latest) if cand[0] is not None]
        if stale_candidates:
            stale_candidates.sort(key=lambda item: item[2] if item[2] is not None else float("inf"))
            return stale_candidates[0]

        last_external = getattr(self, "last_external_spot", None)
        if last_external is not None and last_external > 0:
            return (last_external, "last_external_spot", None)
        if self.external_spot_history:
            hist_ts, hist_px = self.external_spot_history[-1]
            if hist_px > 0:
                age = max(0.0, now - float(hist_ts or 0.0)) if hist_ts else None
                return (hist_px, "external_spot_history", age)
        return (None, "-", None)

    def _capture_market_open_spot(self) -> Optional[Decimal]:
        return self._capture_market_open_spot_detail()[0]

    def _spot_minus_strike_bps(self) -> Optional[Decimal]:
        slug = str(self.current_market_slug or "")
        if not slug:
            return None
        spot = self._capture_market_open_spot()
        strike = self.market_strike_cache_by_slug.get(slug)
        if spot is None or spot <= 0 or strike is None or strike <= 0:
            return None
        try:
            return ((spot / strike) - Decimal("1")) * Decimal("10000")
        except Exception:
            return None

    def _spot_minus_strike_avg(self, lookback_sec: int) -> Optional[Decimal]:
        slug = str(self.current_market_slug or "")
        if not slug or lookback_sec <= 0:
            return None
        strike = self.market_strike_cache_by_slug.get(slug)
        if strike is None or strike <= 0:
            return None
        now_ts = time.time()
        samples: list[Decimal] = []
        try:
            for ts, spot in reversed(self.external_spot_history):
                if now_ts - float(ts) > float(lookback_sec):
                    break
                if spot is None or spot <= 0:
                    continue
                samples.append(Decimal(str(spot)) - strike)
        except Exception:
            return None
        if not samples:
            return None
        return sum(samples, Decimal("0")) / Decimal(len(samples))

    def _spot_still_supports_side(
        self,
        side: ActiveSide,
        *,
        spot: Optional[Decimal],
        strike: Optional[Decimal],
    ) -> bool:
        if side == ActiveSide.NONE or spot is None or strike is None or strike <= 0:
            return False
        try:
            buffer = Decimal(str(getattr(self, "maker_side_invalidation_spot_buffer_bps", Decimal("0"))))
            if side == ActiveSide.UP:
                threshold = strike * (Decimal("1") + (buffer / Decimal("10000")))
                return spot > threshold
            threshold = strike * (Decimal("1") - (buffer / Decimal("10000")))
            return spot < threshold
        except Exception:
            return False

    def _update_side_invalidation_state(
        self,
        *,
        now_ts: float,
        slug: str,
        side: ActiveSide,
        spot: Optional[Decimal],
        strike: Optional[Decimal],
        fair: Optional[Decimal],
        inventory_qty: Decimal,
        time_left_sec: float | None,
    ) -> tuple[bool, bool]:
        if not slug or side == ActiveSide.NONE:
            return False, False
        spot_supports = self._spot_still_supports_side(side, spot=spot, strike=strike)
        fair_invalidates = False
        if fair is not None and fair > 0:
            flip_min = Decimal(str(getattr(self, "maker_side_invalidation_fair_flip_min", Decimal("0.60"))))
            if side == ActiveSide.UP:
                fair_invalidates = fair <= (Decimal("1") - flip_min)
            elif side == ActiveSide.DOWN:
                fair_invalidates = fair <= (Decimal("1") - flip_min)
        invalidated = (not spot_supports) and fair_invalidates
        if invalidated:
            self._side_invalidation_hits_by_slug[slug] = int(self._side_invalidation_hits_by_slug.get(slug, 0)) + 1
        else:
            self._side_invalidation_hits_by_slug[slug] = 0
        hits = int(self._side_invalidation_hits_by_slug.get(slug, 0))
        confirmed = hits >= int(getattr(self, "maker_side_invalidation_confirm_cycles", 2))
        prev_confirmed = bool(self._side_invalidation_confirmed_by_slug.get(slug, False))
        self._side_invalidation_confirmed_by_slug[slug] = confirmed
        if confirmed and not prev_confirmed:
            self._bump_thesis_epoch(slug)
            self._db_strategy_event(
                "SIDE_INVALIDATION_CONFIRMED",
                {
                    "slug": slug,
                    "side": side.value,
                    "hits": hits,
                    "spot": float(spot) if spot is not None else None,
                    "strike": float(strike) if strike is not None else None,
                    "fair": float(fair) if fair is not None else None,
                    "inventory_qty": float(inventory_qty),
                    "time_left_sec": time_left_sec,
                },
            )
        elif (not confirmed) and prev_confirmed:
            self._db_strategy_event(
                "SIDE_INVALIDATION_CLEARED",
                {
                    "slug": slug,
                    "side": side.value,
                    "hits": hits,
                    "spot": float(spot) if spot is not None else None,
                    "strike": float(strike) if strike is not None else None,
                    "fair": float(fair) if fair is not None else None,
                    "inventory_qty": float(inventory_qty),
                    "time_left_sec": time_left_sec,
                },
            )
        force_unlock_last_sec = float(getattr(self, "maker_side_force_unlock_last_sec", 0))
        force_unlock_window = (
            time_left_sec is not None
            and force_unlock_last_sec > 0
            and time_left_sec <= force_unlock_last_sec
        )
        should_clear_side = confirmed and inventory_qty <= 0 and force_unlock_window
        if should_clear_side and self.active_side != ActiveSide.NONE:
            old_side = self.active_side
            self.active_side = ActiveSide.NONE
            self.active_side_locked = False
            self.side_decision_reason = "side_invalidated_force_unlock"
            self.side_pending_flip_side = ActiveSide.NONE
            self.side_pending_flip_count = 0
            self.side_pending_flip_since_ts = 0.0
            self._db_strategy_event(
                "SIDE_FORCE_UNLOCKED",
                {
                    "slug": slug,
                    "old_side": old_side.value,
                    "hits": hits,
                    "spot": float(spot) if spot is not None else None,
                    "strike": float(strike) if strike is not None else None,
                    "fair": float(fair) if fair is not None else None,
                    "time_left_sec": time_left_sec,
                },
            )
        return spot_supports, confirmed

    def _resolve_locked_side_runtime_state(
        self,
        *,
        now_ts: float,
        slug: str,
        inst_id: Any,
        fair: Optional[Decimal],
        current_price: Optional[Decimal],
        price_to_beat: Optional[Decimal],
        inventory_qty: Decimal,
        time_left_sec: float | None,
        shadow_payload: dict[str, Any] | None,
    ) -> LockedSideRuntimeState:
        signal_invalidated = locked_side_signal_invalidated(
            active_side_value=self.active_side.value,
            active_side_locked=bool(self.active_side_locked),
            side_score=self.side_decision_score,
            shadow_payload=shadow_payload,
        )
        active_inst_id = self._instrument_for_side(self.active_side)
        if (
            not self.active_side_locked
            or self.active_side == ActiveSide.NONE
            or active_inst_id is None
            or str(inst_id) != str(active_inst_id)
        ):
            return LockedSideRuntimeState(
                signal_invalidated=signal_invalidated,
                entry_blocked=signal_invalidated,
                entry_block_reason="locked_side_signal_invalidated" if signal_invalidated else "",
            )
        spot_supports, invalidation_confirmed = self._update_side_invalidation_state(
            now_ts=now_ts,
            slug=slug,
            side=self.active_side,
            spot=current_price,
            strike=price_to_beat,
            fair=fair,
            inventory_qty=inventory_qty,
            time_left_sec=time_left_sec,
        )
        entry_blocked = bool(signal_invalidated or invalidation_confirmed)
        entry_block_reason = (
            "locked_side_invalidation_confirmed"
            if invalidation_confirmed
            else "locked_side_signal_invalidated"
            if signal_invalidated
            else ""
        )
        return LockedSideRuntimeState(
            signal_invalidated=signal_invalidated,
            spot_supports=spot_supports,
            invalidation_confirmed=invalidation_confirmed,
            entry_blocked=entry_blocked,
            entry_block_reason=entry_block_reason,
        )

    def _emit_live_signal_compare_snapshot(self, now_ts: float) -> None:
        if not self.trade_db or not getattr(self, "shadow_signal_enabled", False):
            return
        payload = self._build_live_signal_compare_payload(now_ts)
        if payload is None:
            return
        self._db_strategy_event("LIVE_SIGNAL_COMPARE", payload)
        self._emit_entry_regime_observation(payload, now_ts)

        main_sig = json.dumps(
            {
                "slug": payload["slug"],
                "main_candidate_side": payload.get("main_candidate_side"),
                "main_score": round(float(payload.get("main_score") or 0.0), 4),
                "main_locked": bool(payload.get("main_side_locked")),
            },
            sort_keys=True,
        )
        if main_sig != getattr(self, "_last_main_live_candidate_signature", None):
            self._last_main_live_candidate_signature = main_sig
            self._db_strategy_event("MAIN_SIGNAL_CANDIDATE_LIVE", payload)

        shadow_sig = json.dumps(
            {
                "slug": payload["slug"],
                "shadow_candidate_side": payload.get("shadow_candidate_side"),
                "shadow_candidate_edge": round(float(payload.get("shadow_candidate_edge") or 0.0), 4),
                "shadow_score": round(float(payload.get("shadow_score") or 0.0), 4),
            },
            sort_keys=True,
        )
        if shadow_sig != getattr(self, "_last_shadow_live_candidate_signature", None):
            self._last_shadow_live_candidate_signature = shadow_sig
            self._db_strategy_event("SHADOW_SIGNAL_CANDIDATE_LIVE", payload)

    def _emit_entry_regime_observation(self, payload: Dict[str, Any], now_ts: float) -> None:
        observation = build_entry_regime_observation_payload(payload)
        if observation is None:
            return
        signature = json.dumps(
            {
                "slug": payload.get("slug") or "",
                "regime_tag": observation.get("regime_tag"),
                "main_candidate_outcome": observation.get("main_candidate_outcome"),
                "time_left_sec": round(float(observation.get("time_left_sec") or 0.0), 1),
                "signed_spot_minus_strike": round(
                    float(observation.get("signed_spot_minus_strike") or 0.0),
                    2,
                ),
                "main_score": round(float(observation.get("main_score") or 0.0), 3),
            },
            sort_keys=True,
        )
        last_sig = getattr(self, "_last_entry_regime_observation_signature", None)
        last_ts = float(getattr(self, "_last_entry_regime_observation_ts", 0.0))
        if signature == last_sig and (now_ts - last_ts) < 30.0:
            return
        self._last_entry_regime_observation_signature = signature
        self._last_entry_regime_observation_ts = now_ts
        observation_payload = dict(payload)
        observation_payload.update(observation)
        observation_payload["observation_ts"] = float(now_ts)
        self._db_strategy_event("ENTRY_REGIME_OBSERVATION", observation_payload)

    def _build_live_signal_compare_payload(self, now_ts: float) -> Optional[Dict[str, Any]]:
        if not getattr(self, "shadow_signal_enabled", False):
            return None
        slug = str(self.current_market_slug or "")
        if not slug:
            return None
        spot = self._capture_market_open_spot()
        if spot is None or spot <= 0:
            return None
        strike = self.market_strike_cache_by_slug.get(slug)
        up_quote = (
            self._get_quote_for_instrument(self.current_up_instrument_id)
            if self.current_up_instrument_id is not None
            else None
        )
        down_quote = (
            self._get_quote_for_instrument(self.current_down_instrument_id)
            if self.current_down_instrument_id is not None
            else None
        )
        sigma = self._estimate_external_spot_sigma_annualized() or self.maker_digital_sigma_default
        sigma = min(self.maker_digital_sigma_ceiling, max(self.maker_digital_sigma_floor, sigma))
        pricer_diag = getattr(self, "last_digital_pricer_diagnostics", {}) or {}
        diag_sigma = pricer_diag.get("sigma")
        if isinstance(diag_sigma, Decimal):
            sigma = diag_sigma
        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left_sec = max(0.0, float(end_ts - now_ts)) if end_ts is not None else 0.0
        payload = build_live_signal_compare_payload(
            slug=slug,
            spot=spot,
            strike=strike,
            sigma=sigma,
            implied_sigma=pricer_diag.get("implied_sigma"),
            sigma_before_implied_floor=pricer_diag.get("sigma_before_implied_floor"),
            implied_sigma_floor=pricer_diag.get("implied_sigma_floor"),
            implied_sigma_floor_applied=bool(pricer_diag.get("implied_sigma_floor_applied", False)),
            time_left_sec=time_left_sec,
            history=self.external_spot_history,
            now_ts=now_ts,
            active_side_value=self.active_side.value,
            active_side_locked=bool(self.active_side_locked),
            side_score=self.side_decision_score,
            side_reason=self.side_decision_reason,
            ask_up=up_quote[1] if up_quote is not None else None,
            ask_down=down_quote[1] if down_quote is not None else None,
            bid_up=up_quote[0] if up_quote is not None else None,
            bid_down=down_quote[0] if down_quote is not None else None,
            cfg=self.shadow_signal_config,
        )
        reference_source_ts = float(getattr(self, "latest_external_spot_source_ts", 0.0) or 0.0)
        reference_source_age_sec = (
            max(0.0, now_ts - reference_source_ts) if reference_source_ts > 0 else None
        )
        strike_source = str(
            self.market_strike_source_by_slug.get(slug)
            or self.market_strike_provisional_source_by_slug.get(slug)
            or ""
        )
        strike_authoritative = bool(
            strike is not None and self._is_authoritative_strike_source(strike_source)
        )
        strike_lock_state = (
            "authoritative"
            if strike_authoritative
            else "provisional"
            if strike_source
            else "unavailable"
        )
        return attach_forecast_snapshot_telemetry(
            payload,
            diagnostics=pricer_diag,
            reference_source=str(getattr(self, "latest_external_spot_source", "") or ""),
            reference_source_age_sec=reference_source_age_sec,
            strike_source=strike_source,
            strike_authoritative=strike_authoritative,
            strike_lock_state=strike_lock_state,
        )

    # Side decision methods extracted to bot/side_decision.py (SideDecisionMixin)

    def _init_live_prom_metrics(self) -> None:
        """Initialize Prometheus gauges/counters for live trading metrics."""
        try:
            from prometheus_client import Gauge, Counter
            self._prom_live_pnl = Gauge('trading_live_realized_pnl', 'Cumulative realized PnL from live trades (USDC)')
            self._prom_live_trades = Counter('trading_live_trades_total', 'Total live trades (position round-trips)')
            self._prom_live_wins = Counter('trading_live_winning_trades', 'Live winning trades')
            self._prom_live_losses = Counter('trading_live_losing_trades', 'Live losing trades')
            self._prom_live_win_rate = Gauge('trading_live_win_rate', 'Live win rate percentage')
            self._prom_live_open_pos = Gauge('trading_live_open_positions', 'Number of open positions')
            self._prom_live_inventory = Gauge('trading_live_inventory_shares', 'Current inventory in shares')
            self._live_cumulative_pnl = 0.0
            self._live_total_trades = 0
            self._live_total_wins = 0
            self._prom_live_metrics_ok = True
            logger.info("✓ Live Prometheus trading metrics initialized")
        except Exception as e:
            logger.debug(f"Failed to init live prom metrics: {e}")
            self._prom_live_metrics_ok = False

    def _push_position_closed_to_prometheus(self, realized_pnl: float, duration_ns: int) -> None:
        """Push a completed round-trip trade to Prometheus metrics."""
        if not getattr(self, '_prom_live_metrics_ok', False):
            return
        try:
            self._live_cumulative_pnl += realized_pnl
            self._live_total_trades += 1
            won = realized_pnl > 0
            if won:
                self._live_total_wins += 1
                self._prom_live_wins.inc()
            else:
                self._prom_live_losses.inc()
            self._prom_live_trades.inc()
            self._prom_live_pnl.set(self._live_cumulative_pnl)
            win_rate = (self._live_total_wins / self._live_total_trades * 100) if self._live_total_trades > 0 else 0
            self._prom_live_win_rate.set(win_rate)

            logger.info(
                f"📊 Prometheus: trade #{self._live_total_trades} pnl={realized_pnl:+.4f} "
                f"cum_pnl={self._live_cumulative_pnl:+.4f} win_rate={win_rate:.0f}%"
            )
            if self.dashboard_state is not None:
                trades = list(self.dashboard_state.trades)
                trades.insert(
                    0,
                    TradeRecord(
                        trade_id=int(self._live_total_trades),
                        market_slug=str(self.current_market_slug or ""),
                        side=getattr(self.active_side, "value", str(self.active_side)),
                        entry_price=0.0,
                        qty=1.0,
                        exit_price=float(realized_pnl),
                        redeem_amount=None,
                        is_settled=True,
                    ),
                )
                consecutive_losses = int(getattr(self.dashboard_state, "consecutive_losses", 0))
                consecutive_losses = consecutive_losses + 1 if realized_pnl < 0 else 0
                self.dashboard_state.update(
                    trades=trades[:50],
                    cumulative_pnl=float(self._live_cumulative_pnl),
                    consecutive_losses=consecutive_losses,
                )
            if self.terminal_dashboard:
                self.terminal_dashboard.record_position_closed(
                    realized_pnl=realized_pnl,
                    total_trades=self._live_total_trades,
                    win_rate=win_rate,
                )
        except Exception as e:
            logger.debug(f"Failed to push position metrics: {e}")

    def _update_inventory_metric(self) -> None:
        """Update the inventory gauge in Prometheus."""
        if not getattr(self, '_prom_live_metrics_ok', False):
            return
        try:
            self._prom_live_inventory.set(float(self.inventory_delta_shares))
        except Exception:
            pass

    def _update_terminal_dashboard_snapshot(self) -> None:
        if not self.terminal_dashboard:
            return
        try:
            buy_order_str = None
            sell_order_str = None
            for key, state in self.active_maker_orders.items():
                side = state.get("side")
                qty = state.get("token_qty", 0.0)
                price = state.get("limit_price", 0.0)
                if side == "buy":
                    buy_order_str = f"Buy {float(qty):.2f} @ ${float(price):.4f}"
                elif side == "sell":
                    sell_order_str = f"Sell {float(qty):.2f} @ ${float(price):.4f}"

            slug_str = self.current_market_slug or self.selected_slug or "-"
            strike_val = self.market_strike_cache_by_slug.get(str(slug_str))
            spot_val = self._capture_market_open_spot()
            spot_minus_strike = None
            if strike_val is not None and spot_val is not None:
                try:
                    spot_minus_strike = float(spot_val) - float(strike_val)
                except Exception:
                    spot_minus_strike = None

            self.terminal_dashboard.update(
                phase=self.market_phase.value,
                slug=slug_str,
                active_side=self.active_side.value,
                inventory_shares=float(self.inventory_delta_shares),
                wallet_balance_usdc=(
                    float(self._cached_usdc_balance) if self._cached_usdc_balance is not None else None
                ),
                active_orders=len(self.active_maker_orders),
                current_buy_order=buy_order_str,
                current_sell_order=sell_order_str,
                strike=float(strike_val) if strike_val is not None else None,
                spot=float(spot_val) if spot_val is not None else None,
                spot_minus_strike=spot_minus_strike,
            )
        except Exception as e:
            logger.debug(f"Failed to update terminal dashboard snapshot: {e}")

    def _start_terminal_dashboard_sync(self) -> None:
        if not self.terminal_dashboard:
            return
        try:
            self.terminal_dashboard.start()
            while not self._terminal_dashboard_stop_event.wait(self.terminal_dashboard_refresh_sec):
                self._refresh_balance_cache()
                self._update_terminal_dashboard_snapshot()
        except Exception as e:
            logger.error(f"Failed to start terminal dashboard: {e}")
    
    def _is_dry_run_mode(self) -> bool:
        """
        Test mode remains a safety rail, but simulation execution paths are removed.
        """
        return bool(self.test_mode)

    # ------------------------------------------------------------------
    # Binance WebSocket for real-time BTC price
    # ------------------------------------------------------------------

    # Spot pricer methods extracted to bot/spot_pricer.py (SpotPricerMixin)

    @staticmethod
    def _extract_market_start_ts_from_slug(slug: str) -> Optional[int]:
        return extract_market_start_ts_from_slug(slug)

    @staticmethod
    def _extract_price_to_beat_from_market_payload(market: Dict[str, Any]) -> Optional[Decimal]:
        return extract_price_to_beat_from_market_payload(market)

    @staticmethod
    def _resolve_btc_15m_market_slugs() -> List[str]:
        return resolve_btc_15m_market_slugs()

    @staticmethod
    def _instrument_key(instrument_id: Any) -> str:
        return str(instrument_id) if instrument_id is not None else ""

    @staticmethod
    def _normalize_side_text(side_val: Any) -> str:
        txt = str(side_val or "").strip().lower()
        if txt in {"buy", "bid"}:
            return "buy"
        if txt in {"sell", "ask"}:
            return "sell"
        if "buy" in txt:
            return "buy"
        if "sell" in txt:
            return "sell"
        if txt == "1":
            return "buy"
        if txt == "2":
            return "sell"
        return ""

    @staticmethod
    def _extract_token_id_from_instrument(instrument_id: str) -> Optional[str]:
        """
        Extract token_id from Nautilus Polymarket instrument ID:
        {condition_id}-{token_id}.POLYMARKET
        """
        m = re.search(r"-([0-9]+)\.POLYMARKET$", instrument_id)
        if not m:
            return None
        return m.group(1)

    @staticmethod
    def _extract_condition_id_from_instrument(instrument_id: Any) -> str:
        return extract_condition_id_from_instrument_id(instrument_id)

    @staticmethod
    def _extract_venue_balance_shares_from_reject(reason: str) -> Optional[Decimal]:
        txt = str(reason or "")
        m = re.search(r"balance:\s*([0-9]+)", txt)
        if not m:
            return None
        try:
            return Decimal(m.group(1)) / Decimal("1000000")
        except Exception:
            return None

    @staticmethod
    def _extract_market_slug_from_instrument(instrument: Any) -> str:
        info = getattr(instrument, "info", None) or {}
        if isinstance(info, dict):
            for key in ("market_slug", "slug", "event_slug", "marketSlug", "eventSlug"):
                s = str(info.get(key, "") or "")
                if s:
                    return s
        return ""

    def _extract_outcome_from_instrument(self, instrument: Any) -> str:
        """
        Best-effort outcome mapping (up/down) from instrument metadata.
        """
        try:
            inst_id = str(getattr(instrument, "id", "") or "")
            token_id = self._extract_token_id_from_instrument(inst_id)
            info = getattr(instrument, "info", None) or {}
            if not isinstance(info, dict):
                return ""
            tokens = info.get("tokens")
            if isinstance(tokens, list):
                for t in tokens:
                    if not isinstance(t, dict):
                        continue
                    t_id = str(t.get("token_id", "") or "")
                    if token_id and t_id == token_id:
                        return str(t.get("outcome", "") or "").strip().lower()
        except Exception:
            return ""
        return ""

    @staticmethod
    def _normalize_instrument_id(instrument_id: Any) -> Optional[InstrumentId]:
        if instrument_id is None:
            return None
        if isinstance(instrument_id, InstrumentId):
            return instrument_id
        try:
            return InstrumentId.from_str(str(instrument_id))
        except Exception:
            return None

    def _append_real_mid_price(self, instrument_id: Any, mid_price: Decimal) -> None:
        inst_key = str(instrument_id) if instrument_id is not None else ""
        self.real_price_history.append(mid_price)
        if len(self.real_price_history) > self.max_real_history:
            self.real_price_history.pop(0)
        if not inst_key:
            return
        history = self.real_price_history_by_inst.setdefault(inst_key, [])
        history.append(mid_price)
        if len(history) > self.max_real_history:
            history.pop(0)
        # Feed UP token mid into SignalEngine for market consensus signal
        up_inst = getattr(self, 'current_up_instrument_id', None)
        if (
            up_inst is not None
            and self._normalize_instrument_id(instrument_id) == up_inst
            and hasattr(self, '_signal_engine')
        ):
            import time as _time
            self._signal_engine.update_market_mid(mid_price, _time.time())

    def _reset_maker_state_for_new_market(
        self,
        prev_instrument_id: Optional[str],
        new_instrument_id: Optional[str],
        *,
        previous_slug: str = "",
        current_slug: str = "",
    ) -> None:
        """
        Per-market maker state reset.
        Inventory and kill-switch are strategy-local controls and should not carry across 15m markets.
        When the slug is unchanged (same-market rollover / side flip), preserve inventory
        tracking so SELL quotes are not incorrectly blocked.
        """
        if prev_instrument_id == new_instrument_id:
            return
        same_slug = bool(previous_slug and current_slug and previous_slug == current_slug)
        self._cancel_active_maker_orders()
        if same_slug:
            logger.info(
                f"Same-slug rollover: preserving inventory_delta_shares="
                f"{float(self.inventory_delta_shares):.6f} and live_inventory_cost "
                f"(slug={current_slug}, inst {prev_instrument_id} -> {new_instrument_id})"
            )
        else:
            self.inventory_delta_shares = Decimal("0")
            self.live_inventory_cost.clear()
            self._startup_rehydrated_inventory_force_sell_only = False
            self.position_manager.clear_all()
        self.market_cycle_realized_net_usdc = Decimal("0")
        bind_market_cycle_state(self, MarketCycleState())
        if self.maker_kill_switch and self.maker_kill_switch_reset_on_rollover:
            self.maker_kill_switch = False
            logger.warning("Maker kill switch auto-reset on market rollover.")
        self.last_quote_update_ts = 0.0
        logger.info(f"Reset maker per-market state: {prev_instrument_id} -> {new_instrument_id} (same_slug={same_slug})")

    def _project_inventory_after_fill(self, side: str, qty: Decimal, instrument_id: Optional[Any] = None) -> Decimal:
        inst_id = instrument_id if instrument_id is not None else self.instrument_id
        projected = self.inventory_delta_shares
        if side.lower() == "sell":
            projected = self._get_confirmed_inventory_qty_for_instrument(inst_id)

        # A pending BUY consumes the single per-market inventory budget even
        # when it is for the opposite outcome.  A locked-side flip can submit
        # DOWN while an UP cancellation is still awaiting venue confirmation;
        # counting only the current instrument would permit both to fill.
        target_side_str = "BUY" if side.lower() == "buy" else "SELL"
        in_flight_qty = Decimal("0")
        inst_target = str(inst_id) if inst_id else None
        
        for _, state in self.active_maker_orders.items():
            o_side = str(state.get("side", "")).upper()
            if o_side != target_side_str:
                continue

            o_inst = str(state.get("instrument_id", ""))
            # SELL capacity remains token-specific. BUY capacity is shared by
            # both outcomes of the current binary market.
            if side.lower() == "sell" and inst_target and o_inst != inst_target:
                continue

            # If the order is in our active dict, assume its REMAINING quantity is occupying inventory capacity
            # regardless of whether it lacks a VenueOrderId yet or is pending cancel.
            o_qty = Decimal(str(state.get("quantity", "0")))
            o_filled = Decimal(str(state.get("filled_qty", "0")))
            in_flight_qty += max(Decimal("0"), o_qty - o_filled)
            
        if side.lower() == "buy":
            return projected + in_flight_qty + qty
        return projected - in_flight_qty - qty

    def _maybe_auto_tune(self, now_ts: float) -> None:
        if not self.auto_tune_enabled:
            return
        if now_ts - self.last_auto_tune_ts < self.auto_tune_interval_sec:
            return
        metrics = self.rebate_reporter.get_current_metrics()
        suggestion = self.parameter_tuner.suggest(
            current_half_spread=self.maker_half_spread,
            current_min_expected_net=self.maker_min_expected_net_usdc,
            metrics=metrics,
        )
        new_half = suggestion["maker_half_spread"]
        new_min = suggestion["maker_min_expected_net_usdc"]
        if new_half != self.maker_half_spread or new_min != self.maker_min_expected_net_usdc:
            logger.info(
                "Auto-tune update: "
                f"half_spread {float(self.maker_half_spread):.6f}->{float(new_half):.6f}, "
                f"min_net {float(self.maker_min_expected_net_usdc):.6f}->{float(new_min):.6f}"
            )
        self.maker_half_spread = new_half
        self.maker_min_expected_net_usdc = new_min
        # Keep decoupled maker engine config in sync with auto-tuned values.
        self.maker_engine.config.maker_half_spread = new_half
        self.maker_engine.config.maker_min_expected_net_usdc = new_min
        self.last_auto_tune_ts = now_ts

    def _maker_quote_instruments(self) -> List[InstrumentId]:
        instruments: List[InstrumentId] = []
        seen: Set[str] = set()

        def _append(inst: Optional[InstrumentId]) -> None:
            if inst is None:
                return
            key = self._instrument_key(inst)
            if not key or key in seen:
                return
            instruments.append(inst)
            seen.add(key)

        if self.bi_side_enabled:
            active_inst = self._instrument_for_side(self.active_side)
            if self.active_side != ActiveSide.NONE and active_inst is not None:
                _append(active_inst)
        elif self.instrument_id is not None:
            _append(self.instrument_id)

        # Always include legs with confirmed inventory so they remain in the
        # normal maker requote loop even after active-side flips.
        for inst_key, state in list(self.live_inventory_cost.items()):
            try:
                qty = Decimal(str(state.get("qty", "0")))
            except Exception:
                qty = Decimal("0")
            if qty <= 0:
                continue
            inst = self._normalize_instrument_id(inst_key)
            if inst is not None:
                _append(inst)

        # Preserve instruments that recently failed SELL due to venue balance lag.
        for inst_key in list(self._sell_recovery_required_by_inst.keys()):
            inst = self._normalize_instrument_id(inst_key)
            if inst is not None:
                _append(inst)

        return instruments

    def _record_entry_decision_trace(
        self,
        *,
        now_ts: float,
        inst_id: Any,
        side: str,
        should_quote: bool,
        reason: str = "",
        source_event_type: str = "",
        shadow_only: bool = False,
        entry_mode: str = "",
        fair: Any = None,
        entry_price: Any = None,
        robust_net_usdc: Any = None,
        fee_per_share: Any = None,
        planned_quantity: Any = None,
        time_left_sec: Optional[float] = None,
    ) -> None:
        """Record the existing BUY decision path without changing its outcome."""
        if side != "buy" or not self.trade_db:
            return

        def _as_float(value: Any) -> Optional[float]:
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        decision = EntryDecision.observe(
            slug=str(self.current_market_slug or ""),
            instrument_id=inst_id,
            side=side,
            should_quote=should_quote,
            reason=reason,
            source_event_type=source_event_type,
            shadow_only=shadow_only,
            entry_mode=entry_mode,
            time_left_sec=_as_float(time_left_sec),
            side_score=_as_float(self.side_decision_score),
            fair=_as_float(fair),
            entry_price=_as_float(entry_price),
            robust_net_usdc=_as_float(robust_net_usdc),
            # ``fair`` has already passed probability calibration upstream.
            calibrated_probability=_as_float(fair),
            fee_per_share=_as_float(fee_per_share),
            planned_quantity=_as_float(planned_quantity),
        )
        payload = decision.to_payload()
        payload["decision_stage"] = "pre_submit"
        signature = (
            payload["slug"],
            payload["state"],
            payload["layer"],
            payload["final_reason"],
            payload["entry_mode"],
            payload["entry_price"],
            payload["fair"],
            payload["robust_net_usdc"],
            payload["settlement_ev_per_share"],
            payload["planned_quantity"],
        )
        if not hasattr(self, "_last_entry_decision_trace_signature_by_inst"):
            self._last_entry_decision_trace_signature_by_inst = {}
            self._last_entry_decision_trace_ts_by_inst = {}
        inst_key = str(inst_id)
        last_signature = self._last_entry_decision_trace_signature_by_inst.get(inst_key)
        last_ts = float(self._last_entry_decision_trace_ts_by_inst.get(inst_key, 0.0))
        if signature == last_signature and (now_ts - last_ts) < 1.0:
            return
        self._last_entry_decision_trace_signature_by_inst[inst_key] = signature
        self._last_entry_decision_trace_ts_by_inst[inst_key] = now_ts
        self._db_strategy_event("ENTRY_DECISION_TRACE", payload)

    async def _evaluate_quote_targets(
        self,
        *,
        phase: MarketPhase,
        forced_sell_only: bool,
        regime_guard_active: bool,
        now_ts: float,
        recent_vol: Optional[Decimal],
        target_instruments: List[Any],
        end_ts: Optional[float],
        time_left_sec_global: Optional[float],
    ) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        desired_quotes: Dict[str, Dict[str, Any]] = {}
        diag_context_by_inst: Dict[str, Dict[str, Any]] = {}

        for inst_id in target_instruments:
            quote_ctx = await build_quote_instrument_context(
                inst_id=inst_id,
                normalize_instrument_id_fn=self._normalize_instrument_id,
                instrument_key_fn=self._instrument_key,
                get_quote_for_instrument_fn=self._get_quote_for_instrument,
                compute_fair_probability_fn=self._compute_fair_probability,
                cache_instrument_fn=self.cache.instrument,
                extract_token_id_fn=self._extract_token_id_from_instrument,
                get_dynamic_fee_rate_fn=self._get_dynamic_fee_rate,
                get_orderbook_levels_fn=self._get_orderbook_levels_for_token,
                latest_quote_depth_by_inst=self.latest_quote_depth_by_inst,
                latest_quote_ts_by_inst=getattr(self, "last_quote_update_ts_by_inst", {}),
                maker_econ_fee_rate_decimal=self.maker_econ_fee_rate_decimal,
            )
            inst_key = quote_ctx.inst_key
            diag_context_by_inst[inst_key] = quote_ctx.diag_context
            if quote_ctx.quote is None or quote_ctx.fair is None:
                continue
            inst_bid, inst_ask = quote_ctx.quote
            # Executable fair is the tradable market consensus until a
            # chronological OOS calibration demonstrates an improvement.
            # Keep the digital result only as research telemetry.
            raw_fair = quote_ctx.fair
            market_mid = (inst_bid + inst_ask) / Decimal("2")
            fair = market_mid
            quote_ctx.fair = fair
            quote_ctx.diag_context.update({
                "raw_fair": raw_fair,
                "calibrated_fair": fair,
                "market_mid": market_mid,
                "probability_calibration_mode": "market_mid_canonical",
            })
            self.rebate_reporter.record_api_health(self.fee_rate_client.get_health_snapshot())
            if quote_ctx.dynamic_fee_rate is not None:
                pass

            side_plan = self.maker_engine.generate_quote_plan(
                inst_bid=inst_bid,
                inst_ask=inst_ask,
                fair_price=fair,
                fee_rate=quote_ctx.fee_rate_val,
                inventory_delta_shares=self.inventory_delta_shares,
                inventory_last_update_ts=self.inventory_last_update_ts,
                current_time_ts=now_ts,
                tick_size=quote_ctx.tick,
                # Never calculate a token's volatility from an interleaved
                # UP/DOWN series.  The two outcome tokens are complementary.
                recent_vol=self._compute_recent_volatility(inst_id),
                balance_forced_sell_only=forced_sell_only,
                bid_depth=quote_ctx.bid_depth,
                ask_depth=quote_ctx.ask_depth,
                bid_levels=quote_ctx.bid_levels,
                ask_levels=quote_ctx.ask_levels,
            )
            if not side_plan:
                diag_context_by_inst[inst_key]["reason"] = "invalid_quote_plan"
                continue

            momentum_history = self._momentum_history_for_instrument(inst_id)
            guard_outcome = apply_quote_plan_guards(
                side_plan=side_plan,
                quote_sides_mode=self.maker_quote_sides,
                phase_value=phase.value,
                inventory_delta_shares=self.inventory_delta_shares,
                early_sell_only_sec=float(self.maker_early_sell_only_sec),
                time_left_sec_global=time_left_sec_global,
                now_ts=now_ts,
                buy_cooldown_until_ts=float(self.buy_cooldown_until_ts),
                momentum_buy_filter_pct=self.maker_momentum_buy_filter_pct,
                momentum_sell_filter_pct=self.maker_momentum_sell_filter_pct,
                momentum_window_ticks=self.maker_momentum_window_ticks,
                momentum_history=momentum_history,
                fair=fair,
                min_fair_price=self.maker_min_fair_price,
                max_fair_price=self.maker_max_fair_price,
                end_ts=end_ts,
                min_minutes_to_close=self.maker_min_minutes_to_close,
                reduce_only_no_new_sell_last_sec=self.maker_reduce_only_no_new_sell_last_sec,
                forced_sell_only=forced_sell_only,
                active_side=self.active_side.value,
            )
            side_disable_reason_by_side = guard_outcome.side_disable_reason_by_side
            reduce_only_reason = guard_outcome.reduce_only.reason
            reduce_only_tail_sell_block = guard_outcome.reduce_only.tail_sell_block
            reduce_only_tail_sec_left = guard_outcome.reduce_only.tail_sec_left

            if guard_outcome.buy_cooldown_remaining is not None:
                remaining = guard_outcome.buy_cooldown_remaining
                if not getattr(self, "_logged_buy_cd", False) or time.time() - getattr(self, "_last_buy_cd_log_ts", 0) > 30:
                    logger.info(f"Post-fill buy cooldown active: {remaining:.1f}s remaining")
                    self._logged_buy_cd = True
                    self._last_buy_cd_log_ts = time.time()
            elif getattr(self, "_logged_buy_cd", False):
                self._logged_buy_cd = False
                logger.info("Post-fill buy cooldown cleared.")



            if guard_outcome.momentum_buy_blocked and guard_outcome.momentum_trend_pct is not None:
                if not getattr(self, "_logged_mom_buy", False) or time.time() - getattr(self, "_last_mom_ts", 0) > 30:
                    threshold_pct = (
                        float((guard_outcome.momentum_buy_threshold_pct or Decimal("0")) * 100)
                        if guard_outcome.momentum_buy_threshold_pct is not None
                        else 0.0
                    )
                    logger.warning(
                        "Trend Protection: momentum filter "
                        f"(dropped {float(guard_outcome.momentum_trend_pct * 100):.1f}% <= -{threshold_pct:.1f}%). "
                        "Blocking BUY orders."
                    )
                    self._logged_mom_buy = True
                    self._last_mom_ts = time.time()
            elif "buy" in side_plan and getattr(self, "_logged_mom_buy", False):
                self._logged_mom_buy = False

            has_sellable_inventory_context = self.inventory_delta_shares > 0
            if (
                guard_outcome.momentum_sell_blocked
                and guard_outcome.momentum_trend_pct is not None
                and has_sellable_inventory_context
            ):
                if not getattr(self, "_logged_mom_sell", False) or time.time() - getattr(self, "_last_mom_ts_s", 0) > 30:
                    threshold_pct = (
                        float((guard_outcome.momentum_sell_threshold_pct or Decimal("0")) * 100)
                        if guard_outcome.momentum_sell_threshold_pct is not None
                        else 0.0
                    )
                    logger.warning(
                        "Trend Protection: momentum filter "
                        f"(pumped {float(guard_outcome.momentum_trend_pct * 100):.1f}% >= +{threshold_pct:.1f}%). "
                        "Blocking SELL orders."
                    )
                    self._logged_mom_sell = True
                    self._last_mom_ts_s = time.time()
            elif getattr(self, "_logged_mom_sell", False):
                self._logged_mom_sell = False

            if reduce_only_reason:
                if "buy" in side_plan:
                    if not getattr(self, "_logged_reduce_only", False) or time.time() - getattr(self, "_last_ro_log_ts", 0) > 60:
                        logger.warning(f"Maker Reduce-Only active ({reduce_only_reason}). Blocking BUY orders.")
                        self._logged_reduce_only = True
                        self._last_ro_log_ts = time.time()
                if "sell" in side_plan and reduce_only_tail_sell_block and self.inventory_delta_shares <= 0:
                    if (
                        not getattr(self, "_logged_reduce_only_tail_sell_block", False)
                        or time.time() - getattr(self, "_last_ro_tail_sell_log_ts", 0) > 60
                    ):
                        logger.warning(
                            "Maker Reduce-Only tail guard active "
                            f"({reduce_only_tail_sec_left:.1f}s left <= {self.maker_reduce_only_no_new_sell_last_sec}s). "
                            "Blocking new SELL quotes."
                        )
                        self._logged_reduce_only_tail_sell_block = True
                        self._last_ro_tail_sell_log_ts = time.time()
                elif "sell" in side_plan:
                    self._logged_reduce_only_tail_sell_block = False
            elif "buy" in side_plan and getattr(self, "_logged_reduce_only", False):
                self._logged_reduce_only = False
                self._logged_extreme_sell_block = False
                self._logged_reduce_only_tail_sell_block = False

            live_shadow_payload = None
            for side, quote_data in side_plan.items():
                order_key = self._order_key_for(side, inst_id)
                inst_key = self._instrument_key(inst_id)
                current_slug = str(self.current_market_slug or "")
                thesis_epoch = self._current_thesis_epoch(current_slug)
                market_buy_budget_key = self._market_buy_budget_key(current_slug)
                market_stop_loss_count = int(self.market_stop_loss_count_by_slug.get(current_slug, 0))
                market_buy_count = max(
                    int(self.market_buy_count_by_slug.get(market_buy_budget_key, 0)),
                    int(getattr(self, "market_buy_count_total_by_slug", {}).get(current_slug, 0)),
                )
                if (
                    side == "buy"
                    and current_slug
                    and self.market_stop_loss_max_per_market > 0
                    and market_stop_loss_count >= self.market_stop_loss_max_per_market
                ):
                    self._db_order_event(
                        event_type="ORDER_SKIP_MARKET_STOP_LOSS_LIMIT",
                        side=side.upper(),
                        status="SKIPPED",
                        reason="market_stop_loss_limit",
                        payload={
                            "slug": current_slug,
                            "instrument_id": str(inst_id),
                            "market_stop_loss_count": market_stop_loss_count,
                            "market_stop_loss_max_per_market": self.market_stop_loss_max_per_market,
                        },
                    )
                    continue
                if (
                    side == "buy"
                    and current_slug
                    and self.market_max_buy_events_per_market > 0
                    and market_buy_count >= self.market_max_buy_events_per_market
                ):
                    self._db_order_event(
                        event_type="ORDER_SKIP_MARKET_BUY_LIMIT",
                        side=side.upper(),
                        status="SKIPPED",
                        reason="market_buy_limit",
                        payload={
                            "slug": current_slug,
                            "thesis_epoch": thesis_epoch,
                            "budget_key": market_buy_budget_key,
                            "instrument_id": str(inst_id),
                            "market_buy_count": market_buy_count,
                            "market_max_buy_events_per_market": self.market_max_buy_events_per_market,
                        },
                    )
                    continue
                if inst_key in self.pending_taker_exit_by_inst:
                    self._db_order_event(
                        event_type="ORDER_SKIP_PENDING_TAKER_EXIT",
                        side=side.upper(),
                        status="SKIPPED",
                        reason="pending_taker_exit",
                        payload={
                            "instrument_id": str(inst_id),
                            "pending_taker_exit_client_order_id": self.pending_taker_exit_by_inst.get(inst_key),
                        },
                    )
                    continue
                sell_pause_until = float(self._sell_reject_pause_until_by_inst.get(inst_key, 0.0))
                sellable_qty = None
                confirmed_inventory_qty = Decimal("0")
                other_held_inventory_qty = Decimal("0")
                if side == "sell":
                    confirmed_inventory_qty = self._get_confirmed_inventory_qty_for_instrument(inst_id)
                    for held_key, held_state in list(self.live_inventory_cost.items()):
                        if held_key == inst_key:
                            continue
                        try:
                            held_qty = Decimal(str(held_state.get("qty", "0")))
                        except Exception:
                            held_qty = Decimal("0")
                        if held_qty > 0:
                            other_held_inventory_qty += held_qty
                    recent_buy_ts = float(self.recent_buy_fill_ts_by_inst.get(inst_key, 0.0))
                    if recent_buy_ts > 0 and self.sell_delay_after_buy_sec > 0:
                        sell_pause_until = max(
                            sell_pause_until,
                            recent_buy_ts + float(self.sell_delay_after_buy_sec),
                        )
                if side == "sell" and not self._is_dry_run_mode():
                    sellable_qty = self._get_effective_sellable_qty(instrument_id=inst_id)
                inv_state = self.live_inventory_cost.get(inst_key) if inst_key else None
                avg_entry = (
                    Decimal(str(inv_state.get("avg_entry_price", "0")))
                    if inv_state is not None
                    else Decimal("0")
                )
                current_inst_inventory_qty = (
                    Decimal(str(inv_state.get("qty", "0")))
                    if inv_state is not None
                    else Decimal("0")
                )
                if (
                    live_shadow_payload is None
                    and self.active_side_locked
                    and self.active_side != ActiveSide.NONE
                    and side in {"buy", "sell"}
                ):
                    live_shadow_payload = self._build_live_signal_compare_payload(now_ts)
                try:
                    market_consensus_score = Decimal(
                        str((self.side_decision_inputs or {}).get("market_consensus", "0"))
                    )
                except Exception:
                    market_consensus_score = Decimal("0")
                tail_inventory_exit_context = bool(
                    side == "sell"
                    and current_inst_inventory_qty > 0
                    and (
                        phase == MarketPhase.REDUCE_ONLY
                        or self._is_emergency_exit_window(time_left_sec_global)
                    )
                )
                current_price = self._capture_market_open_spot()
                price_to_beat = self.market_strike_cache_by_slug.get(str(self.current_market_slug or ""))
                market_strike_status = str(
                    getattr(self, "market_strike_status_by_slug", {}).get(current_slug, "pending")
                )
                market_strike_source = str(
                    self.market_strike_source_by_slug.get(current_slug, "")
                )
                market_strike_entry_eligible = bool(
                    getattr(self, "_market_strike_is_entry_eligible", lambda _slug: False)(current_slug)
                )
                locked_side_runtime = self._resolve_locked_side_runtime_state(
                    now_ts=now_ts,
                    slug=current_slug,
                    inst_id=inst_id,
                    fair=quote_ctx.fair,
                    current_price=current_price,
                    price_to_beat=price_to_beat,
                    inventory_qty=current_inst_inventory_qty,
                    time_left_sec=time_left_sec_global,
                    shadow_payload=live_shadow_payload,
                )
                candidate_robust_net = (
                    quote_data[3]
                    if isinstance(quote_data, (tuple, list)) and len(quote_data) > 3
                    else None
                )
                regime_economics = {"applied": False, "reason": "not_buy"}
                # Midpoint remains the canonical quoted fair.  Only the
                # already-measured strong directional regime may replace the
                # *economics* estimate, and only for a flat first entry.
                if side == "buy" and market_buy_count == 0 and current_inst_inventory_qty <= 0:
                    try:
                        outcome_side = self._side_for_instrument_id(inst_id).value
                    except Exception:
                        outcome_side = self.active_side.value
                    quote_data, regime_economics = apply_strong_directional_regime_economics(
                        tuple(quote_data),
                        active_side=self.active_side.value,
                        outcome_side=outcome_side,
                        side_locked=bool(self.active_side_locked),
                        side_score=Decimal(str(self.side_decision_score)),
                        time_left_sec=time_left_sec_global,
                        spot=current_price,
                        strike=price_to_beat,
                        calibrations=getattr(self, "strong_directional_regime_calibration", None),
                        markout_calibrations=getattr(self, "maker_buy_markout_calibrations", None),
                        min_expected_net_usdc=self.maker_min_expected_net_usdc,
                    )
                    candidate_robust_net = quote_data[3]
                buy_entry_eval = evaluate_buy_entry_controls(
                    side=side,
                    bi_side_enabled=self.bi_side_enabled,
                    active_side_locked=self.active_side_locked,
                    active_side_value=self.active_side.value,
                    locked_side_entry_blocked=locked_side_runtime.entry_blocked,
                    locked_side_entry_block_reason=locked_side_runtime.entry_block_reason,
                    side_score=self.side_decision_score,
                    directional_entry_min_score_abs_new=self.directional_entry_min_score_abs_new,
                    directional_first_entry_min_score_abs_new=self.directional_first_entry_min_score_abs_new,
                    first_entry_max_time_left_sec=int(getattr(self, "first_entry_max_time_left_sec", 0)),
                    locked_side_score_abs=Decimal(str(getattr(self, "active_side_lock_score_abs", Decimal("0")))),
                    maker_min_expected_net_usdc=self.maker_min_expected_net_usdc,
                    current_inst_inventory_qty=current_inst_inventory_qty,
                    max_locked_side_position=self.max_locked_side_position,
                    inventory_full_behavior=self.inventory_full_behavior,
                    twap_reference_degraded=bool(
                        getattr(self, "twap_degraded_block_new_entries", True)
                        and getattr(self, "_twap_reference_degraded", False)
                    ),
                    current_slug=current_slug,
                    inst_id=inst_id,
                    market_buy_count=market_buy_count,
                    time_left_sec=time_left_sec_global,
                    best_bid=quote_ctx.quote[0] if quote_ctx.quote is not None else None,
                    fair=quote_ctx.fair,
                    candidate_entry_price=quote_ctx.quote[0] if quote_ctx.quote is not None else None,
                    robust_net_usdc=candidate_robust_net,
                    shadow_payload=live_shadow_payload,
                    entry_quality_allow_size_down=bool(
                        getattr(self, "entry_quality_allow_size_down", False)
                    ),
                    market_strike_entry_eligible=market_strike_entry_eligible,
                    market_strike_status=market_strike_status,
                    market_strike_source=market_strike_source,
                )
                # Defaults keep rejected candidates observable even when the
                # quote context is incomplete and therefore has no quote plan.
                quote_plan = None
                planned_entry_price = None
                planned_fee_ps = Decimal("0")
                if side == "buy" and quote_ctx.quote is not None and quote_ctx.fair is not None:
                    try:
                        outcome_side = self._side_for_instrument_id(inst_id).value
                    except Exception:
                        outcome_side = self.active_side.value
                    quote_bid, quote_ask = quote_ctx.quote
                    market_mid_outcome = (quote_bid + quote_ask) / Decimal("2")
                    spread_ps = max(Decimal("0"), quote_ask - quote_bid)
                    relative_spread = spread_ps / market_mid_outcome if market_mid_outcome > 0 else None
                    model_probability_outcome = quote_ctx.fair
                    if outcome_side == ActiveSide.DOWN.value:
                        model_probability_up = Decimal("1") - model_probability_outcome
                        market_mid_up = Decimal("1") - market_mid_outcome
                        market_mid_source = "synthetic_from_down_book"
                        up_bid = None
                        up_ask = None
                        down_bid = quote_bid
                        down_ask = quote_ask
                    else:
                        model_probability_up = model_probability_outcome
                        market_mid_up = market_mid_outcome
                        market_mid_source = "direct_up_book"
                        up_bid = quote_bid
                        up_ask = quote_ask
                        down_bid = None
                        down_ask = None
                    quote_plan = parse_quote_plan(quote_data)
                    planned_fee_ps = quote_plan.fee_per_share if quote_plan is not None else Decimal("0")
                    planned_other_cost_ps = quote_plan.other_cost_per_share if quote_plan is not None else Decimal("0")
                    # This is intentionally sampled at emission time. ``now_ts`` is
                    # the quote-cycle start and may predate a quote received while
                    # asynchronous quote evaluation is still running.
                    observation_ts = time.time()
                    quote_age = build_quote_age_telemetry(
                        observation_ts=observation_ts,
                        quote_ts=quote_ctx.quote_ts,
                        clock_skew_tolerance_sec=getattr(
                            self,
                            "quote_event_clock_skew_tolerance_sec",
                            Decimal("0.25"),
                        ),
                    )
                    quote_age_sec = quote_age.effective_age_sec
                    edge_state = build_edge_state(
                        model_probability_up=model_probability_up,
                        market_mid=market_mid_up,
                        up_bid=up_bid,
                        up_ask=up_ask,
                        down_bid=down_bid,
                        down_ask=down_ask,
                        fee_buffer=planned_fee_ps,
                        slippage_buffer=planned_other_cost_ps,
                        quote_age_sec=quote_age_sec,
                        up_quote_age_sec=quote_age_sec if outcome_side == ActiveSide.UP.value else None,
                        down_quote_age_sec=quote_age_sec if outcome_side == ActiveSide.DOWN.value else None,
                        max_quote_age_sec=Decimal(str(getattr(self, "quote_stale_sec", 2.0))),
                    )
                    planned_entry_price = quote_plan.price if quote_plan is not None else None
                    planned_maker_net_edge = None
                    if planned_entry_price is not None:
                        planned_maker_net_edge = (
                            model_probability_outcome
                            - planned_entry_price
                            - planned_fee_ps
                            - planned_other_cost_ps
                        )
                    complementary_ask_sum = None
                    complementary_bid_sum = None
                    complementary_quote_ts = None
                    try:
                        other_side = ActiveSide.DOWN if outcome_side == ActiveSide.UP.value else ActiveSide.UP
                        other_inst_id = self._instrument_for_side(other_side)
                        other_quote = getattr(self, "latest_quote_by_inst", {}).get(str(other_inst_id))
                        other_quote_ts = getattr(self, "last_quote_update_ts_by_inst", {}).get(str(other_inst_id))
                        if other_quote is not None:
                            other_bid, other_ask = other_quote
                            complementary_bid_sum = quote_bid + other_bid
                            complementary_ask_sum = quote_ask + other_ask
                            complementary_quote_ts = other_quote_ts
                    except Exception:
                        pass
                    edge_payload = {
                        **edge_state.to_dict(),
                        "event_ts": observation_ts,
                        "quote_ts": quote_ctx.quote_ts,
                        "quote_age_raw_sec": (
                            float(quote_age.raw_age_sec)
                            if quote_age.raw_age_sec is not None
                            else None
                        ),
                        "quote_clock_skew_sec": (
                            float(quote_age.clock_skew_sec)
                            if quote_age.clock_skew_sec is not None
                            else None
                        ),
                        "quote_timestamp_tolerance_applied": quote_age.tolerance_applied,
                        "slug": current_slug,
                        "instrument_id": str(inst_id),
                        "outcome_side": outcome_side,
                        "observed_side": outcome_side,
                        "observed_outcome_mid": float(market_mid_outcome),
                        "market_mid_source": market_mid_source,
                        "spread_ps": float(spread_ps),
                        "relative_spread": float(relative_spread) if relative_spread is not None else None,
                        "complementary_bid_sum": float(complementary_bid_sum) if complementary_bid_sum is not None else None,
                        "complementary_ask_sum": float(complementary_ask_sum) if complementary_ask_sum is not None else None,
                        "complementary_quote_ts": complementary_quote_ts,
                        "complementary_quote_age_sec": (
                            float(observation_ts - complementary_quote_ts)
                            if complementary_quote_ts is not None
                            else None
                        ),
                        "buffer_mode": "untrained_shadow",
                        "entry_mode": buy_entry_eval.entry_mode,
                        "time_left_sec": time_left_sec_global,
                        "candidate_entry_price_per_share": float(planned_entry_price) if planned_entry_price is not None else None,
                        "planned_maker_net_edge_per_share": float(planned_maker_net_edge) if planned_maker_net_edge is not None else None,
                        "planned_fee_per_share": float(planned_fee_ps),
                        "planned_other_cost_per_share": float(planned_other_cost_ps),
                        "execution_penalty_components": {
                            key: float(value)
                            for key, value in quote_plan.execution_penalty_components.items()
                        },
                        "strong_directional_regime": {
                            key: float(value) if isinstance(value, Decimal) else value
                            for key, value in regime_economics.items()
                        },
                        "decision": "SKIP" if buy_entry_eval.skip else "ELIGIBLE",
                        "decision_reason": buy_entry_eval.reason or "shadow_only",
                        "loss_history_tail": list(getattr(self, "recent_fill_pnl_results", [])[-5:]),
                        "recovery_size_multiplier": float(getattr(self, "loss_recovery_size_multiplier", 1.0)),
                        "recovery_min_edge_addition": float(getattr(self, "loss_recovery_min_edge_addition", 0)),
                        "effective_size_multiplier": float(
                            buy_entry_eval.size_multiplier
                            * Decimal(str(getattr(self, "loss_recovery_size_multiplier", 1.0)))
                        ),
                        "effective_min_expected_net_usdc": float(
                            buy_entry_eval.min_expected_net_usdc
                            + Decimal(str(getattr(self, "loss_recovery_min_edge_addition", 0)))
                        ),
                    }
                    if not hasattr(self, "_last_edge_observation_signature_by_inst"):
                        self._last_edge_observation_signature_by_inst = {}
                    if not hasattr(self, "_last_edge_observation_ts_by_inst"):
                        self._last_edge_observation_ts_by_inst = {}
                    edge_signature = (
                        current_slug,
                        str(inst_id),
                        str(planned_entry_price),
                        str(model_probability_outcome),
                        buy_entry_eval.entry_mode,
                        buy_entry_eval.skip,
                    )
                    should_emit_edge = should_emit_edge_observation(
                        str(inst_id),
                        edge_signature,
                        now_ts,
                        self._last_edge_observation_signature_by_inst,
                        self._last_edge_observation_ts_by_inst,
                        min_interval_sec=1.0,
                    )
                    if should_emit_edge:
                        self._db_order_event(
                            event_type="ENTRY_EDGE_OBSERVATION",
                            side=side.upper(),
                            status="SHADOW",
                            reason="shadow_only",
                            payload=edge_payload,
                        )
                min_expected_net_usdc = buy_entry_eval.min_expected_net_usdc
                if buy_entry_eval.skip:
                    diag_payload = buy_entry_eval.payload or {}
                    self._record_entry_decision_trace(
                        now_ts=now_ts,
                        inst_id=inst_id,
                        side=side,
                        should_quote=False,
                        reason=buy_entry_eval.reason,
                        source_event_type=buy_entry_eval.event_type,
                        shadow_only=buy_entry_eval.shadow_only,
                        entry_mode=buy_entry_eval.entry_mode,
                        fair=quote_ctx.fair,
                        entry_price=(
                            planned_entry_price
                            if planned_entry_price is not None
                            else (quote_ctx.quote[0] if quote_ctx.quote is not None else None)
                        ),
                        robust_net_usdc=candidate_robust_net,
                        fee_per_share=planned_fee_ps,
                        planned_quantity=(quote_plan.quantity if quote_plan is not None else None),
                        time_left_sec=time_left_sec_global,
                    )
                    self._db_buy_path_diagnostic(
                        event_type=buy_entry_eval.event_type,
                        side=side.upper(),
                        status="SKIPPED",
                        reason=buy_entry_eval.reason,
                        payload=diag_payload,
                    )
                    continue
                # Determine if the directional thesis has weakened against our position.
                # Loss-selling should be allowed more aggressively when we are confirmed
                # offside against a locked side decision, even if cost-protect would
                # normally block the new SELL price.
                _thesis_weakened = False
                _offside_confirmed = False
                _stop_loss_regime_armed = False
                decision_state = None
                hold_sec = 0.0
                spot_still_supports_position = False
                stop_loss_pending_active = bool(self._stop_loss_execution_priority_by_inst.get(inst_key, False))
                if (
                    side == "sell"
                    and self.inventory_delta_shares > 0
                    and self.bi_side_enabled
                ):
                    target_active_inst = self._instrument_for_side(self.active_side)
                    if (
                        self.active_side_locked
                        and self.active_side != ActiveSide.NONE
                        and target_active_inst is not None
                        and target_active_inst != inst_id
                    ):
                        _offside_confirmed = True
                    legacy_thesis_weakened = self._assess_thesis_weakened(
                        inst_id=inst_id,
                        now_ts=now_ts,
                        side_score=self.side_decision_score,
                    )
                    _thesis_weakened = confirmed_adverse_exit(
                        active_side_value=self.active_side.value,
                        active_side_locked=bool(self.active_side_locked),
                        legacy_thesis_weakened=legacy_thesis_weakened,
                        market_consensus=market_consensus_score,
                        shadow_payload=live_shadow_payload,
                    )
                    if (
                        hasattr(self, "position_manager")
                        and inv_state is not None
                    ):
                        opened_ts = float(inv_state.get("opened_ts", 0.0))
                        hold_sec = max(0.0, now_ts - opened_ts) if opened_ts > 0 else 0.0
                        held_side = (
                            self._side_for_instrument_id(inst_id).value
                            if hasattr(self, "_side_for_instrument_id")
                            else "NONE"
                        )
                        matches_position = self._instrument_for_side(self.active_side) == inst_id
                        if (
                            matches_position
                            and current_price is not None
                            and price_to_beat is not None
                            and self.active_side is not None
                        ):
                            spot_still_supports_position = locked_side_runtime.spot_supports
                        peak_bid = self.maker_profit_run_peak_bid_by_inst.get(inst_key)
                        peak_fair = self.maker_profit_run_peak_fair_by_inst.get(inst_key)
                        if quote_ctx.quote is not None:
                            decision_state = self.position_manager.compute_decision_state(
                                inst_key=inst_key,
                                now_ts=now_ts,
                                qty=current_inst_inventory_qty,
                                opened_ts=opened_ts,
                                held_side=held_side,
                                active_side=self.active_side.value,
                                signal_score=self.side_decision_score,
                                signal_matches_position=matches_position,
                                current_price=current_price,
                                price_to_beat=price_to_beat,
                                best_bid=quote_ctx.quote[0],
                                best_ask=quote_ctx.quote[1],
                                fair=quote_ctx.fair,
                                time_left_sec=time_left_sec_global,
                                avg_entry=avg_entry,
                                peak_bid=peak_bid,
                                peak_fair=peak_fair,
                            )
                        regime = self.position_manager.assess_stop_loss_regime(
                            inst_key=inst_key,
                            now_ts=now_ts,
                            qty=current_inst_inventory_qty,
                            opened_ts=opened_ts,
                            held_side=held_side,
                            signal_active_side=self.active_side.value,
                            signal_score=self.side_decision_score,
                            signal_matches_position=matches_position,
                            force_exit=False,
                            current_price=current_price,
                            price_to_beat=price_to_beat,
                            best_bid=quote_ctx.quote[0] if quote_ctx.quote is not None else Decimal("0"),
                            best_ask=quote_ctx.quote[1] if quote_ctx.quote is not None else Decimal("0"),
                            fair=quote_ctx.fair,
                            time_left_sec=time_left_sec_global,
                            avg_entry=avg_entry,
                            peak_bid=peak_bid,
                            peak_fair=peak_fair,
                            precomputed_decision_state=decision_state,
                        )
                        _stop_loss_regime_armed = regime.status == "armed"
                        if decision_state is not None:
                            # Keep decision_state as a diagnostic/sizing input, but do not let
                            # DE_RISK/CHOP alone become an exit authority for fresh/profitable
                            # positions. Only a confirmed EXIT phase can contribute to
                            # thesis-weakened escalation here.
                            _phase_is_adverse = decision_state.phase == DecisionPhase.EXIT
                            # Signal flip cooldown: require N consecutive adverse cycles
                            # before overriding thesis_weakened, to prevent BTC pulse
                            # from triggering premature loss sells.
                            if _phase_is_adverse:
                                self._maker_signal_flip_hits[inst_key] = (
                                    self._maker_signal_flip_hits.get(inst_key, 0) + 1
                                )
                            else:
                                self._maker_signal_flip_hits[inst_key] = 0
                            _flip_confirmed = (
                                self._maker_signal_flip_hits.get(inst_key, 0)
                                >= self.maker_signal_flip_cooldown_cycles
                            )
                            if _phase_is_adverse and _flip_confirmed:
                                _thesis_weakened = True
                                _offside_confirmed = _offside_confirmed or (
                                    decision_state.phase == DecisionPhase.EXIT
                                    and not matches_position
                                )
                                _stop_loss_regime_armed = (
                                    _stop_loss_regime_armed
                                    or decision_state.phase == DecisionPhase.EXIT
                                )
                    if locked_side_runtime.invalidation_confirmed and current_inst_inventory_qty > 0:
                        _offside_confirmed = True
                if quote_ctx.quote is not None:
                        self._update_profit_run_peaks(
                            inst_id,
                            best_bid=quote_ctx.quote[0],
                            fair=quote_ctx.fair,
                        )

                desired_entry = build_desired_quote_entry(
                    order_key=order_key,
                    side=side,
                    inst_id=inst_id,
                    quote_data=quote_data,
                    side_disable_reason_by_side=side_disable_reason_by_side,
                    reduce_only_reason=reduce_only_reason,
                    reduce_only_tail_sell_block=reduce_only_tail_sell_block,
                    reduce_only_no_new_sell_last_sec=self.maker_reduce_only_no_new_sell_last_sec,
                    forced_sell_only=forced_sell_only,
                    min_expected_net_usdc=(
                        min_expected_net_usdc
                        + (
                            Decimal(str(getattr(self, "loss_recovery_min_edge_addition", 0)))
                            if side == "buy"
                            else Decimal("0")
                        )
                    ),
                    now_ts=now_ts,
                    sell_pause_until=sell_pause_until,
                    is_dry_run_mode=self._is_dry_run_mode(),
                    sellable_qty=sellable_qty,
                    maker_exchange_min_shares=self.maker_exchange_min_shares,
                    avg_entry=avg_entry,
                    emergency_window=self._is_emergency_exit_window(time_left_sec_global),
                    high_cost_exit_cooldown_enabled=self.maker_high_cost_exit_cooldown_enabled,
                    high_cost_exit_cooldown_sec=float(self.maker_high_cost_exit_cooldown_sec),
                    high_cost_exit_cooldown_until=float(self.high_cost_exit_cooldown_until_by_inst.get(inst_key, 0.0)),
                    maker_sell_cost_protect_enabled=self.maker_sell_cost_protect_enabled,
                    maker_sell_cost_protect_fee_buffer_ps=self.maker_sell_cost_protect_fee_buffer_ps,
                    maker_sell_min_profit_floor_ps=self.maker_sell_min_profit_floor_ps,
                    thesis_weakened=_thesis_weakened,
                    offside_confirmed=_offside_confirmed,
                    confirmed_adverse_exit_active=bool(
                        locked_side_runtime.invalidation_confirmed or _offside_confirmed
                    ),
                    spot_still_supports_position=spot_still_supports_position,
                    stop_loss_pending_active=stop_loss_pending_active,
                    stop_loss_regime_armed=_stop_loss_regime_armed,
                    decision_phase=decision_state.phase.value if decision_state is not None else "",
                    decision_regime=decision_state.regime.value if decision_state is not None else "",
                    decision_pressure=float(decision_state.pressure) if decision_state is not None else None,
                    hold_sec=hold_sec,
                    loss_sell_min_hold_sec=self.maker_loss_sell_min_hold_sec,
                    time_left_sec=time_left_sec_global,
                    entry_mode=buy_entry_eval.entry_mode,
                    entry_size_multiplier=(
                        buy_entry_eval.size_multiplier
                        * Decimal(str(getattr(self, "loss_recovery_size_multiplier", 1)))
                        if side == "buy"
                        else buy_entry_eval.size_multiplier
                    ),
                    entry_quality=buy_entry_eval.payload,
                    entry_is_flat=(
                        current_inst_inventory_qty <= Decimal("0")
                        and other_held_inventory_qty <= Decimal("0")
                    ),
                    entry_signal_confirmed=(
                        bool(self.active_side_locked)
                        and self._latest_observation_supports_locked_side(
                            self.active_side,
                            self.side_decision_score,
                        )
                        and not bool(locked_side_runtime.entry_blocked)
                    ),
                )
                if buy_entry_eval.shadow_only:
                    desired_entry["fair_edge_bucket_shadow"] = buy_entry_eval.fair_edge_bucket
                desired_entry = apply_entry_quality_quote_placement(
                    desired_entry=desired_entry,
                    side=side,
                    quote=quote_ctx.quote,
                    tick=quote_ctx.tick,
                )
                desired_entry = attach_desired_entry_runtime_metadata(
                    desired_entry=desired_entry,
                    dynamic_fee_rate=quote_ctx.dynamic_fee_rate,
                    min_expected_net_usdc=min_expected_net_usdc,
                    quote=quote_ctx.quote,
                    now_ts=now_ts,
                )
                desired_entry = apply_confirmed_inventory_sell_guard(
                    desired_entry=desired_entry,
                    side=side,
                    confirmed_inventory_qty=confirmed_inventory_qty,
                    other_held_inventory_qty=other_held_inventory_qty,
                )
                desired_entry = preserve_profitable_existing_sell_order(
                    desired_entry=desired_entry,
                    side=side,
                    existing_state=self.active_maker_orders.get(order_key),
                    avg_entry=avg_entry,
                    maker_sell_cost_protect_fee_buffer_ps=self.maker_sell_cost_protect_fee_buffer_ps,
                    maker_sell_min_profit_floor_ps=self.maker_sell_min_profit_floor_ps,
                )
                tail_protect_tp_active = False
                tail_protect_tp_qty = Decimal("0")
                if (
                    side == "sell"
                    and getattr(self, "hold_to_redeem_enabled", False)
                    and getattr(self, "tail_protect_tp_enabled", False)
                    and current_inst_inventory_qty > 0
                    and avg_entry >= getattr(self, "tail_protect_tp_min_entry_price", Decimal("1"))
                ):
                    tp_fraction = max(
                        Decimal("0"),
                        min(Decimal("1"), Decimal(str(getattr(self, "tail_protect_tp_fraction", Decimal("0"))))),
                    )
                    tail_protect_tp_qty = current_inst_inventory_qty * tp_fraction
                    if tail_protect_tp_qty + Decimal("0.000001") >= self.maker_exchange_min_shares:
                        tail_protect_tp_active = True
                        desired_entry["should_quote"] = True
                        desired_entry["price"] = self._align_price_to_tick(
                            Decimal(str(getattr(self, "tail_protect_tp_price", Decimal("0.95")))),
                            side,
                            quote_ctx.instrument,
                        )
                        desired_entry["diag_reason"] = (
                            f"tail_protect_tp price={float(desired_entry['price']):.4f} "
                            f"qty={float(tail_protect_tp_qty):.6f} "
                            f"entry={float(avg_entry):.4f}"
                        )
                        desired_entry["loss_sell_reason"] = (
                            f"tail_protect_tp:{float(tail_protect_tp_qty):.6f}"
                        )
                        desired_entry["target_qty_override"] = tail_protect_tp_qty
                        desired_entry["tail_protect_tp"] = True
                        desired_entry["tail_protect_tp_price"] = desired_entry["price"]
                if (
                    side == "sell"
                    and getattr(self, "hold_to_redeem_enabled", False)
                    and current_inst_inventory_qty > 0
                    and not tail_protect_tp_active
                ):
                    desired_entry["should_quote"] = False
                    desired_entry["diag_reason"] = "hold_to_redeem_enabled"
                unified_sell_exit_decision = None
                sell_intent = "NONE"
                quote_intent_state = QuoteIntentState(quote_mode=QuoteMode.OBSERVE)
                hold_to_redeem_sell_block = bool(
                    side == "sell"
                    and getattr(self, "hold_to_redeem_enabled", False)
                    and current_inst_inventory_qty > 0
                    and not tail_protect_tp_active
                )
                peak_bid = (
                    self.maker_profit_run_peak_bid_by_inst.get(inst_key, quote_ctx.quote[0])
                    if side == "sell" and quote_ctx.quote is not None
                    else Decimal("0")
                )
                peak_fair = (
                    self.maker_profit_run_peak_fair_by_inst.get(
                        inst_key,
                        quote_ctx.fair or (quote_ctx.quote[0] if quote_ctx.quote is not None else Decimal("0")),
                    )
                    if side == "sell"
                    else Decimal("0")
                )
                if (
                    side == "sell"
                    and desired_entry.get("should_quote", False)
                    and quote_ctx.quote is not None
                ):
                    de_risk_diag_context = bool(
                        _thesis_weakened
                        or _offside_confirmed
                        or (
                            decision_state is not None
                            and decision_state.phase == DecisionPhase.EXIT
                        )
                    )
                    spread = max(Decimal("0"), quote_ctx.quote[1] - quote_ctx.quote[0])
                    mid = (
                        (quote_ctx.quote[0] + quote_ctx.quote[1]) / Decimal("2")
                        if (quote_ctx.quote[0] + quote_ctx.quote[1]) > 0
                        else Decimal("0")
                    )
                    spread_pct = (spread / mid) if mid > 0 else Decimal("0")
                    held_side = self._side_for_instrument_id(inst_id).value
                    entry_fee_remaining = Decimal(str(inv_state.get("entry_fee_remaining", "0"))) if inv_state is not None else Decimal("0")
                    unified_sell_exit_decision = self.exit_policy_engine.evaluate(
                        MarketSnapshot(
                            instrument_id=inst_key,
                            phase=self.market_phase,
                            time_left_sec=time_left_sec_global,
                            best_bid=quote_ctx.quote[0],
                            best_ask=quote_ctx.quote[1],
                            fee_rate=quote_ctx.fee_rate_val,
                            spread=spread,
                            spread_pct=spread_pct,
                            slippage_buffer_pct=Decimal("0"),
                            exit_stage=self.exit_policy.stage(time_left_sec_global),
                            in_reduce_only_tail=bool(reduce_only_reason),
                            stop_loss_disabled_in_tail=False,
                            fair=quote_ctx.fair,
                            fair_edge_ps=(
                                max(Decimal("0"), quote_ctx.fair - quote_ctx.quote[0])
                                if quote_ctx.fair is not None and quote_ctx.fair > 0
                                else None
                            ),
                            spot_minus_strike_bps=self._spot_minus_strike_bps(),
                        ),
                        PositionState(
                            instrument_id=inst_key,
                            qty=current_inst_inventory_qty,
                            sellable_qty=sellable_qty,
                            avg_entry_price=avg_entry,
                            entry_fee_remaining=entry_fee_remaining,
                            hold_sec=hold_sec,
                            stop_loss_confirm_hits=0,
                            held_side=held_side,
                            peak_bid=peak_bid,
                            peak_fair=peak_fair,
                        ),
                        SignalDecision(
                            active_side=self.active_side.value,
                            score=self.side_decision_score,
                            locked=self.active_side_locked,
                            reason=self.side_decision_reason,
                            matches_position=(self._instrument_for_side(self.active_side) == inst_id),
                        ),
                        external_thesis_weakened=_thesis_weakened,
                        external_offside_confirmed=_offside_confirmed,
                        stop_loss_pending_active=stop_loss_pending_active,
                        locked_side_invalidated=locked_side_runtime.invalidation_confirmed,
                        confirmed_adverse_exit_active=bool(_thesis_weakened or _offside_confirmed),
                    )
                    if unified_sell_exit_decision.decision_type == ExitDecisionType.HOLD_IN_BAND:
                        if tail_inventory_exit_context:
                            desired_entry["should_quote"] = True
                            desired_entry["diag_reason"] = "tail_inventory_exit"
                        else:
                            # HOLD_IN_BAND is now diagnostics-only for maker recycle flow.
                            # Do not let legacy hold-band / recycle-hold reasons suppress
                            # passive sell quotes after we have already determined a valid
                            # same-side inventory exit price.
                            if desired_entry.get("should_quote", False):
                                desired_entry["diag_reason"] = unified_sell_exit_decision.reason
                            desired_entry["exit_policy_hold_reason"] = unified_sell_exit_decision.reason
                recycle_profit_candidate = False
                adverse_exit_context = False
                was_econ_gated = False
                if side == "sell" and quote_ctx.quote is not None and avg_entry > 0:
                    adverse_exit_context = bool(
                        _thesis_weakened
                        or _offside_confirmed
                        or locked_side_runtime.invalidation_confirmed
                        or stop_loss_pending_active
                        or (
                            decision_state is not None
                            and decision_state.phase == DecisionPhase.EXIT
                        )
                    )
                    limit_price_now = Decimal(str(desired_entry.get("price", "0") or "0"))
                    min_profit_sell = (
                        avg_entry
                        + self.maker_sell_cost_protect_fee_buffer_ps
                        + self.maker_sell_min_profit_floor_ps
                    )
                    was_econ_gated = str(desired_entry.get("diag_reason", "")).startswith("econ_gate")
                    is_hold_in_band = (unified_sell_exit_decision is not None and unified_sell_exit_decision.decision_type == ExitDecisionType.HOLD_IN_BAND)
                    if (
                        (desired_entry.get("should_quote", False) or was_econ_gated)
                        and not adverse_exit_context
                        and not tail_inventory_exit_context
                        and not str(desired_entry.get("loss_sell_reason", "") or "")
                        and limit_price_now >= min_profit_sell
                        and not is_hold_in_band
                    ):
                        recycle_profit_candidate = True
                        if was_econ_gated or not str(desired_entry.get("diag_reason", "") or "").startswith("sell_pause"):
                            desired_entry["diag_reason"] = (
                                f"recycle_profit_candidate sell={float(limit_price_now):.4f} "
                                f">= min={float(min_profit_sell):.4f}"
                                + (f" (bypassed {desired_entry.get('diag_reason')})" if was_econ_gated else "")
                            )
                if side == "sell":
                    quote_intent_state = resolve_quote_intent_state(
                        side=side,
                        desired_should_quote=bool(desired_entry.get("should_quote", False)),
                        tail_inventory_exit_context=tail_inventory_exit_context,
                        adverse_exit_context=adverse_exit_context,
                        stop_loss_pending_active=stop_loss_pending_active,
                        recycle_sell_ready=False,
                        recycle_profit_candidate=recycle_profit_candidate,
                        active_side_locked=bool(self.active_side_locked),
                        active_side_value=self.active_side.value,
                        inst_id=inst_id,
                        active_instrument_id=self._instrument_for_side(self.active_side),
                        locked_side_entry_blocked=locked_side_runtime.entry_blocked,
                    )
                    sell_intent = quote_intent_state.sell_intent
                elif side == "buy":
                    quote_intent_state = resolve_quote_intent_state(
                        side=side,
                        desired_should_quote=bool(desired_entry.get("should_quote", False)),
                        tail_inventory_exit_context=False,
                        adverse_exit_context=False,
                        stop_loss_pending_active=False,
                        recycle_sell_ready=False,
                        recycle_profit_candidate=False,
                        active_side_locked=bool(self.active_side_locked),
                        active_side_value=self.active_side.value,
                        inst_id=inst_id,
                        active_instrument_id=self._instrument_for_side(self.active_side),
                        locked_side_entry_blocked=locked_side_runtime.entry_blocked,
                    )

                desired_entry["quote_mode"] = quote_intent_state.quote_mode.value
                desired_entry["hard_exit_allowed"] = quote_intent_state.hard_exit_allowed
                if side == "sell" and not quote_intent_state.hard_exit_allowed:
                    desired_entry["loss_sell_reason"] = ""
                if hold_to_redeem_sell_block:
                    sell_intent = "NONE"
                    quote_intent_state = QuoteIntentState(quote_mode=QuoteMode.OBSERVE)
                    desired_entry["quote_mode"] = quote_intent_state.quote_mode.value
                    desired_entry["hard_exit_allowed"] = False
                    desired_entry["should_quote"] = False
                    desired_entry["loss_sell_reason"] = ""
                    desired_entry["diag_reason"] = "hold_to_redeem_enabled"
                elif side == "sell" and tail_protect_tp_active:
                    sell_intent = "TAIL_PROTECT_TP"
                    desired_entry["quote_mode"] = QuoteMode.RECYCLE_LOCKED_SIDE.value
                    desired_entry["hard_exit_allowed"] = False

                if side == "sell" and was_econ_gated and not hold_to_redeem_sell_block:
                    # Ungate Maker passive sell boundary check if only blocked by econ_gate,
                    # because closing inventory shouldn't require the strict entry edge minimum.
                    desired_entry["should_quote"] = True

                desired_entry = maybe_apply_trapped_inventory_recovery(
                    desired_entry=desired_entry,
                    side=side,
                    trapped_inventory_recovery_enabled=self.trapped_inventory_recovery_enabled,
                    current_inst_inventory_qty=current_inst_inventory_qty,
                    trapped_inventory_recovery_min_qty=self.trapped_inventory_recovery_min_qty,
                    maker_exchange_min_shares=self.maker_exchange_min_shares,
                    active_side_locked=bool(self.active_side_locked),
                    inst_id=inst_id,
                    active_instrument_id=self._instrument_for_side(self.active_side),
                    latest_observation_supports_locked_side=self._latest_observation_supports_locked_side(
                        self.active_side,
                        self.side_decision_score,
                    ),
                    robust_net=desired_entry.get("robust_net"),
                    max_robust_net_deficit_usdc=self.trapped_inventory_recovery_max_robust_net_deficit_usdc,
                    time_left_sec=time_left_sec_global,
                )
                if (
                    side == "sell"
                    and sell_intent == "RECYCLE_PROFIT"
                    and quote_ctx.quote is not None
                    and not hold_to_redeem_sell_block
                ):
                    desired_entry["should_quote"] = True
                    desired_entry = apply_locked_side_recycle_sell_pricing(
                        desired_entry=desired_entry,
                        side=side,
                        avg_entry=avg_entry,
                        fair=quote_ctx.fair,
                        best_bid=quote_ctx.quote[0],
                        best_ask=quote_ctx.quote[1],
                        tick=quote_ctx.tick,
                        maker_sell_cost_protect_fee_buffer_ps=self.maker_sell_cost_protect_fee_buffer_ps,
                        maker_sell_min_profit_floor_ps=self.maker_sell_min_profit_floor_ps,
                        recycle_sell_discount_ps=self.maker_recycle_sell_discount_ps,
                    )
                if side == "sell" and decision_state is not None:
                    desired_entry["decision_pressure"] = decision_state.pressure
                    desired_entry["decision_phase"] = decision_state.phase.value
                    desired_entry["decision_regime"] = decision_state.regime.value
                    desired_entry["legacy_pending"] = decision_state.metadata.get("would_pending_confirmation")
                    desired_entry["legacy_thesis_hold"] = decision_state.metadata.get("would_hold_thesis_not_opposite")
                if (
                    side == "sell"
                    and stop_loss_pending_active
                    and quote_ctx.quote is not None
                    and current_inst_inventory_qty > 0
                    and not hold_to_redeem_sell_block
                ):
                    desired_entry["should_quote"] = True
                    desired_entry["diag_reason"] = (
                        desired_entry.get("diag_reason")
                        or "stop_loss_execution_priority"
                    )
                    desired_entry["loss_sell_reason"] = (
                        str(desired_entry.get("loss_sell_reason", "") or "")
                        or "stop_loss_execution_priority"
                    )
                if (
                    side == "sell"
                    and sell_intent == "FORCED_EXIT"
                    and quote_ctx.quote is not None
                    and current_inst_inventory_qty > 0
                    and not hold_to_redeem_sell_block
                ):
                    desired_entry["should_quote"] = True
                    desired_entry["diag_reason"] = (
                        desired_entry.get("diag_reason")
                        or "forced_exit_execution_priority"
                    )
                if (
                    side == "sell"
                    and desired_entry.get("should_quote", False)
                    and quote_ctx.quote is not None
                    and sell_intent == "FORCED_EXIT"
                    and not hold_to_redeem_sell_block
                ):
                    desired_entry = apply_forced_exit_sell_pricing(
                        desired_entry=desired_entry,
                        side=side,
                        avg_entry=avg_entry,
                        fair=quote_ctx.fair,
                        best_bid=quote_ctx.quote[0],
                        best_ask=quote_ctx.quote[1],
                        tick=quote_ctx.tick,
                        maker_sell_cost_protect_fee_buffer_ps=self.maker_sell_cost_protect_fee_buffer_ps,
                        maker_sell_min_profit_floor_ps=self.maker_sell_min_profit_floor_ps,
                        exit_decision_reason=(
                            "confirmed_locked_side_invalidation"
                            if locked_side_runtime.invalidation_confirmed
                            else (
                                f"state_machine_{decision_state.phase.value.lower()}"
                                if decision_state is not None
                                else "forced_exit"
                            )
                        ),
                        allow_loss_exit_below_cost_floor=bool(
                            locked_side_runtime.invalidation_confirmed or _offside_confirmed
                        ),
                    )
                elif (
                    side == "sell"
                    and sell_intent == "TAIL_EXIT"
                    and desired_entry.get("should_quote", False)
                    and quote_ctx.quote is not None
                    and not hold_to_redeem_sell_block
                ):
                    desired_entry = apply_forced_exit_sell_pricing(
                        desired_entry=desired_entry,
                        side=side,
                        avg_entry=avg_entry,
                        fair=quote_ctx.fair,
                        best_bid=quote_ctx.quote[0],
                        best_ask=quote_ctx.quote[1],
                        tick=quote_ctx.tick,
                        maker_sell_cost_protect_fee_buffer_ps=self.maker_sell_cost_protect_fee_buffer_ps,
                        maker_sell_min_profit_floor_ps=self.maker_sell_min_profit_floor_ps,
                        exit_decision_reason="tail_inventory_exit",
                        allow_loss_exit_below_cost_floor=False,
                    )
                if (
                    side == "sell"
                    and quote_intent_state.hard_exit_allowed
                    and desired_entry.get("should_quote", False)
                    and not hold_to_redeem_sell_block
                ):
                    desired_entry = preserve_recent_loss_sell_order(
                        desired_entry=desired_entry,
                        side=side,
                        existing_state=self.active_maker_orders.get(order_key),
                        now_ts=now_ts,
                        loss_sell_reprice_min_interval_sec=self.maker_loss_sell_reprice_min_interval_sec,
                    )
                if side == "buy" and quote_ctx.quote is not None:
                    locked_for_sec = (
                        max(0.0, now_ts - float(getattr(self, "active_side_locked_since_ts", 0.0)))
                        if getattr(self, "active_side_locked_since_ts", 0.0) > 0
                        else 0.0
                    )
                    desired_entry = maybe_apply_continuation_entry(
                        desired_entry=desired_entry,
                        side=side,
                        active_side_locked=bool(self.active_side_locked),
                        active_side_value=self.active_side.value,
                        inst_id=inst_id,
                        active_instrument_id=self._instrument_for_side(self.active_side),
                        side_score=self.side_decision_score,
                        locked_for_sec=locked_for_sec,
                        time_left_sec=time_left_sec_global,
                        current_inventory_qty=current_inst_inventory_qty,
                        market_buy_count=market_buy_count,
                        best_bid=quote_ctx.quote[0],
                        fair=quote_ctx.fair,
                        continuation_enabled=self.continuation_entry_enabled,
                        continuation_size_multiplier=self.continuation_entry_size_multiplier,
                    )
                    if live_shadow_payload is None:
                        live_shadow_payload = self._build_live_signal_compare_payload(now_ts)
                    desired_entry = apply_shadow_entry_veto(
                        desired_entry=desired_entry,
                        side=side,
                        entry_mode=str(desired_entry.get("entry_mode", buy_entry_eval.entry_mode or "value")).lower(),
                        inst_id=inst_id,
                        up_instrument_id=self.current_up_instrument_id,
                        down_instrument_id=self.current_down_instrument_id,
                        shadow_payload=live_shadow_payload,
                    )
                    entry_confirmation_engine = getattr(self, "entry_confirmation_engine", None)
                    entry_confirmation_config = (
                        getattr(entry_confirmation_engine, "config", None)
                        if entry_confirmation_engine is not None
                        else None
                    )
                    if (
                        entry_confirmation_engine is not None
                        and entry_confirmation_config is not None
                        and (
                            bool(getattr(entry_confirmation_config, "enabled", False))
                            or bool(getattr(entry_confirmation_config, "shadow_enabled", True))
                        )
                    ):
                        ref_spot, ref_spot_source, ref_spot_age = self._capture_market_open_spot_detail(now_ts=now_ts)
                        binance_ts = float(getattr(self, "_binance_ws_price_ts", 0.0) or 0.0)
                        binance_age = max(0.0, now_ts - binance_ts) if binance_ts > 0 else None
                        entry_confirmation_signal = entry_confirmation_engine.evaluate(
                            active_side=self.active_side.value,
                            p_fair=desired_entry.get("p_fair"),
                            fair=quote_ctx.fair,
                            best_bid=quote_ctx.quote[0],
                            best_ask=quote_ctx.quote[1],
                            ref_spot=ref_spot,
                            ref_spot_source=ref_spot_source,
                            ref_spot_age_sec=ref_spot_age,
                            strike=self.market_strike_cache_by_slug.get(str(self.current_market_slug or "")),
                            binance_spot=getattr(self, "_binance_ws_price", None),
                            binance_age_sec=binance_age,
                        )
                        desired_entry = apply_entry_confirmation_adjustment(
                            desired_entry=desired_entry,
                            side=side,
                            signal=entry_confirmation_signal,
                            config=entry_confirmation_config,
                        )
                        if self.trade_db:
                            entry_confirmation_payload = entry_confirmation_signal.as_payload()
                            entry_confirmation_payload.update(
                                {
                                    "slug": str(self.current_market_slug or ""),
                                    "instrument_id": str(inst_id),
                                    "should_quote": bool(desired_entry.get("should_quote", False)),
                                    "entry_mode": str(desired_entry.get("entry_mode", "") or ""),
                                    "price": (
                                        float(desired_entry.get("price"))
                                        if desired_entry.get("price") is not None
                                        else None
                                    ),
                                    "robust_net_usdc": (
                                        float(desired_entry.get("robust_net"))
                                        if desired_entry.get("robust_net") is not None
                                        else None
                                    ),
                                    "side_score": float(self.side_decision_score),
                                    "time_left_sec": (
                                        float(time_left_sec_global)
                                        if time_left_sec_global is not None
                                        else None
                                    ),
                                }
                            )
                            self._db_strategy_event("ENTRY_CONFIRMATION_OBSERVATION", entry_confirmation_payload)
                    smart_money_tracker = getattr(self, "smart_money_tracker", None)
                    smart_money_config = getattr(self, "smart_money_config", None)
                    if (
                        smart_money_tracker is not None
                        and smart_money_config is not None
                        and (
                            bool(getattr(smart_money_config, "enabled", False))
                            or bool(getattr(smart_money_config, "shadow_enabled", True))
                        )
                    ):
                        condition_id = (
                            self._extract_condition_id_from_instrument(inst_id)
                            or self._extract_condition_id_from_instrument(self.current_up_instrument_id)
                            or self._extract_condition_id_from_instrument(self.current_down_instrument_id)
                        )
                        if condition_id:
                            smart_money_tracker.watch_market(
                                condition_id=condition_id,
                                slug=str(self.current_market_slug or ""),
                                up_token_id=extract_token_id_from_instrument_id(self.current_up_instrument_id),
                                down_token_id=extract_token_id_from_instrument_id(self.current_down_instrument_id),
                            )
                        smart_money_signal = smart_money_tracker.evaluate(
                            condition_id=condition_id,
                            active_side=self.active_side.value,
                            market_end_ts=(
                                float(self.current_market_end_timestamp)
                                if self.current_market_end_timestamp is not None
                                else None
                            ),
                            now_ts=now_ts,
                        )
                        desired_entry = apply_smart_money_adjustment(
                            desired_entry=desired_entry,
                            side=side,
                            signal=smart_money_signal,
                            config=smart_money_config,
                        )
                        if self.trade_db:
                            smart_money_payload = smart_money_signal.as_payload()
                            smart_money_payload.update(
                                {
                                    "slug": str(self.current_market_slug or ""),
                                    "condition_id": condition_id,
                                    "instrument_id": str(inst_id),
                                    "should_quote": bool(desired_entry.get("should_quote", False)),
                                    "entry_mode": str(desired_entry.get("entry_mode", "") or ""),
                                    "price": (
                                        float(desired_entry.get("price"))
                                        if desired_entry.get("price") is not None
                                        else None
                                    ),
                                    "robust_net_usdc": (
                                        float(desired_entry.get("robust_net"))
                                        if desired_entry.get("robust_net") is not None
                                        else None
                                    ),
                                    "side_score": float(self.side_decision_score),
                                    "time_left_sec": (
                                        float(time_left_sec_global)
                                        if time_left_sec_global is not None
                                        else None
                                    ),
                                }
                            )
                            self._db_strategy_event("SMART_MONEY_OBSERVATION", smart_money_payload)
                    desired_entry = apply_weak_pfair_size_adjustment(
                        desired_entry=desired_entry,
                        side=side,
                        enabled=bool(getattr(self, "maker_weak_pfair_size_adjust_enabled", True)),
                        lower=Decimal(str(getattr(self, "maker_weak_pfair_size_adjust_lower", Decimal("0.47")))),
                        upper=Decimal(str(getattr(self, "maker_weak_pfair_size_adjust_upper", Decimal("0.53")))),
                        multiplier=Decimal(str(getattr(self, "maker_weak_pfair_size_adjust_multiplier", Decimal("0.5")))),
                    )
                    desired_entry = apply_high_entry_price_size_adjustment(
                        desired_entry=desired_entry,
                        side=side,
                        enabled=bool(getattr(self, "maker_high_entry_price_size_adjust_enabled", False)),
                        threshold=Decimal(
                            str(
                                getattr(
                                    self,
                                    "maker_high_entry_price_size_adjust_threshold",
                                    Decimal("0.70"),
                                )
                            )
                        ),
                        multiplier=Decimal(
                            str(
                                getattr(
                                    self,
                                    "maker_high_entry_price_size_adjust_multiplier",
                                    Decimal("0.5"),
                                )
                            )
                        ),
                    )
                    desired_entry = apply_fractional_kelly_sizing(
                        desired_entry=desired_entry,
                        side=side,
                        enabled=bool(getattr(self, "kelly_sizing_enabled", False)),
                        available_collateral_usdc=getattr(self, "_cached_usdc_balance", None),
                        fraction=Decimal(str(getattr(self, "kelly_sizing_fraction", Decimal("0.25")))),
                        max_collateral_fraction=Decimal(str(getattr(self, "kelly_sizing_max_collateral_fraction", Decimal("0.10")))),
                        base_quantity=self._compute_maker_order_qty(
                            Decimal(str(desired_entry.get("price", "0"))),
                            int(getattr(quote_ctx.instrument, "size_precision", 6) or 6),
                        ),
                    )
                    if side == "buy" and desired_entry.get("should_quote", False):
                        requested_quantity = desired_entry.get("target_qty_override")
                        if requested_quantity is None:
                            base_quantity = self._compute_maker_order_qty(
                                Decimal(str(desired_entry.get("price", "0") or "0")),
                                int(getattr(quote_ctx.instrument, "size_precision", 6) or 6),
                            )
                            requested_quantity = (
                                base_quantity
                                * Decimal(str(desired_entry.get("size_multiplier", "1") or "1"))
                            )
                        else:
                            requested_quantity = Decimal(str(requested_quantity)) * Decimal(
                                str(desired_entry.get("size_multiplier", "1") or "1")
                            )
                        desired_entry = synchronize_desired_buy_economics_to_quantity(
                            desired_entry=desired_entry,
                            requested_quantity=requested_quantity,
                        )
                    self._emit_buy_observe_diagnostic(
                        inst_id=inst_id,
                        desired_entry=desired_entry,
                        quote_intent_state=quote_intent_state,
                        locked_side_runtime=locked_side_runtime,
                        current_inst_inventory_qty=current_inst_inventory_qty,
                        market_buy_count=market_buy_count,
                        time_left_sec=time_left_sec_global,
                    )
                    self._record_entry_decision_trace(
                        now_ts=now_ts,
                        inst_id=inst_id,
                        side=side,
                        should_quote=bool(desired_entry.get("should_quote", False)),
                        reason=str(desired_entry.get("diag_reason", "") or "eligible"),
                        source_event_type="ENTRY_DECISION_PRE_SUBMIT",
                        shadow_only=bool(desired_entry.get("fair_edge_bucket_shadow", "")),
                        entry_mode=str(desired_entry.get("entry_mode", buy_entry_eval.entry_mode) or ""),
                        fair=quote_ctx.fair,
                        entry_price=desired_entry.get("price"),
                        robust_net_usdc=desired_entry.get("robust_net", candidate_robust_net),
                        fee_per_share=desired_entry.get("fee_ps"),
                        planned_quantity=desired_entry.get("planned_quantity"),
                        time_left_sec=time_left_sec_global,
                    )
                desired_quotes[order_key] = desired_entry

        return desired_quotes, diag_context_by_inst

    async def _quote_maker_orders(self, bid_price: Decimal, ask_price: Decimal) -> None:
        """
        Place symmetric maker quotes if expected net economics is positive.
        """
        self._telegram_cycle_tick()
        if self.dashboard_state is not None and self.dashboard_state.bot_paused:
            now_ts = time.time()
            if now_ts - self._last_dashboard_pause_log_ts >= 30.0:
                logger.info("Telegram pause active; skipping maker quote cycle.")
                self._last_dashboard_pause_log_ts = now_ts
            return
        cycle = await self._prepare_quote_cycle()
        if cycle is None:
            return
        desired_quotes, diag_context_by_inst = await self._evaluate_quote_targets(
            phase=cycle["phase"],
            forced_sell_only=cycle["forced_sell_only"],
            regime_guard_active=cycle["regime_guard_active"],
            now_ts=cycle["now_ts"],
            recent_vol=cycle["recent_vol"],
            target_instruments=cycle["target_instruments"],
            end_ts=cycle["end_ts"],
            time_left_sec_global=cycle["time_left_sec_global"],
        )
        await self._submit_quote_cycle(
            phase=cycle["phase"],
            now_ts=cycle["now_ts"],
            target_instruments=cycle["target_instruments"],
            target_inst_set=cycle["target_inst_set"],
            desired_quotes=desired_quotes,
            diag_context_by_inst=diag_context_by_inst,
        )

    async def _submit_maker_quote(
        self,
        instrument_id: Any,
        side: str,
        limit_price: Decimal,
        econ,
        dynamic_fee_rate: Optional[Decimal] = None,
        directional_snapshot: Optional[Dict[str, Any]] = None,
        target_version: Optional[int] = None,
        loss_sell_reason: str = "",
        target_qty_override: Optional[Decimal] = None,
        fair_edge_bucket_shadow: Optional[str] = None,
    ) -> None:
        if self.dashboard_state is not None and self.dashboard_state.bot_paused:
            return
        submit_maker_quote(
            self,
            instrument_id=instrument_id,
            side=side,
            limit_price=limit_price,
            econ=econ,
            dynamic_fee_rate=dynamic_fee_rate,
            directional_snapshot=directional_snapshot,
            target_version=target_version,
            loss_sell_reason=loss_sell_reason,
            target_qty_override=target_qty_override,
            fair_edge_bucket_shadow=fair_edge_bucket_shadow,
        )
    
    def on_start(self):
        """Called when strategy starts."""
        logger.info("Strategy start sequence initiated.")
        self._log_strategy_config_summary()
        self.last_valid_quote_ts = time.time()
        self.consecutive_invalid_quote_ticks = 0
        
        # Find BTC instrument FIRST and wait for it
        if not self._wait_for_btc_instrument(timeout_sec=60, poll_interval_sec=2):
            raise RuntimeError("Startup check failed: no BTC 15-min instrument loaded")

        # Recover local inventory tracking if the strategy restarted mid-market.
        self._rehydrate_inventory_state_on_startup()
        self._restore_market_risk_guards_from_trade_db_on_startup()
        self._recover_market_strike_from_trade_db_on_startup()
        self._apply_empirical_execution_penalty_calibration()

        log_strategy_run_start(
            trade_db=self.trade_db,
            run_id=self.run_id,
            is_dry_run_mode=self._is_dry_run_mode(),
            test_mode=self.test_mode,
            maker_mode=self.maker_mode,
            instrument_id=self.instrument_id,
            selected_slug=self.selected_slug,
            maker_quote_sides=self.maker_quote_sides,
            maker_quote_size_usdc=self.maker_quote_size_usdc,
        )
        self._db_strategy_event(
            "STRATEGY_START",
            {
                "instrument_id": str(self.instrument_id) if self.instrument_id else None,
                "selected_slug": self.selected_slug,
                "test_mode": self.test_mode,
                "bi_side_enabled": self.bi_side_enabled,
                "active_side": self.active_side.value,
                "git_revision": self.runtime_git_revision,
                "source_fingerprint": self.runtime_source_fingerprint,
                "maker_fixed_shares": float(self.maker_fixed_shares),
                "maker_max_order_usdc": float(self.maker_max_order_usdc),
                "directional_entry_min_score_abs": float(self.directional_entry_min_score_abs),
                "maker_urgent_exit_enabled": self.maker_urgent_exit_enabled,
                "taker_exit_eval_interval_sec": float(self.taker_exit_eval_interval_sec),
                "taker_exit_stop_loss_confirmations": int(self.taker_exit_stop_loss_confirmations),
                "taker_exit_stop_loss_usdc": float(self.taker_exit_stop_loss_usdc),
                "taker_exit_wait_for_sell_quote_sec": float(self.taker_exit_wait_for_sell_quote_sec),
                "market_stop_loss_max_per_market": int(self.market_stop_loss_max_per_market),
                "market_max_buy_events_per_market": int(self.market_max_buy_events_per_market),
                "stop_loss_reentry_cooldown_sec": int(self.stop_loss_reentry_cooldown_sec),
                "exit_stop_loss_requires_thesis_weakening": self.exit_stop_loss_requires_thesis_weakening,
                "exit_stop_loss_hold_on_none_signal": self.exit_stop_loss_hold_on_none_signal,
                "exit_stop_loss_thesis_min_score_abs": float(self.exit_stop_loss_thesis_min_score_abs),
                "exit_conviction_band_min_price": float(self.exit_conviction_band_min_price),
                "exit_hold_band_min_price": float(self.exit_hold_band_min_price),
                "exit_conviction_band_min_score_abs": float(self.exit_conviction_band_min_score_abs),
                "exit_hold_band_min_score_abs": float(self.exit_hold_band_min_score_abs),
                "exit_conviction_stop_loss_multiplier": float(self.exit_conviction_stop_loss_multiplier),
                "exit_conviction_extra_confirmations": int(self.exit_conviction_extra_confirmations),
                "exit_hold_band_requires_locked": self.exit_hold_band_requires_locked,
            },
        )
        self._bootstrap_regime_guard_window_from_db()

        # Log which side-decision engine is active
        if self.bi_side_enabled:
            logger.info(
                "Side decision engine: SignalEngine (probabilistic) | "
                f"min_confidence={self.side_signal_min_confidence} "
                f"threshold_up={self.side_signal_threshold_up} "
                f"threshold_down={self.side_signal_threshold_down} | "
                f"BTC EMA {self.side_signal_btc_ema_fast_sec}s/{self.side_signal_btc_ema_slow_sec}s | "
                f"Mid EMA {self.side_signal_mid_ema_fast_sec}s/{self.side_signal_mid_ema_slow_sec}s"
            )
        
        # Ensure we have sufficient history.
        if len(self.price_history) < 20:
            self._generate_synthetic_history(target_count=20, existing_count=len(self.price_history))
        
        # Try to get real price if instrument exists and we have quotes
        if self.instrument_id:
            try:
                # Get the most recent quote from cache
                quote = self.cache.quote_tick(self.instrument_id)
                if quote and quote.bid_price and quote.ask_price:
                    current_price = (quote.bid_price + quote.ask_price) / 2
                    # Replace last synthetic price with real one
                    if self.price_history:
                        self.price_history[-1] = current_price
                    else:
                        self.price_history.append(current_price)
                    self.real_price_history.append(current_price)
                    if len(self.real_price_history) > self.max_real_history:
                        self.real_price_history.pop(0)
            except Exception as e:
                logger.debug(f"Could not get real price: {e}")
                logger.debug("Using synthetic prices until real quotes arrive")
        
        # Start market lifecycle timer (replaces fixed 12-min reload)
        self._lifecycle_stop_event.clear()
        self._lifecycle_thread = start_background_thread(self._start_market_lifecycle_timer, "market-lifecycle")
        # Initialize live Prometheus trading metrics
        self._init_live_prom_metrics()
        # Start real-time BTC price streams
        self._start_polymarket_chainlink_ws()
        self._start_binance_ws()
        # Also start the legacy reload timer as a fallback
        self._reload_stop_event.clear()
        self._reload_thread = start_background_thread(self._start_reload_timer, "reload-timer")
        self._quote_watchdog_stop_event.clear()
        self._quote_watchdog_thread = start_background_thread(self._start_quote_watchdog_timer, "quote-watchdog")
        # Initialize phase based on current market
        self._update_market_phase()
        if self.auto_redeem_enabled:
            self._redeem_stop_event.clear()
            self._redeem_thread = start_background_thread(self._start_auto_redeem_timer, "auto-redeem")
            self._schedule_auto_redeem(reason="startup")
        self._balance_stop_event.clear()
        self._balance_thread = start_background_thread(self._start_balance_refresh_timer, "balance-refresh")
        try:
            self._refresh_balance_cache_sync()
        except Exception as e:
            logger.debug(f"Initial balance refresh failed: {e}")
        
        if self.terminal_dashboard:
            self._terminal_dashboard_stop_event.clear()
            self._terminal_dashboard_thread = start_background_thread(
                self._start_terminal_dashboard_sync,
                "terminal-dashboard",
            )
            self._update_terminal_dashboard_snapshot()

        logger.info(f"Strategy active. price_history_points={len(self.price_history)}")
        if len(self.price_history) >= 20:
            logger.info("Ready to trade at next interval.")
        else:
            logger.warning(f"Need more history ({len(self.price_history)}/20)")
                
    def _preload_history_sync(self):
        """Synchronous wrapper for history preload."""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._preload_price_history())
        finally:
            loop.close()
    
    async def _preload_price_history(self):
        """Pre-load price history from cache or generate synthetic data for testing."""
        logger.info("Preloading price history...")
        
        # Get current instrument
        if not self.instrument_id:
            logger.warning("No instrument ID, skipping preload")
            return
        
        # Try to get current price from cache first
        quote = self.cache.quote_tick(self.instrument_id)
        if quote:
            current_price = (quote.bid_price + quote.ask_price) / 2
            self.price_history.append(current_price)
        
        self.price_history = dedupe_price_history(self.price_history)
        
        # If still not enough, generate synthetic data
        if len(self.price_history) < 20:
            logger.warning(f"Only {len(self.price_history)} historical quotes found, generating synthetic data to fill")
            self._generate_synthetic_history(existing_count=len(self.price_history))
        
        logger.info(f"Price history preload complete: points={len(self.price_history)}")
        if len(self.price_history) >= 20:
            logger.info("Price history is sufficient.")
        else:
            logger.warning("Still need more history - will collect from live data")
    
    def _generate_synthetic_history(self, target_count: int = 20, existing_count: int = 0):
        """Generate synthetic price history for testing/initialization."""
        needed = extend_synthetic_history(
            price_history=self.price_history,
            target_count=target_count,
            existing_count=existing_count,
        )
        if needed > 0:
            logger.info(f"Synthetic history added: +{needed} (total={len(self.price_history)})")
    
    def _start_reload_timer(self):
        """Start timer to reload instruments every 12 minutes."""
        while not self._reload_stop_event.wait(720):  # 12 minutes
            logger.info("Reloading instruments (timer)...")
            
            try:
                # Request instrument reload from data client
                instruments = self.cache.instruments()
                
                # Re-find BTC instrument (this will select the active one)
                previous_slug = self.current_market_slug
                if not self._find_btc_instrument():
                    logger.warning("Reload completed but no BTC 15-min instrument found")
                elif self.auto_redeem_enabled and self.auto_redeem_on_rollover and previous_slug and self.current_market_slug and previous_slug != self.current_market_slug:
                    self._schedule_auto_redeem(reason=f"market_rollover:{previous_slug}->{self.current_market_slug}")
                
                logger.info(f"Instruments reload complete. cached={len(instruments)}")
            except Exception as e:
                logger.error(f"Failed to reload instruments: {e}")

    def _schedule_auto_redeem(self, reason: str) -> None:
        """
        Run redeem checker script in a detached worker so trading flow is never blocked.
        """
        if not self.auto_redeem_enabled:
            return
        if self._is_dry_run_mode():
            logger.info(f"Auto redeem skipped in dry-run mode: reason={reason}")
            return

        def _runner() -> None:
            if not self._redeem_job_lock.acquire(blocking=False):
                logger.debug(f"Auto redeem skipped (already running): reason={reason}")
                return
            try:
                now_ts = time.time()
                skip_run, elapsed_since_last = should_skip_auto_redeem_run(
                    now_ts=now_ts,
                    auto_redeem_min_gap_sec=float(self.auto_redeem_min_gap_sec),
                    last_redeem_run_ts=float(self._last_redeem_run_ts),
                )
                if skip_run:
                    logger.info(
                        "Auto redeem skipped by min gap: "
                        f"reason={reason} elapsed={elapsed_since_last:.1f}s "
                        f"required={self.auto_redeem_min_gap_sec}s"
                    )
                    return
                self._last_redeem_run_ts = now_ts
                if self.terminal_dashboard:
                    self.terminal_dashboard.increment_redeem()

                def _record_redeem_event(event_type: str, payload: Dict[str, Any]) -> None:
                    self._db_strategy_event(event_type, payload)
                    if event_type == "REDEEM_EXECUTED":
                        self._reconcile_redeem_cycle_pnl(payload)

                run_auto_redeem_script(
                    repo_root=Path(__file__).parent,
                    reason=reason,
                    auto_redeem_slug_filter=self.auto_redeem_slug_filter,
                    auto_redeem_apply=self.auto_redeem_apply,
                    auto_redeem_timeout_sec=int(self.auto_redeem_timeout_sec),
                    logger_info_fn=logger.info,
                    logger_warning_fn=logger.warning,
                    db_strategy_event_fn=_record_redeem_event,
                )
            except Exception as e:
                logger.warning(f"Auto redeem failed (reason={reason}): {e}")
            finally:
                self._redeem_job_lock.release()

        threading.Thread(target=_runner, daemon=True).start()

    def _start_auto_redeem_timer(self) -> None:
        """
        Periodic auto redeem timer (default every 15 minutes).
        Also checks for YES/NO merge opportunities.
        """
        while not self._redeem_stop_event.wait(self.auto_redeem_interval_sec):
            if self._stopping:
                return
            self._schedule_auto_redeem(reason="interval")
            # Check for merge opportunities periodically
            self._try_merge_yes_no_positions()

    def _try_merge_yes_no_positions(self) -> None:
        if self._is_dry_run_mode():
            logger.info("YES/NO merge skipped in dry-run mode")
            return
        try_merge_yes_no_positions(
            strategy=self,
            logger_info_fn=logger.info,
            logger_debug_fn=logger.debug,
            logger_warning_fn=logger.warning,
            adjust_inventory_after_merge_fn=adjust_inventory_after_merge,
        )

    def _execute_merge_on_chain(
        self, pk: str, condition_id: str, amount: int, rpc_url: str, chain_id: int
    ) -> bool:
        from bot.merge_ops import execute_merge_on_chain
        return execute_merge_on_chain(
            pk=pk,
            condition_id=condition_id,
            amount=amount,
            rpc_url=rpc_url,
            chain_id=chain_id,
            logger_info_fn=logger.info,
            logger_warning_fn=logger.warning,
        )

    def _request_quote_stream_node_rollover(self, trigger: str, now_ts: float) -> None:
        """Escalate a failed resubscribe to a clean data-client rebuild by the launcher."""
        if self._stopping or getattr(self, "_quote_stream_rollover_requested", False):
            return
        self._quote_stream_rollover_requested = True
        self._rollover_requested_flag = True
        self._stopping = True
        logger.error(f"Quote stream recovery exhausted; requesting node rollover: trigger={trigger}")
        self._db_strategy_event(
            "QUOTE_WATCHDOG_NODE_ROLLOVER",
            {"trigger": trigger, "instrument": str(self.instrument_id) if self.instrument_id else None, "ts": now_ts},
        )
        try:
            if hasattr(self, "_trader") and hasattr(self._trader, "node"):
                self._trader.node.stop()
        except Exception as exc:
            logger.error(f"Quote stream node rollover stop failed: {exc}")

    def _quote_watchdog_recovery_is_needed(self) -> bool:
        """Only rebuild a quote stream when it can still affect risk or quoting."""
        phase = getattr(self, "market_phase", None)
        phase_value = str(getattr(phase, "value", phase) or "").upper()
        if phase_value == "ACTIVE":
            return True
        try:
            if Decimal(str(getattr(self, "inventory_delta_shares", "0"))) > 0:
                return True
        except (ArithmeticError, TypeError, ValueError):
            return True
        return bool(getattr(self, "active_maker_orders", {}))

    def _trigger_quote_watchdog_reload(self, trigger: str, now_ts: float) -> None:
        """
        Recover quote stream when valid bid/ask updates disappear for too long.
        """
        if not should_attempt_quote_watchdog_recovery(
            now_ts=now_ts,
            last_quote_watchdog_reload_ts=float(self.last_quote_watchdog_reload_ts),
            quote_reload_cooldown_sec=float(self.quote_reload_cooldown_sec),
        ):
            remaining_sec = max(
                0.0,
                float(self.quote_reload_cooldown_sec) - (now_ts - self.last_quote_watchdog_reload_ts),
            )
            logger.debug(
                "Quote watchdog recovery suppressed by reload cooldown: "
                f"trigger={trigger} remaining={remaining_sec:.1f}s"
            )
            return
        trigger_source = str(trigger).split("|", 1)[0]
        trigger_counts = getattr(self, "quote_watchdog_trigger_counts", None)
        if not isinstance(trigger_counts, dict):
            trigger_counts = {}
            self.quote_watchdog_trigger_counts = trigger_counts
        trigger_counts[trigger_source] = int(trigger_counts.get(trigger_source, 0)) + 1
        selected_ok, reload_ts, _stale_for, prev_instrument = handle_quote_watchdog_recovery(
            trigger=trigger,
            now_ts=now_ts,
            last_quote_watchdog_reload_ts=float(self.last_quote_watchdog_reload_ts),
            quote_reload_cooldown_sec=float(self.quote_reload_cooldown_sec),
            instrument_id=self.instrument_id,
            last_valid_quote_ts=float(self.last_valid_quote_ts),
            consecutive_invalid_quote_ticks=int(self.consecutive_invalid_quote_ticks),
            db_strategy_event_fn=self._db_strategy_event,
            cancel_active_maker_orders_fn=self._cancel_active_maker_orders,
            find_btc_instrument_fn=self._find_btc_instrument,
            logger_warning_fn=logger.warning,
            logger_error_fn=logger.error,
            trigger_count=trigger_counts[trigger_source],
        )
        if reload_ts == self.last_quote_watchdog_reload_ts:
            return
        self.last_quote_watchdog_reload_ts = reload_ts
        new_instrument = str(self.instrument_id) if self.instrument_id else None
        if selected_ok and self.instrument_id is not None:
            from bot.market_runtime import refresh_quote_tick_subscriptions

            refresh_quote_tick_subscriptions(self)
            self.quote_recovery_attempts = int(getattr(self, "quote_recovery_attempts", 0)) + 1
            # Start the grace period only after the resubscribe call returns.
            # The call itself can take seconds, especially during rollover.
            self.quote_recovery_started_ts = time.time()
            logger.warning(
                "Quote watchdog resubscribed; awaiting first fresh quote: "
                f"{prev_instrument} -> {new_instrument} grace={self.quote_resubscribe_grace_sec}s"
            )
            self._db_strategy_event(
                "QUOTE_WATCHDOG_RESUBSCRIBED",
                {
                    "instrument_before": prev_instrument,
                    "instrument_after": new_instrument,
                    "grace_sec": self.quote_resubscribe_grace_sec,
                    "trigger": trigger,
                    "trigger_count": trigger_counts[trigger_source],
                },
            )
            self.consecutive_invalid_quote_ticks = 0
            return

        logger.error("Quote watchdog recovery failed: no BTC 15-min instrument selected")
        self._db_strategy_event(
            "QUOTE_WATCHDOG_FAILED",
            {
                "instrument_before": prev_instrument,
                "instrument_after": new_instrument,
            },
        )

    def _maybe_run_quote_watchdog(self, trigger: str) -> None:
        now_ts = time.time()
        should_run, stale_for = should_run_quote_watchdog(
            now_ts=now_ts,
            last_quote_watchdog_check_ts=float(self.last_quote_watchdog_check_ts),
            quote_healthcheck_interval_sec=float(self.quote_healthcheck_interval_sec),
            last_valid_quote_ts=float(self.last_valid_quote_ts),
            quote_stale_sec=float(self.quote_stale_sec),
            consecutive_invalid_quote_ticks=int(self.consecutive_invalid_quote_ticks),
            quote_invalid_tick_reload_threshold=int(self.quote_invalid_tick_reload_threshold),
        )
        if not should_run:
            return
        self.last_quote_watchdog_check_ts = now_ts

        reason = trigger
        stale_hit = self.last_valid_quote_ts > 0 and stale_for >= self.quote_stale_sec
        invalid_hit = self.consecutive_invalid_quote_ticks >= self.quote_invalid_tick_reload_threshold
        if stale_hit:
            reason = f"{reason}|stale_quotes"
        if invalid_hit:
            reason = f"{reason}|invalid_ticks"
        self._trigger_quote_watchdog_reload(reason, now_ts)

    def _start_quote_watchdog_timer(self) -> None:
        """
        Background heartbeat for quote health.
        Needed because DataClient can drop incomplete ticks before strategy receives them.
        """
        while not self._quote_watchdog_stop_event.wait(self.quote_healthcheck_interval_sec):
            if self._stopping:
                return
            now_ts = time.time()
            self._emit_strategy_status(now_ts)
            recovery_started_ts = float(getattr(self, "quote_recovery_started_ts", 0.0))
            pending_instruments = getattr(self, "quote_recovery_pending_instruments", set())
            if pending_instruments and recovery_started_ts > 0:
                if not self._quote_watchdog_recovery_is_needed():
                    pending_instruments.clear()
                    self.quote_recovery_started_ts = 0.0
                    continue
                recovery_age = now_ts - recovery_started_ts
                if recovery_age >= float(self.quote_resubscribe_grace_sec):
                    if int(getattr(self, "quote_recovery_attempts", 0)) >= 1:
                        self._request_quote_stream_node_rollover("quote_resubscribe_timeout", now_ts)
                        return
                    self._trigger_quote_watchdog_reload("quote_subscription_timeout", now_ts)
                continue
            stale_for = (now_ts - self.last_valid_quote_ts) if self.last_valid_quote_ts > 0 else None
            if stale_for is None or stale_for < self.quote_stale_sec:
                continue
            self._trigger_quote_watchdog_reload("timer_stale_quotes", now_ts)

    def _emit_strategy_status(self, now_ts: float) -> None:
        """
        Periodic concise status line to explain why bot is (not) quoting.
        """
        if now_ts - self.last_status_log_ts < self.strategy_status_interval_sec:
            return
        self.last_status_log_ts = now_ts
        self._emit_live_signal_compare_snapshot(now_ts)

        reasons: List[str] = []
        if self._stopping:
            reasons.append("stopping")
        if self.market_phase == MarketPhase.WAITING:
            reasons.append("phase_waiting")
        elif self.market_phase == MarketPhase.SETTLING:
            reasons.append("phase_settling")
        if not self.maker_mode:
            reasons.append("maker_mode_off")
        if self.maker_kill_switch:
            reasons.append("kill_switch_on")
        if now_ts < self.quote_pause_until_ts:
            reasons.append(f"paused_{int(self.quote_pause_until_ts - now_ts)}s")
        if now_ts < self.orderbook_unavailable_until_ts:
            reasons.append(f"orderbook_unavailable_{int(self.orderbook_unavailable_until_ts - now_ts)}s")
        if self.latest_market_bid is None or self.latest_market_ask is None:
            reasons.append("no_valid_quote")
        if self.instrument_id is None:
            reasons.append("no_instrument")
        if now_ts < float(self.regime_guard_conservative_until_ts):
            reasons.append(f"regime_guard_{int(self.regime_guard_conservative_until_ts - now_ts)}s")
        current_slug = str(self.current_market_slug or "")
        market_stop_loss_count = int(self.market_stop_loss_count_by_slug.get(current_slug, 0))
        if (
            current_slug
            and self.market_stop_loss_max_per_market > 0
            and market_stop_loss_count >= self.market_stop_loss_max_per_market
        ):
            reasons.append(
                f"market_stop_loss_limit_{market_stop_loss_count}/{self.market_stop_loss_max_per_market}"
            )
        if self.bi_side_enabled:
            if self.active_side == ActiveSide.NONE:
                reasons.append("active_side_none")
            elif self.active_side == ActiveSide.DOWN and self.current_down_instrument_id is None:
                reasons.append("down_instrument_missing")

        bid_txt = f"{float(self.latest_market_bid):.4f}" if self.latest_market_bid is not None else "None"
        ask_txt = f"{float(self.latest_market_ask):.4f}" if self.latest_market_ask is not None else "None"
        stale_for = (now_ts - self.last_valid_quote_ts) if self.last_valid_quote_ts > 0 else -1.0
        active_orders = list(self.active_maker_orders.keys())
        tradable = "YES" if len(reasons) == 0 else "NO"
        reason_txt = "ok" if len(reasons) == 0 else ",".join(reasons)
        side_score_txt = f"{float(self.side_decision_score):+.2f}" if self.bi_side_enabled else "n/a"
        side_reason_txt = self.side_decision_reason if self.bi_side_enabled else "disabled"
        side_locked_txt = "1" if self.active_side_locked else "0"
        side_due_in = max(0.0, self.side_decision_due_ts - now_ts) if self.bi_side_enabled and self.side_decision_due_ts > 0 else 0.0
        selected_ref_spot, selected_ref_src, selected_ref_age = self._capture_market_open_spot_detail(now_ts=now_ts)
        ref_spot_txt = f"{float(selected_ref_spot):.2f}" if selected_ref_spot is not None else "None"
        ref_src_txt = str(selected_ref_src or "-")
        ref_age = float(selected_ref_age) if selected_ref_age is not None else -1.0
        binance_spot_txt = f"{float(self._binance_ws_price):.2f}" if self._binance_ws_price is not None else "None"
        binance_age = max(0.0, now_ts - float(self._binance_ws_price_ts or 0.0)) if self._binance_ws_price_ts > 0 else -1.0

        logger.info(
            "STATUS "
            f"tradable={tradable} reason={reason_txt} "
            f"phase={self.market_phase.value} "
            f"slug={self.current_market_slug or '-'} "
            f"instrument={self.instrument_id or '-'} "
            f"active_side={self.active_side.value} "
            f"side_score={side_score_txt} "
            f"side_locked={side_locked_txt} "
            f"side_reason={side_reason_txt} "
            f"side_due_in={side_due_in:.1f}s "
            f"ref_spot={ref_spot_txt} ref_src={ref_src_txt} ref_age={ref_age:.1f}s "
            f"binance_spot={binance_spot_txt} binance_age={binance_age:.1f}s "
            f"bid={bid_txt} ask={ask_txt} "
            f"stale_for={stale_for:.1f}s invalid_ticks={self.consecutive_invalid_quote_ticks} "
            f"inventory={float(self.inventory_delta_shares):.4f}/{float(self.maker_max_inventory_shares):.4f} "
            f"active_orders={active_orders}"
            f"{self._format_time_left()}"
        )
        if "active_side_none" in reasons and self.bi_side_enabled:
            throttle_key = f"{self.current_market_slug or '-'}:active_side_none"
            last_ts = float(getattr(self, "_last_no_trade_reason_event_ts_by_key", {}).get(throttle_key, 0.0))
            if now_ts - last_ts >= max(30.0, float(self.strategy_status_interval_sec)):
                if not hasattr(self, "_last_no_trade_reason_event_ts_by_key"):
                    self._last_no_trade_reason_event_ts_by_key = {}
                self._last_no_trade_reason_event_ts_by_key[throttle_key] = now_ts
                self._db_strategy_event(
                    "NO_TRADE_ACTIVE_SIDE_NONE",
                    {
                        "phase": self.market_phase.value,
                        "side_score": float(self.side_decision_score),
                        "side_reason": self.side_decision_reason,
                        "side_locked": bool(self.active_side_locked),
                        "time_left_sec": (
                            float(self.current_market_end_timestamp - now_ts)
                            if self.current_market_end_timestamp is not None
                            else None
                        ),
                    },
                )

    def _format_time_left(self) -> str:
        """Format remaining time and next-market info for status line."""
        parts: List[str] = []
        end_ts = getattr(self, "current_market_end_timestamp", None)
        if end_ts is not None:
            remaining = end_ts - time.time()
            if remaining > 0:
                parts.append(f" time_left={remaining / 60:.1f}m")
            else:
                parts.append(f" time_left=ENDED({abs(remaining):.0f}s ago)")
        if self.next_market_slug:
            if self.next_market_start_ts:
                until = self.next_market_start_ts - time.time()
                parts.append(f" next={self.next_market_slug}(in {until / 60:.1f}m)")
            else:
                parts.append(f" next={self.next_market_slug}")
        return "".join(parts)

    # ------------------------------------------------------------------
    # Market Lifecycle State Machine
    # ------------------------------------------------------------------

    def _update_market_phase(self) -> MarketPhase:
        """
        Evaluate current time vs market end timestamp and transition
        between lifecycle phases.

        Returns the current phase after evaluation.
        """
        now_ts = time.time()
        end_ts = getattr(self, "current_market_end_timestamp", None)
        decision = evaluate_market_phase(
            current_phase_value=self.market_phase.value,
            end_ts=end_ts,
            now_ts=now_ts,
            min_minutes_to_close=self.maker_min_minutes_to_close,
            settling_since_ts=self._market_settling_since_ts,
            settling_grace_sec=self.market_settling_grace_sec,
        )
        if decision is not None:
            if decision.set_settling_since:
                self._market_settling_since_ts = now_ts
            self._transition_market_phase(MarketPhase(decision.next_phase_value), now_ts)

        return self.market_phase

    def _align_price_to_tick(self, price: Decimal, side: str, instrument: Optional[Any]) -> Decimal:
        return align_price_to_tick(self, price, side, instrument)

    def _start_maker_worker(self, bid_decimal: Decimal, ask_decimal: Decimal) -> None:
        start_maker_worker(self, bid_decimal, ask_decimal)

    def _find_btc_instrument(self):
        return find_btc_instrument(self)

    def _wait_for_btc_instrument(self, timeout_sec: int = 60, poll_interval_sec: int = 2) -> bool:
        return wait_for_btc_instrument(self, timeout_sec=timeout_sec, poll_interval_sec=poll_interval_sec)
                        
    def on_quote_tick(self, tick: QuoteTick):
        handle_quote_tick(self, tick)

    def _maker_quote_sync(self, bid_price: float, ask_price: float) -> None:
        maker_quote_sync(self, bid_price, ask_price)
	                                            
    def on_order_filled(self, event):
        handle_order_filled(self, event)

    def on_event(self, event):
        handle_generic_event(self, event)

    def on_order_canceled(self, event):
        handle_order_canceled(self, event)
    
    def on_order_cancel_rejected(self, event):
        handle_order_cancel_rejected(self, event)
    
    def on_order_denied(self, event):
        self._handle_order_rejection_like_event(event, title="ORDER DENIED")

    def on_order_rejected(self, event):
        self._handle_order_rejection_like_event(event, title="ORDER REJECTED")

    def _handle_order_rejection_like_event(self, event, title: str = "ORDER REJECTED") -> None:
        handle_order_rejection_like_event(self, event, title=title)
    
    def on_stop(self):
        handle_stop(self)


if __name__ == "__main__":
    from bot.launcher import main

    main()
