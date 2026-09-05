import argparse
import asyncio
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loguru import logger

from alert_watcher import AlertWatcher
from dashboard_state import DashboardState
from bot.adapters.outcome_auth import OutcomeAuth
from bot.adapters.outcome_client import OutcomeClient
from bot.lifecycle.outcome_lifecycle import (
    OutcomeMarketSpec,
    discover_btc_markets_by_preference,
    discover_btc_15m_markets,
    parse_period_preferences,
    select_configured_btc_market,
    select_active_or_next_btc_market,
    evaluate_outcome_market_phase,
)
from bot.outcome_daily_scope import resolve_daily_outcome_scope
from bot.pricing.outcome_pricing import (
    OutcomePricingState,
    compute_min_shares_for_notional,
)
from bot.outcome_live_execution_runtime import OutcomeLiveExecutionRuntime
from bot.outcome_live_strategy import OutcomeOiEntryGate
from bot.outcome_settlement import OutcomeSettlementAdapter
from bot.outcome_pnl_reconciliation import OutcomePnLReconciler
from bot.outcome_ws_recorder import OutcomeWebSocketRecorder
from bot.outcome_execution_ledger import OutcomeExecutionLedger
from bot.outcome_operations_monitor import OutcomeOperationsMonitor
from bot.outcome_research_capture import OutcomeResearchCapture
from bot.outcome_research_worker import OutcomeResearchWorker
from bot.outcome_rollover import OutcomeRolloverCoordinator
from monitoring.trade_journal_db import TradeJournalDB
from bot.enums import MarketPhase
from bot.app_config import AppConfig
from bot.runtime_env import load_runtime_env
from bot.process_lock import ProcessLock
from telegram_bot import start_telegram_bot_thread
from telegram_notifier import TelegramNotifier


def resolve_hyperliquid_auth() -> Optional[OutcomeAuth]:
    """
    Resolve and initialize Hyperliquid Outcome auth from environment.
    """
    wallet_address = (
        os.getenv("HL_WALLET_ADDRESS")
        or os.getenv("HYPERLIQUID_WALLET_ADDRESS")
        or os.getenv("POLYMARKET_WALLET_ADDRESS")
        or os.getenv("POLYMARKET_FUNDER")
        or ""
    )
    private_key = (
        os.getenv("HL_PRIVATE_KEY")
        or os.getenv("HYPERLIQUID_PRIVATE_KEY")
        or os.getenv("POLYMARKET_PK")
        or ""
    )
    agent_private_key = (
        os.getenv("HL_AGENT_PRIVATE_KEY")
        or os.getenv("HYPERLIQUID_AGENT_PRIVATE_KEY")
        or ""
    )
    is_testnet = os.getenv("HL_TESTNET", os.getenv("HYPERLIQUID_TESTNET", "0")).strip().lower() in ("1", "true", "yes", "on")
    base_url = os.getenv("HL_BASE_URL", os.getenv("HYPERLIQUID_BASE_URL", None))
    ws_url = os.getenv("HL_WS_URL", os.getenv("HYPERLIQUID_WS_URL", None))

    if not wallet_address and not private_key:
        logger.error("Hyperliquid auth resolution failed: no wallet address or private key in .env.")
        return None

    return OutcomeAuth(
        wallet_address=wallet_address,
        private_key=private_key if private_key else None,
        agent_private_key=agent_private_key if agent_private_key else None,
        is_testnet=is_testnet,
        base_url=base_url,
        ws_url=ws_url,
    )


def run_hyperliquid_preflight_checks(simulation: bool) -> bool:
    """
    Perform preflight connectivity, credentials, and market feed checks for Hyperliquid.
    """
    logger.info("Hyperliquid Outcome preflight check started.")
    auth = resolve_hyperliquid_auth()
    if not auth:
        logger.error("Hyperliquid auth resolution failed.")
        return False

    client = OutcomeClient(auth)
    logger.info(f"Hyperliquid auth: wallet={auth.wallet_address} agent={auth.agent_address} is_testnet={auth.is_testnet}")

    try:
        meta = client.get_outcome_meta_sync()
        preferences, allow_fallback = resolve_daily_outcome_scope(os.environ)
        selected, status, selected_period, fallback_used = select_configured_btc_market(meta, period_preferences=preferences, allow_fallback=allow_fallback)
        logger.info(f"Hyperliquid Outcome market preferences={preferences} selected_period={selected_period} fallback={fallback_used}")
        if selected:
            logger.info(f"Active/Upcoming Outcome Market: ID={selected.outcome_id} Period={selected.period} Strike=${selected.strike} Expiry={selected.expiry_str} Status={status or 'active'}")
            
            # Probe live L2 order book
            book_yes = client.get_l2_book_sync(selected.yes_coin)
            levels_yes = book_yes.get("levels", [[], []])
            bids_yes = levels_yes[0] if len(levels_yes) > 0 else []
            asks_yes = levels_yes[1] if len(levels_yes) > 1 else []
            best_bid_yes = bids_yes[0]["px"] if bids_yes else "None"
            best_ask_yes = asks_yes[0]["px"] if asks_yes else "None"
            
            all_mids = client.get_all_mids_sync()
            btc_mark = all_mids.get("BTC", "N/A")
            logger.info(f"Live Market Feeds Verified -> BTC Mark: ${btc_mark} | YES ({selected.yes_coin}) Bid: {best_bid_yes}, Ask: {best_ask_yes} | Levels: {len(bids_yes)}/{len(asks_yes)}")
        else:
            logger.warning("No active Outcome BTC market right now (waiting for upcoming)")
    except Exception as e:
        logger.warning(f"Live Outcome API check note: {e}")

    mode_text = "SIMULATION" if simulation else "LIVE TRADING"
    logger.info(f"Hyperliquid preflight mode target: {mode_text}")
    logger.info("HYPERLIQUID PREFLIGHT CHECK PASSED")
    return True


def _strategy_requested_rollover(node: Any) -> bool:
    """Check if strategy requested a scheduled rollover."""
    if node is None:
        return False
    trader = getattr(node, "trader", None)
    if trader and hasattr(trader, "strategies"):
        for strat in trader.strategies():
            if getattr(strat, "_rollover_requested_flag", False) or getattr(strat, "_rollover_requested", False):
                return True
    if hasattr(node, "_scheduled_rollover_requested"):
        flag = getattr(node, "_scheduled_rollover_requested")
        return bool(flag.is_set() if hasattr(flag, "is_set") else flag)
    return bool(getattr(node, "_rollover_requested", False) or getattr(node, "_rollover_requested_flag", False))


def acquire_live_process_lock() -> ProcessLock | None:
    """Prevent two live launchers on the same host from sharing one wallet."""
    lock_path = os.getenv("LIVE_PROCESS_LOCK_PATH", "/tmp/hyperliquid-outcome-strategy-live.lock")
    lock = ProcessLock(lock_path)
    if lock.acquire():
        return lock
    logger.error(
        f"Another local live bot process already holds {lock_path}. "
        "Refusing to start a second wallet writer."
    )
    return None


def run_integrated_hyperliquid_bot(
    simulation: bool = True,
    test_mode: bool = True,
    enable_terminal_dashboard: bool = False,
):
    """
    Run Hyperliquid Outcome (HIP-4) Prediction Market Trading Node.
    """
    logger.info("Starting integrated Hyperliquid Outcome prediction market trading bot.")

    # ``bot.launcher --live`` already requires a typed confirmation and holds
    # the one-wallet process lock.  It is therefore the sole production
    # execution authorization: do not make the operator repeat four fragile
    # environment flags for the same decision.  Shadow/preflight invocation
    # never reaches this live branch.
    if not simulation:
        os.environ["OUTCOME_AUTOMATED_EXECUTION_ENABLED"] = "1"
        os.environ["OUTCOME_SDK_EXECUTION_ENABLED"] = "1"
        os.environ["OUTCOME_LIVE_STRATEGY_ENABLED"] = "1"
        os.environ["OUTCOME_EXIT_REQUOTE_ENABLED"] = "1"

    auth = resolve_hyperliquid_auth()
    if not auth:
        raise RuntimeError("Cannot resolve Hyperliquid auth (provide HL_WALLET_ADDRESS and HL_PRIVATE_KEY in .env).")

    client = OutcomeClient(auth)
    pricing = OutcomePricingState()
    # Live orders are only dispatched through the recovery-aware official SDK
    # runtime; this remains inert without both explicit execution gates.
    # P3 calibration shares the shadow journal so confirmed fills can be
    # paired with its accepted v3 quote history for executable markouts.
    default_execution_journal = "./logs/outcome_shadow.db" if (
        os.getenv("OUTCOME_P3_CALIBRATION_ENABLED") == "1"
        or os.getenv("OUTCOME_LIVE_STRATEGY_ENABLED") == "1"
    ) else "./logs/outcome_execution.db"
    live_journal = TradeJournalDB(os.getenv("OUTCOME_EXECUTION_JOURNAL_PATH", default_execution_journal))
    live_ws_recorder: OutcomeWebSocketRecorder | None = None
    # This is a venue-read-only capture path.  It shares the live journal but
    # never owns /exchange submission; the live runtime remains the sole
    # wallet writer.
    research_capture = None
    research_worker = None
    if not simulation:
        # Keep capture off the wallet/execution loop.  This client is used
        # exclusively by the worker, whose code has no order submission or
        # cancellation path.
        research_client = OutcomeClient(auth, timeout_sec=5.0)
        research_capture = OutcomeResearchCapture(
            client=research_client, wallet_address=auth.wallet_address, journal=live_journal,
        )
        research_worker = OutcomeResearchWorker(
            client=research_client, capture=research_capture, journal=live_journal,
        )
        research_worker.start()
    ops_monitor = OutcomeOperationsMonitor(live_journal, f"outcome-ops-{uuid.uuid4().hex[:10]}")
    live_execution = OutcomeLiveExecutionRuntime(
        account=client, wallet=auth.wallet_address,
        ledger=OutcomeExecutionLedger(live_journal, f"outcome-live-{uuid.uuid4().hex[:10]}"),
    )
    live_strategy_gate = OutcomeOiEntryGate(live_journal.db_path) if live_execution.live_strategy_enabled() else None
    settlement_adapter = OutcomeSettlementAdapter()
    pnl_reconciler = OutcomePnLReconciler(live_journal, f"outcome-pnl-{uuid.uuid4().hex[:10]}")

    dashboard_state = DashboardState(
        strike_price=0.0,
        spot_price=0.0,
        position_side=None,
        position_entry=None,
        position_qty=None,
        position_ask=None,
        current_market_price=0.0,
        trades=[],
        cumulative_pnl=0.0,
        usdc_balance=0.0,
        pol_balance=0.0,
        account_last_updated=datetime.now(timezone.utc),
    )
    telegram_notifier = TelegramNotifier()
    alert_watcher = AlertWatcher()
    telegram_thread = start_telegram_bot_thread(dashboard_state)
    if telegram_thread is not None:
        logger.info("Telegram bot controller started in background thread.")

    terminal_dash = None
    if enable_terminal_dashboard:
        from monitoring.terminal_dashboard import TerminalDashboard
        terminal_dash = TerminalDashboard(title="Hyperliquid Outcome BTC Bot")
        terminal_dash.start()

    logger.info(
        f"Hyperliquid Trading Node active: mode={'SIMULATION' if simulation else 'LIVE'} "
        f"wallet={auth.wallet_address} testnet={auth.is_testnet}"
    )

    current_market: Optional[OutcomeMarketSpec] = None
    active_order_id: Optional[str] = None
    inventory_side: Optional[str] = None
    inventory_shares: float = 0.0
    inventory_entry_px: float = 0.0
    settled_markets: Set[int] = set()
    settlement_last_attempt_at: dict[int, float] = {}
    pnl_last_attempt_at = 0.0
    rollover = OutcomeRolloverCoordinator()

    refresh_interval = 1.5 if not test_mode else 1.0
    last_log_time = 0.0
    market_preferences, market_allow_fallback = resolve_daily_outcome_scope(os.environ)

    try:
        while True:
            cycle_start = time.time()

            # 1. Discover active market (with 20s TTL cache to respect API rate limits)
            try:
                meta = client.get_outcome_meta_sync(ttl_sec=20.0)
                market, status, selected_period, fallback_used = select_configured_btc_market(
                    meta, period_preferences=market_preferences, allow_fallback=market_allow_fallback,
                )
                configured_markets, _, _ = discover_btc_markets_by_preference(
                    meta, period_preferences=market_preferences, allow_fallback=market_allow_fallback,
                )
                if fallback_used:
                    logger.info(f"Outcome preferred market unavailable; using configured fallback period={selected_period}")
            except Exception as e:
                logger.warning(f"Error fetching outcome metadata: {e}")
                time.sleep(3.0)
                continue

            if market is None:
                if time.time() - last_log_time > 10:
                    logger.info("Waiting for active Outcome BTC prediction market...")
                    last_log_time = time.time()
                time.sleep(2.0)
                continue

            current_market = market
            if research_worker is not None:
                research_worker.set_market(market)
            retiring_markets = rollover.observe(selected=market, discovered=configured_markets)

            # Derived accounting uses only prior official fills.  Keep it out
            # of the high-frequency decision path; it has no venue-write side
            # effects and is idempotent across restarts.
            if not simulation and time.monotonic() - pnl_last_attempt_at >= 30.0:
                pnl_last_attempt_at = time.monotonic()
                try:
                    written = pnl_reconciler.reconcile_sells()
                    if written:
                        logger.info(f"[OUTCOME PNL] recorded {written} canonical FIFO sell lots")
                except Exception as e:
                    live_journal.log_strategy_event("outcome-pnl-error", "OUTCOME_PNL_RECONCILE_ERROR", {
                        "venue": "hyperliquid_outcome", "stage": "sell_fifo",
                        "error_type": type(e).__name__, "error": str(e),
                    })

            # 2. Update pricing feeds
            book_yes: dict[str, Any] | None = None
            book_no: dict[str, Any] | None = None
            yes_received_at_ms = no_received_at_ms = capture_complete_at_ms = 0
            try:
                all_mids = client.get_all_mids_sync()
                btc_mark_str = all_mids.get("BTC", "0")
                btc_mark = float(btc_mark_str)
                if btc_mark > 0:
                    pricing.update_btc_mark_price(btc_mark)

                book_yes = client.get_l2_book_sync(market.yes_coin)
                yes_received_at_ms = int(time.time() * 1000)
                book_no = client.get_l2_book_sync(market.no_coin)
                no_received_at_ms = int(time.time() * 1000)
                capture_complete_at_ms = int(time.time() * 1000)
                pricing.update_l2_book(market.yes_coin, book_yes)
                pricing.update_l2_book(market.no_coin, book_no)
            except Exception as e:
                logger.debug(f"Pricing update note: {e}")

            if not simulation:
                try:
                    if live_ws_recorder is None or live_ws_recorder._market_id != market.outcome_id:
                        if live_ws_recorder is not None:
                            live_ws_recorder.stop()
                        live_ws_recorder = OutcomeWebSocketRecorder(
                            client, live_journal, f"outcome-live-ws-{uuid.uuid4().hex[:10]}",
                            pricing_state=pricing,
                        )
                    live_ws_recorder.start(outcome_id=market.outcome_id, yes_coin=market.yes_coin, no_coin=market.no_coin)
                    if live_ws_recorder.resync_required.is_set():
                        # The REST reads above are the mandatory post-connect
                        # snapshot before a WS stream can permit entry.
                        live_ws_recorder.mark_rest_resynced()
                        live_ws_recorder.resync_required.clear()
                    live_execution.stream_health = live_ws_recorder.health
                except Exception as e:
                    logger.warning(f"Outcome WS health setup failed; runtime will fail closed: {e}")
                operational = ops_monitor.observe(
                    market=market, fallback_used=fallback_used,
                    stream_health=live_ws_recorder.health if live_ws_recorder else None,
                    automated_execution_enabled=OutcomeLiveExecutionRuntime.enabled(),
                )
                if time.time() - last_log_time > 10:
                    logger.info(
                        f"[OUTCOME OPS] market=#{operational.market_id} period={operational.period} "
                        f"fallback={operational.fallback_used} ws={operational.ws_reason} "
                        f"auto_execution={operational.automated_execution_enabled}"
                    )
                    last_log_time = time.time()

            phase = evaluate_outcome_market_phase(market)
            time_left = market.time_to_expiry_sec()
            spot_px = float(pricing.btc_mark_price or 0.0)
            strike_px = float(market.strike)

            mid_yes = pricing.get_outcome_mid(market.yes_coin)
            mid_no = pricing.get_outcome_mid(market.no_coin)
            best_bid_yes, best_ask_yes = pricing.get_best_bid_ask(market.yes_coin)
            best_bid_no, best_ask_no = pricing.get_best_bid_ask(market.no_coin)

            # 3. Strategy decision & signal evaluation (Directional UP / DOWN / NONE)
            active_side = None
            side_score = 0.0
            p_fair = 0.50

            if spot_px > 0 and strike_px > 0:
                pct_diff = (spot_px - strike_px) / strike_px
                side_score = max(-1.0, min(1.0, pct_diff * 100.0))
                if side_score >= 0.20:
                    active_side = "UP"
                    p_fair = min(0.95, 0.50 + side_score * 0.40)
                elif side_score <= -0.20:
                    active_side = "DOWN"
                    p_fair = max(0.05, 0.50 + side_score * 0.40)
                else:
                    active_side = "NONE"
                    p_fair = 0.50

            # 4. State machine execution (Hold-to-Redeem directional logic)
            if phase == MarketPhase.ACTIVE:
                if not simulation:
                    try:
                        if live_strategy_gate is not None:
                            decision = live_strategy_gate.evaluate(
                                spot_price=Decimal(str(spot_px)) if spot_px > 0 else None,
                                strike_price=Decimal(str(strike_px)) if strike_px > 0 else None,
                            )
                            runtime_result = live_execution.tick_live_strategy(
                                market=market, entry_side_index=decision.side_index,
                                entry_reason=decision.reason, entry_evidence=decision.evidence,
                                retiring_markets=retiring_markets,
                                market_context={
                                    **decision.evidence, "spot_price": str(spot_px),
                                    "strike_price": str(strike_px),
                                },
                            )
                        elif live_execution.calibration_enabled():
                            runtime_result = live_execution.tick_p3_calibration(market=market)
                        else:
                            entry_side_index = 0 if active_side == "UP" else 1 if active_side == "DOWN" else None
                            runtime_result = live_execution.tick_market(market=market, entry_side_index=entry_side_index)
                        active_order_id = runtime_result.order_id if runtime_result.state in {"buy_placed", "buy_resting"} else None
                        logger.info(f"[LIVE OUTCOME RUNTIME] state={runtime_result.state} detail={runtime_result.detail} order={runtime_result.order_id}")
                    except Exception as e:
                        logger.error(f"Outcome live runtime failed closed: {e}")
                # Maker Buy quoting logic (1 entry per market invariant)
                if simulation and inventory_shares <= 0 and active_order_id is None and active_side in ("UP", "DOWN"):
                    target_coin = market.yes_coin if active_side == "UP" else market.no_coin
                    target_bid = float(best_bid_yes if active_side == "UP" else best_bid_no or 0.50)

                    if target_bid > 0 and target_bid < 0.95:
                        min_shares = compute_min_shares_for_notional(target_bid, min_notional_usdc=Decimal("10.0"))

                        if simulation:
                            active_order_id = f"sim_buy_{int(time.time())}"
                            logger.info(
                                f"[SIMULATION MAKER BUY] Placed ALO GTC on {target_coin} ({active_side}) "
                                f"at ${target_bid:.4f} x {min_shares} shares (Notional: ${target_bid * float(min_shares):.2f})"
                            )
                            inventory_side = active_side
                            inventory_shares = float(min_shares)
                            inventory_entry_px = target_bid
                            if terminal_dash:
                                terminal_dash.record_order_submitted(
                                    side="buy",
                                    token_side=active_side,
                                    qty=float(min_shares),
                                    price=target_bid,
                                    client_order_id=active_order_id,
                                    is_taker=False,
                                )
                                terminal_dash.increment_fill(
                                    is_maker_fill=True,
                                    side="buy",
                                    token_side=active_side,
                                    qty=float(min_shares),
                                    price=target_bid,
                                    commission_usdc=0.0,
                                    client_order_id=active_order_id,
                                    is_taker_exit=False,
                                )
                            active_order_id = None
                            logger.info(f"[SIMULATION FILL] Filled {min_shares} {active_side} @ ${target_bid:.4f}")

            elif phase == MarketPhase.REDUCE_ONLY:
                if simulation:
                    if active_order_id:
                        if terminal_dash:
                            terminal_dash.record_order_canceled(client_order_id=active_order_id)
                        active_order_id = None
                        logger.info("[SIMULATION] Cancelled active entry order in REDUCE_ONLY phase.")
                else:
                    try:
                        # Account truth, not the process-local order id, owns
                        # the daily one-hour reduce-only cancellation.
                        result = live_execution.cancel_resting_buys(
                            market=market, tracked_markets=retiring_markets,
                        )
                        logger.info(f"[LIVE OUTCOME REDUCE_ONLY] state={result.state} detail={result.detail}")
                        active_order_id = None
                    except Exception as e:
                        logger.error(f"Error cancelling open order: {e}")

            # A process may have been down during the one-hour tail.  Once a
            # new daily instance is selected, cancel any inherited *old* buy
            # orders as well.  Never touch old protective sells or inventory.
            if not simulation:
                for retiring_market in retiring_markets:
                    try:
                        result = live_execution.cancel_resting_buys(
                            market=retiring_market,
                            tracked_markets=(market, *retiring_markets),
                        )
                        if result.state == "cancelled":
                            logger.info(
                                f"[OUTCOME ROLLOVER] cancelled retiring-market entry buys "
                                f"market=#{retiring_market.outcome_id} order={result.order_id}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"[OUTCOME ROLLOVER] retiring market #{retiring_market.outcome_id} "
                            f"buy-cancel reconciliation failed: {e}"
                        )

            # Settlement outlives active-market selection.  Keep requesting
            # official evidence for retiring instances until confirmed; BTC
            # price and UI state are never used as a settlement proxy.
            settlement_candidates = {retiring_market.outcome_id for retiring_market in retiring_markets}
            if phase == MarketPhase.SETTLING:
                settlement_candidates.add(market.outcome_id)
            # Process restarts must not lose a previous daily market's
            # settlement evidence.  Recover open journal inventory by market
            # id; the official SDK endpoint only needs that id.
            if not simulation:
                settlement_candidates.update(pnl_reconciler.unresolved_outcome_ids())
            for settlement_market_id in settlement_candidates:
                if settlement_market_id not in settled_markets:
                    now_monotonic = time.monotonic()
                    if now_monotonic - settlement_last_attempt_at.get(settlement_market_id, 0.0) < 30.0:
                        continue
                    settlement_last_attempt_at[settlement_market_id] = now_monotonic
                    if not simulation:
                        try:
                            settlement = settlement_adapter.fetch_outcome_id(settlement_market_id)
                            if not settlement.settled:
                                logger.info(f"[OUTCOME SETTLEMENT] #{settlement_market_id} not yet confirmed by official SDK; holding state.")
                            else:
                                logger.info(
                                    f"[OUTCOME SETTLEMENT] #{settlement_market_id} confirmed by official SDK "
                                    f"fraction={settlement.settle_fraction} details={settlement.details}"
                                )
                                # Confirmation alone is not PnL.  Require the
                                # official account's payout or zero-balance
                                # evidence before a canonical settlement row.
                                account_fills = client.get_user_fills_sync(auth.wallet_address)
                                clearinghouse = client.get_spot_clearinghouse_state_sync(auth.wallet_address)
                                status = pnl_reconciler.reconcile_settlement(
                                    settlement=settlement, raw_fills=account_fills, clearinghouse=clearinghouse,
                                )
                                if status in {"recorded", "already_recorded"}:
                                    settled_markets.add(settlement_market_id)
                                    logger.info(f"[OUTCOME SETTLEMENT] #{settlement_market_id} canonical PnL {status}")
                                else:
                                    logger.info(
                                        f"[OUTCOME SETTLEMENT] #{settlement_market_id} confirmation retained; "
                                        f"canonical PnL {status}"
                                    )
                        except Exception as e:
                            logger.warning(f"[OUTCOME SETTLEMENT] official confirmation unavailable: {e}")
                    else:
                        settled_markets.add(settlement_market_id)
                        logger.info(
                            f"[OUTCOME SETTLEMENT] simulation marked #{settlement_market_id} "
                            "terminal without inferring payout from BTC price"
                        )

            # 5. Update dashboard state
            with dashboard_state._lock:
                dashboard_state.strike_price = strike_px
                dashboard_state.spot_price = spot_px
                dashboard_state.current_market_price = float(mid_yes or 0.0)
                dashboard_state.market_phase = phase.name
                dashboard_state.active_side = active_side
                dashboard_state.time_left_sec = time_left
                dashboard_state.side_score = side_score
                dashboard_state.p_fair = p_fair
                dashboard_state.book_bid = float(best_bid_yes or 0.0)
                dashboard_state.book_ask = float(best_ask_yes or 0.0)
                dashboard_state.book_mid = float(mid_yes or 0.0)
                if inventory_shares > 0:
                    dashboard_state.position_side = inventory_side
                    dashboard_state.position_qty = inventory_shares
                    dashboard_state.position_entry = inventory_entry_px

            if terminal_dash:
                terminal_dash.update(
                    phase=phase.name,
                    slug=f"Outcome #{market.outcome_id} ({market.period.upper()})",
                    strike=strike_px if strike_px > 0 else None,
                    spot=spot_px if spot_px > 0 else None,
                    spot_minus_strike=(spot_px - strike_px) if (spot_px > 0 and strike_px > 0) else None,
                    time_left_str=f"{time_left / 3600:.2f}h ({time_left:.0f}s)",
                    signal_str=f"{active_side} (Score: {side_score:+.2f}, Fair: {p_fair:.1%})",
                    position_desc=f"{inventory_shares:.1f} {inventory_side} @ ${inventory_entry_px:.4f}" if inventory_shares > 0 else "FLAT (0.0 shares)",
                    yes_coin=market.yes_coin,
                    no_coin=market.no_coin,
                    yes_bid_ask=f"{best_bid_yes}/{best_ask_yes}",
                    no_bid_ask=f"{best_bid_no}/{best_ask_no}",
                    current_buy_order=f"ALO BUY {active_side} {inventory_shares:.1f} @ ${inventory_entry_px:.4f}" if inventory_shares > 0 else "No active buy order",
                    current_sell_order="Tail-Protect TP @ $0.9700" if inventory_shares > 0 else "No active sell order",
                    inventory_shares=inventory_shares,
                    wallet_balance_usdc=100.0,
                )

            if not enable_terminal_dashboard and time.time() - last_log_time >= 5.0:
                last_log_time = time.time()
                logger.info(
                    f"[{phase.name}] #{market.outcome_id} ({market.period}) | "
                    f"Spot: ${spot_px:,.2f} / Strike: ${strike_px:,.2f} | "
                    f"TimeLeft: {time_left / 3600:.2f}h ({time_left:.0f}s) | Signal: {active_side} (Score: {side_score:+.2f}) | "
                    f"YES Bid/Ask: {best_bid_yes}/{best_ask_yes} | NO Bid/Ask: {best_bid_no}/{best_ask_no} | "
                    f"Pos: {inventory_shares:.1f} {inventory_side or '-'}"
                )

            elapsed = time.time() - cycle_start
            time.sleep(max(0.1, refresh_interval - elapsed))

    except KeyboardInterrupt:
        logger.info("Hyperliquid trading bot stopped by user.")
    finally:
        if research_worker is not None:
            research_worker.stop()
        if live_ws_recorder is not None:
            live_ws_recorder.stop()
        if terminal_dash:
            terminal_dash.stop()
        logger.info("Hyperliquid trading bot shutdown complete.")


def main():
    load_runtime_env()
    parser = argparse.ArgumentParser(description="Integrated Hyperliquid BTC Prediction Trading Bot")
    parser.add_argument(
        "--venue",
        choices=["hyperliquid", "polymarket"],
        default=os.getenv("VENUE", "hyperliquid").lower(),
        help="Target prediction market venue (default: hyperliquid)"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run in LIVE mode (real money at risk!). Default is simulation."
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Run in TEST MODE (faster check interval for testing)"
    )
    parser.add_argument(
        "--terminal-dashboard",
        action="store_true",
        help="Show simplified Rich terminal dashboard"
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run safety checks only (no trading node startup)"
    )

    args = parser.parse_args()

    simulation = not args.live
    test_mode = bool(args.test_mode or not args.live)
    enable_terminal_dashboard = args.terminal_dashboard
    app_config = AppConfig.from_env(enable_terminal_dashboard=enable_terminal_dashboard)

    if enable_terminal_dashboard:
        logger.remove()
        log_dir = Path("logs/bot")
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(str(log_dir / "terminal_bot.log"), rotation="20 MB", retention="5 days", level="DEBUG")
        print(f"\n[INFO] Terminal dashboard enabled.")
        print(f"[INFO] Background logs are re-routed to: {log_dir}/terminal_bot.log")
        print(f"[INFO] Tip: Run 'tail -f {log_dir}/terminal_bot.log' in another terminal to view live logs.\n")

    if not run_hyperliquid_preflight_checks(simulation=simulation):
        print("Preflight check failed. Startup aborted.")
        return

    if args.preflight_only:
        print("Preflight check passed. Exiting without starting bot.")
        return

    if not simulation:
        print("WARNING: LIVE TRADING MODE - REAL MONEY AT RISK!")
        confirm = input("Type 'yes' to continue: ")
        if confirm.lower() != "yes":
            print("Cancelled.")
            return

    live_lock = acquire_live_process_lock() if not simulation else None
    if not simulation and live_lock is None:
        return
    try:
        run_integrated_hyperliquid_bot(
            simulation=simulation,
            test_mode=test_mode,
            enable_terminal_dashboard=enable_terminal_dashboard,
        )
    finally:
        if live_lock is not None:
            live_lock.release()


if __name__ == "__main__":
    main()
