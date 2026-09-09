"""Single official execution runtime invoked by a strategy loop.

It is deliberately disabled unless the operator enables all execution gates.
It makes the account-recovery report a precondition for each state-machine
tick, so restart behaviour never depends on stale local order IDs.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR

from bot.adapters.outcome_client import OutcomeClient
from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_account_recovery import OutcomeAccountRecovery
from bot.outcome_account_read_cache import OutcomeAccountReadCache
from bot.outcome_execution_gateway import OutcomeExecutionGateway, whole_share_size
from bot.outcome_maker_state_machine import MakerTickResult, OutcomeMakerStateMachine
from bot.outcome_risk_gate import OutcomePreTradeRiskGate, OutcomeRiskLimits
from bot.outcome_stream_health import OutcomeStreamHealth
from bot.outcome_execution_ledger import OutcomeExecutionLedger
from bot.outcome_research_gate import OutcomeResearchGate
from bot.outcome_p3_calibration import OutcomeP3CalibrationConfig, choose_consensus_calibration_side, take_profit_price
from bot.outcome_live_strategy import OutcomeLiveStrategyConfig
from bot.outcome_exit_target_policy import OutcomeExitTargetPolicy
from bot.outcome_exit_lifecycle import OutcomeExitLifecycle, OutcomeExitLifecycleStore
from bot.outcome_exit_quote_planner import ExitQuoteAction, ExitQuoteInput, ExitQuotePlan, OutcomeExitQuotePlanner, OutcomeExitQuotePlannerConfig
from bot.outcome_exit_requote_controller import OutcomeExitRequoteController
from bot.outcome_entry_lifecycle import OutcomeEntryLifecycle, OutcomeEntryLifecycleStore
from bot.outcome_entry_requote import (
    EntryQuoteAction,
    EntryQuoteInput,
    OutcomeEntryFastRiskTracker,
    OutcomeEntryQuotePlanner,
    OutcomeEntryQuotePlannerConfig,
    OutcomeEntryRequoteController,
)
from bot.outcome_holding_path import OutcomeHoldingPathObservation, OutcomeHoldingPathRecorder
from bot.outcome_trend_continuation import OutcomeTrendContinuationRecorder
from bot.outcome_reversal import OutcomeReversalClassifier, OutcomeReversalInput, OutcomeReversalState
from bot.outcome_loss_reentry import OutcomeLossReentryGate
from bot.outcome_tier_b_execution_gate import OutcomeTierBExecutionGate
from bot.outcome_portfolio_guard import OutcomePortfolioGuard
from bot.outcome_market_regime import (
    OutcomeMarketRegimeInput,
    OutcomeMarketRegimeShadow,
    OutcomeToxicFillInput,
)
from bot.outcome_emergency_exit import (
    EmergencyExitAction,
    OutcomeEmergencyExitController,
    OutcomeEmergencyExitInput,
    OutcomeEmergencyExitPolicy,
    book_age_sec as emergency_book_age_sec,
    parse_bid_levels,
)


@dataclass(frozen=True)
class LiveExecutionResult:
    state: str
    detail: str
    order_id: str | None = None


class OutcomeLiveExecutionRuntime:
    # Holding-path research is low-frequency evidence, not an execution
    # trigger.  Sampling it every 1.5-second strategy turn used a full REST
    # L2 request even while a managed sell was safely resting.
    _HOLDING_PATH_MIN_INTERVAL_SEC = 30.0
    _REVERSAL_RISK_MIN_INTERVAL_SEC = 5.0

    def __init__(self, *, account: OutcomeClient, wallet: str, gateway: OutcomeExecutionGateway | None = None, risk_gate: OutcomePreTradeRiskGate | None = None, stream_health: OutcomeStreamHealth | None = None, ledger: OutcomeExecutionLedger | None = None, research_gate: OutcomeResearchGate | None = None, exit_planner: OutcomeExitQuotePlanner | None = None, exit_lifecycle_store: OutcomeExitLifecycleStore | None = None, exit_requote_controller: OutcomeExitRequoteController | None = None, entry_planner: OutcomeEntryQuotePlanner | None = None, entry_lifecycle_store: OutcomeEntryLifecycleStore | None = None, entry_requote_controller: OutcomeEntryRequoteController | None = None) -> None:
        self._account_reads = OutcomeAccountReadCache(account)
        self.recovery = OutcomeAccountRecovery(account=self._account_reads, wallet=wallet)
        self.machine = OutcomeMakerStateMachine(
            account=self._account_reads, gateway=gateway or OutcomeExecutionGateway(), wallet=wallet,
            journal=ledger.journal if ledger else None,
        )
        self.risk_gate = risk_gate or OutcomePreTradeRiskGate(OutcomeRiskLimits(
            max_entry_notional_usdc=Decimal(os.environ.get("OUTCOME_MAX_ENTRY_NOTIONAL_USDC", "11")),
            max_total_outcome_exposure_usdc=Decimal(os.environ.get("OUTCOME_MAX_OUTCOME_EXPOSURE_USDC", "11")),
            max_open_orders=int(os.environ.get("OUTCOME_MAX_OPEN_ORDERS", "1")),
        ))
        self.stream_health = stream_health
        self.ledger = ledger
        self.research_gate = research_gate or OutcomeResearchGate()
        if exit_planner is None:
            exit_planner = OutcomeExitQuotePlanner(OutcomeExitQuotePlannerConfig())
        self.exit_planner = exit_planner
        self.exit_lifecycle_store = exit_lifecycle_store or (OutcomeExitLifecycleStore(ledger.journal, ledger.run_id) if ledger else None)
        self.exit_requote_controller = exit_requote_controller or (
            OutcomeExitRequoteController(account=self._account_reads, gateway=self.machine.gateway, store=self.exit_lifecycle_store, wallet=wallet)
            if self.exit_lifecycle_store else None
        )
        self.entry_planner = entry_planner or OutcomeEntryQuotePlanner(OutcomeEntryQuotePlannerConfig())
        self.entry_fast_risk_tracker = OutcomeEntryFastRiskTracker()
        self.entry_lifecycle_store = entry_lifecycle_store or (OutcomeEntryLifecycleStore(ledger.journal, ledger.run_id) if ledger else None)
        self.entry_requote_controller = entry_requote_controller or (
            OutcomeEntryRequoteController(account=self._account_reads, gateway=self.machine.gateway, store=self.entry_lifecycle_store, wallet=wallet)
            if self.entry_lifecycle_store else None
        )
        self.holding_path_recorder = OutcomeHoldingPathRecorder(ledger.journal, ledger.run_id) if ledger else None
        self.trend_continuation_recorder = OutcomeTrendContinuationRecorder(ledger.journal, ledger.run_id) if ledger else None
        self.reversal_classifier = OutcomeReversalClassifier()
        self.loss_reentry_gate = OutcomeLossReentryGate(ledger.journal, ledger.run_id) if ledger else None
        self.tier_b_execution_gate = OutcomeTierBExecutionGate(ledger.journal.db_path) if ledger else None
        self.portfolio_guard = OutcomePortfolioGuard(ledger.journal.db_path) if ledger else None
        # This observer is deliberately shadow-only.  It has no reference to
        # an execution controller and cannot alter S0/S2/S3 authority.
        self.market_regime_shadow = OutcomeMarketRegimeShadow()
        self.emergency_exit_policy = OutcomeEmergencyExitPolicy()
        self.emergency_exit_controller = (
            OutcomeEmergencyExitController(
                account=self._account_reads, gateway=self.machine.gateway, store=self.exit_lifecycle_store,
                wallet=wallet, policy=self.emergency_exit_policy,
            ) if self.exit_lifecycle_store else None
        )
        self._holding_context: dict[int, dict[str, object]] = {}
        self._opposite_observation_counts: dict[tuple[int, str], int] = {}
        # Emergency S3 requires three independently spaced confirmed samples.
        # This state is intentionally reset on restart, which is conservative:
        # a new process must observe the persistent reversal again.
        self._emergency_reversal_windows: dict[tuple[int, str], tuple[float, float, int]] = {}
        # E5 is deliberately process-local.  A restart must never resume a
        # canary against an old position without a new explicit operator run.
        self._e5_canary_eligible_order_ids: set[str] = set()
        self._tick_books: dict[tuple[int, int], dict[str, object]] = {}
        self._last_holding_path_capture_at: dict[tuple[int, str], float] = {}
        self._last_reversal_risk_observation_at: dict[tuple[int, str], float] = {}
        # Cache only immutable, exact-fill provenance.  A missing/ambiguous
        # lookup is intentionally retried on a later observation rather than
        # cached as a fabricated lifecycle identity.
        self._holding_entry_provenance: dict[tuple[int, str, str, str], dict[str, object]] = {}
        self._last_regime_shadow_record: dict[int, tuple[str, float]] = {}
        self._last_toxic_shadow_record: dict[tuple[int, str], tuple[str, float]] = {}

    @staticmethod
    def _audit_decimal(audit: dict[str, object], name: str) -> Decimal | None:
        try:
            value = audit.get(name)
            return Decimal(str(value)) if value is not None else None
        except (ArithmeticError, ValueError):
            return None

    def _entry_fast_risk_decision(
        self, *, market: OutcomeMarketSpec, lifecycle: OutcomeEntryLifecycle,
        current_side_index: int, desired_side_index: int | None, decision_reason: str,
    ) -> tuple[bool, str | None, dict[str, object]]:
        """Observe strong cancel-only risks; ordinary quote movement stays on the 5m lane."""
        key = (market.outcome_id, lifecycle.coin)
        reason: str | None = None
        evidence: dict[str, object] = {}
        status = self.stream_health.check(market) if self.stream_health is not None else None
        if status is None or not status.ready:
            reason = f"market_data_unhealthy:{status.reason if status is not None else 'not_configured'}"
        elif desired_side_index in (0, 1) and desired_side_index != current_side_index:
            reason = "confirmed_side_flip"
        elif desired_side_index not in (0, 1) and decision_reason in {
            "directional_confirmation_not_met", "selected_bid_in_no_trade_band",
        }:
            reason = "confirmed_signal_invalidation"
        else:
            snapshot = self.stream_health.fresh_book_top(market, lifecycle.coin) if self.stream_health else None
            audit = self.entry_lifecycle_store.submit_audit(
                order_id=lifecycle.order_id, coin=lifecycle.coin,
            ) if self.entry_lifecycle_store else None
            if snapshot is not None:
                bid = Decimal(str(snapshot["bid"]))
                depth = Decimal(str(snapshot["top3_bid_depth"]))
                evidence.update({"ws_bid": str(bid), "ws_ask": str(snapshot["ask"]),
                                 "ws_top3_bid_depth": str(depth)})
                drift_bps = max(Decimal("0"), (lifecycle.price - bid) / lifecycle.price * Decimal("10000"))
                drift_limit = self._audit_decimal(audit or {}, "entry_max_submit_drift_bps") or Decimal("25")
                evidence.update({"adverse_bid_drift_bps": str(drift_bps), "drift_limit_bps": str(drift_limit)})
                baseline_depth = self._audit_decimal(audit or {}, "entry_top3_depth_shares")
                submitted = self._audit_decimal(audit or {}, "entry_submitted_shares")
                depth_collapsed = bool(
                    baseline_depth is not None and baseline_depth > 0 and submitted is not None and submitted > 0
                    and depth < baseline_depth
                    and depth <= submitted * Decimal("1.25")
                )
                if drift_bps >= drift_limit:
                    reason = "adverse_bid_drift"
                elif depth_collapsed:
                    reason = "supporting_bid_depth_collapsed"
        observed = self.entry_fast_risk_tracker.observe(key=key, reason=reason, now=time.monotonic())
        evidence.update({
            "reason": observed.reason, "observation_count": observed.observation_count,
            "duration_sec": round(observed.duration_sec, 3), "confirmed": observed.confirmed,
        })
        if self.ledger is not None and reason is not None:
            self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_ENTRY_FAST_RISK", {
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
                "coin": lifecycle.coin, "order_id": lifecycle.order_id,
                "execution_submitted": False, **evidence,
            })
        return observed.confirmed, observed.reason, evidence

    @staticmethod
    def _context_decimal(context: dict[str, object], name: str) -> Decimal | None:
        try:
            value = context.get(name)
            return Decimal(str(value)) if value is not None else None
        except (ArithmeticError, ValueError):
            return None

    def _observe_market_regime_shadow(
        self, *, market: OutcomeMarketSpec, entry_side_index: int | None,
        entry_evidence: dict[str, object], market_context: dict[str, object] | None,
    ) -> dict[str, object]:
        """Classify the market for audit only; this never changes an order."""
        context = dict(market_context or {})
        continuation = entry_evidence.get("trend_continuation")
        continuation_data = continuation if isinstance(continuation, dict) else {}
        bid = self._context_decimal(context, "yes_best_bid")
        ask = self._context_decimal(context, "yes_best_ask")
        midpoint = (bid + ask) / Decimal("2") if bid is not None and ask is not None and bid < ask else None
        decision = self.market_regime_shadow.observe_market(OutcomeMarketRegimeInput(
            outcome_id=market.outcome_id, now_ts=time.time(), yes_midpoint=midpoint,
            spot_strike_bps=self._context_decimal(context, "spot_strike_bps"),
            mark_5m_bps=self._context_decimal(context, "mark_return_bps"),
            mark_15m_bps=self._context_decimal(continuation_data, "mark_15m_bps"),
            mark_60m_bps=self._context_decimal(continuation_data, "mark_60m_bps"),
            candidate_side_index=entry_side_index,
        ))
        payload = {
            "venue": "hyperliquid_outcome", "read_only": True,
            "outcome_id": market.outcome_id, "period": market.period,
            "state": decision.state, "reason": decision.reason,
            "candidate_side_index": entry_side_index,
            "yes_midpoint": str(midpoint) if midpoint is not None else None,
            "spot_strike_bps": str(self._context_decimal(context, "spot_strike_bps")) if self._context_decimal(context, "spot_strike_bps") is not None else None,
            "mark_5m_bps": str(self._context_decimal(context, "mark_return_bps")) if self._context_decimal(context, "mark_return_bps") is not None else None,
            "mark_15m_bps": str(self._context_decimal(continuation_data, "mark_15m_bps")) if self._context_decimal(continuation_data, "mark_15m_bps") is not None else None,
            "mark_60m_bps": str(self._context_decimal(continuation_data, "mark_60m_bps")) if self._context_decimal(continuation_data, "mark_60m_bps") is not None else None,
            "confirmed_crosses_2h": decision.confirmed_crosses_2h,
            "last_cross_at": decision.last_cross_at,
            "current_zone": decision.current_zone,
            "execution_submitted": False,
        }
        if self.ledger is not None:
            previous = self._last_regime_shadow_record.get(market.outcome_id)
            now = time.monotonic()
            # Persist state changes immediately and otherwise a compact row at
            # 30-second cadence.  The in-memory classifier still sees every
            # strategy tick, so rate limiting storage cannot change a state.
            if previous is None or previous[0] != str(decision.state) or now - previous[1] >= 30.0:
                self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_MARKET_REGIME_SHADOW", payload)
                self._last_regime_shadow_record[market.outcome_id] = (str(decision.state), now)
        return payload

    def _observe_toxic_fill_shadow(self, *, market: OutcomeMarketSpec, finding: object) -> None:
        """Record a short post-fill adverse-selection candidate without exiting."""
        if self.ledger is None or self.stream_health is None:
            return
        coin = str(getattr(finding, "coin", ""))
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        snapshot = self.stream_health.fresh_book_top(market, coin)
        if not coin or inventory <= 0 or vwap is None or snapshot is None:
            return
        provenance = self._resolve_holding_entry_provenance(
            market=market, coin=coin, inventory=inventory, fill_vwap=vwap,
        ) or {}
        try:
            filled_at = datetime.fromisoformat(str(provenance["entry_filled_at"])).timestamp()
            holding_age_sec = max(0.0, time.time() - filled_at)
            age_basis = "official_fill_timestamp"
        except (KeyError, TypeError, ValueError):
            return
        decision = self.market_regime_shadow.observe_toxic_fill(OutcomeToxicFillInput(
            outcome_id=market.outcome_id, coin=coin, now_ts=time.time(), holding_age_sec=holding_age_sec,
            fill_vwap=vwap, best_bid=Decimal(str(snapshot["bid"])),
            top3_bid_depth=Decimal(str(snapshot["top3_bid_depth"])),
        ))
        key, now = (market.outcome_id, coin), time.monotonic()
        previous = self._last_toxic_shadow_record.get(key)
        # During the two-minute window retain a compact ten-second path, plus
        # every state change.  No raw L2 levels are copied to the journal.
        if previous is not None and previous[0] == str(decision.state) and now - previous[1] < 10.0:
            return
        self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_TOXIC_FILL_SHADOW", {
            "venue": "hyperliquid_outcome", "read_only": True,
            "outcome_id": market.outcome_id, "period": market.period, "coin": coin,
            "state": decision.state, "reason": decision.reason,
            "holding_age_sec": round(holding_age_sec, 3), "age_basis": age_basis,
            "fill_vwap": str(vwap), "best_bid": str(snapshot["bid"]),
            "top3_bid_depth": str(snapshot["top3_bid_depth"]),
            "executable_return_pct": str(decision.executable_return_pct) if decision.executable_return_pct is not None else None,
            "bid_drift_bps": str(decision.bid_drift_bps) if decision.bid_drift_bps is not None else None,
            "depth_ratio": str(decision.depth_ratio) if decision.depth_ratio is not None else None,
            "observation_count": decision.observation_count,
            "duration_sec": round(decision.duration_sec, 3), "execution_submitted": False,
        })
        self._last_toxic_shadow_record[key] = (str(decision.state), now)

    def _continuation_entry_already_submitted(self, *, outcome_id: int) -> bool:
        """One continuation canary submit per daily market, including restarts."""
        if self.ledger is None:
            return True
        try:
            with sqlite3.connect(f"file:{self.ledger.journal.db_path}?mode=ro", uri=True) as conn:
                rows = conn.execute(
                    "SELECT payload_json FROM strategy_events "
                    "WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED' ORDER BY id DESC LIMIT 500"
                ).fetchall()
        except sqlite3.Error:
            return True
        for (raw,) in rows:
            try:
                payload = json.loads(raw or "{}")
                if int(payload.get("outcome_id")) == outcome_id and payload.get("entry_tier") == "tier_c_trend_continuation":
                    return True
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return False

    def _capture_trend_continuation_path(
        self, *, market: OutcomeMarketSpec, entry_evidence: dict[str, object], market_context: dict[str, object] | None,
    ) -> None:
        if self.trend_continuation_recorder is None:
            return
        candidate = entry_evidence.get("trend_continuation")
        context = market_context or {}
        try:
            bbo = {
                0: (context.get("yes_best_bid"), context.get("yes_best_ask")),
                1: (context.get("no_best_bid"), context.get("no_best_ask")),
            }
        except AttributeError:
            return
        self.trend_continuation_recorder.observe(
            outcome_id=market.outcome_id, period=market.period,
            candidate=candidate if isinstance(candidate, dict) else None,
            bbo_by_side=bbo,
        )

    def _begin_tick(self) -> None:
        self._account_reads.begin_tick()
        self._tick_books.clear()

    def _fresh_book_once(self, *, market: OutcomeMarketSpec, side_index: int) -> dict[str, object]:
        """One REST L2 snapshot per decision tick for observational logic.

        This cache is never passed through a mutation boundary.  Controllers
        independently refetch after cancel confirmation, preserving the
        post-only and price-protection contracts.
        """
        key = (market.outcome_id, side_index)
        book = self._tick_books.get(key)
        if book is None:
            book = self.machine.gateway.fetch_order_book(market=market, side_index=side_index)
            self._tick_books[key] = book
        return book

    @staticmethod
    def _top_of_book(book: dict[str, object]) -> tuple[Decimal, Decimal] | None:
        try:
            bid = Decimal(str(book["bids"][0]["price"]))  # type: ignore[index]
            ask = Decimal(str(book["asks"][0]["price"]))  # type: ignore[index]
            return (bid, ask) if Decimal("0") < bid < ask < Decimal("1") else None
        except (IndexError, KeyError, TypeError, ValueError, ArithmeticError):
            return None

    def _record(self, market: OutcomeMarketSpec, side_index: int, result: MakerTickResult) -> LiveExecutionResult:
        coin = self.machine.gateway.outcome_coin(market, side_index)
        if self.ledger:
            self.ledger.record_transition(market_id=market.outcome_id, coin=coin, result=result)
            fills = getattr(self.recovery.account, "get_user_fills_sync", lambda _user: [])(self.recovery.wallet)
            self.ledger.sync_fills(fills=fills, market_key=f"outcome:{market.outcome_id}", period=market.period)
        if result.state == "sell_placed" and self.exit_lifecycle_store and result.order_id and result.audit:
            try:
                self.exit_lifecycle_store.record(OutcomeExitLifecycle(
                    wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin, order_id=str(result.order_id),
                    inventory=Decimal(str(result.audit["inventory"])), target_price=Decimal(str(result.audit["requested_price"])),
                    replacement_count=0, state="SELL_RESTING",
                ), reason="initial_verified_alo_sell", extra={"pricing_basis": result.audit.get("pricing_basis"), "exit_mode": result.audit.get("exit_mode")})
                self._e5_canary_eligible_order_ids.add(str(result.order_id))
            except (KeyError, ValueError):
                pass
        if result.state == "buy_placed" and self.entry_lifecycle_store and result.order_id and result.audit:
            # Only an S0 schema-versioned decision can grant future entry
            # cancel/requote ownership.  P3 and manual orders remain outside
            # this controller's authority.
            try:
                if result.audit.get("entry_policy_schema_version") == 1 and result.audit.get("entry_policy_kind") in {
                    "s0_oi_spot_mark_confirmation", "s0_spot_mark_tier_b",
                }:
                    self.entry_lifecycle_store.record(OutcomeEntryLifecycle(
                        wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
                        order_id=str(result.order_id), price=Decimal(str(result.audit["entry_bid_at_decision"])),
                        replacement_count=0, state="BUY_RESTING",
                    ), reason="initial_audited_s0_alo_buy")
            except (KeyError, ValueError):
                pass
        return LiveExecutionResult(result.state, result.detail, result.order_id)

    @staticmethod
    def enabled() -> bool:
        return (
            os.environ.get("OUTCOME_AUTOMATED_EXECUTION_ENABLED") == "1"
            and os.environ.get("OUTCOME_SDK_EXECUTION_ENABLED") == "1"
        )

    @staticmethod
    def calibration_enabled() -> bool:
        return (
            OutcomeLiveExecutionRuntime.enabled()
            and os.environ.get("OUTCOME_P3_CALIBRATION_ENABLED") == "1"
        )

    @staticmethod
    def live_strategy_enabled() -> bool:
        """Separate opt-in for S0; P3 calibration never enables it."""
        return (
            OutcomeLiveExecutionRuntime.enabled()
            and os.environ.get("OUTCOME_LIVE_STRATEGY_ENABLED") == "1"
        )

    @staticmethod
    def exit_requote_enabled() -> bool:
        """E4's explicit fourth gate; false is the safe default."""
        return OutcomeLiveExecutionRuntime.enabled() and os.environ.get("OUTCOME_EXIT_REQUOTE_ENABLED") == "1"

    @staticmethod
    def exit_requote_canary_enabled() -> bool:
        """One explicit additional gate for an automated, single E5 replacement."""
        return OutcomeLiveExecutionRuntime.exit_requote_enabled() and os.environ.get("OUTCOME_EXIT_REQUOTE_CANARY_ENABLED") == "1"

    def _daily_calibration_entries(self) -> int:
        if not self.ledger:
            return 0
        with sqlite3.connect(self.ledger.journal.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM strategy_events WHERE event_type='OUTCOME_P3_CALIBRATION_ENTRY_PLACED' AND date(ts)=date('now')"
            ).fetchone()
        return int(row[0] or 0)

    def _persisted_entry_policy_evidence(self, *, market: OutcomeMarketSpec, coin: str) -> tuple[str, dict[str, object]] | None:
        """Load a verified entry policy, preferring strategy evidence.

        New S0 orders duplicate their policy decision on the matching
        ``ORDER_SUBMIT`` audit row.  This provides a recovery path if the
        process exits after the accepted order is journaled but before its
        follow-up strategy event commits.  When both records exist, their
        target and fee fields must agree; disagreement is fail-closed.
        """
        if self.ledger is None:
            return None
        try:
            with sqlite3.connect(self.ledger.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT ts, payload_json FROM strategy_events
                    WHERE event_type IN ('OUTCOME_P3_CALIBRATION_ENTRY_PLACED', 'OUTCOME_LIVE_STRATEGY_ENTRY_PLACED')
                      AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                      AND json_extract(payload_json, '$.coin')=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (market.outcome_id, coin),
                ).fetchone()
                if row:
                    timestamp, raw_payload = str(row[0]), row[1]
                    payload = json.loads(raw_payload)
                    if not isinstance(payload, dict):
                        return None
                    # Legacy/P3 entries may predate submit-audit evidence.
                    # For new S0 entries, verify the matching order record
                    # whenever it is present, without making old positions
                    # impossible to reconcile after deployment.
                    if str(payload.get("sampling_policy", "")) in {
                        "oi_spot_mark_confirmation", "spot_mark_tier_b",
                    }:
                        order_id = str(payload.get("order_id") or "")
                        if order_id:
                            audit_row = conn.execute(
                                """
                                SELECT payload_json FROM order_events
                                WHERE event_type='ORDER_SUBMIT' AND side='BUY' AND venue_order_id=?
                                ORDER BY id DESC LIMIT 1
                                """, (order_id,),
                            ).fetchone()
                            if audit_row:
                                order_payload = json.loads(audit_row[0] or "{}")
                                audit = order_payload.get("audit") if isinstance(order_payload, dict) else None
                                if isinstance(audit, dict) and audit.get("entry_policy_schema_version") == 1:
                                    for field in ("target_return_pct", "maker_close_fee_rate"):
                                        if str(audit.get(field)) != str(payload.get(field)):
                                            return None
                    return timestamp, payload

                # No strategy event: recover only from the dedicated S0
                # submit audit, scoped to the exact market/coin.  This is the
                # crash-window fallback, not a license to adopt manual buys.
                rows = conn.execute(
                    """
                    SELECT ts, payload_json FROM order_events
                    WHERE event_type='ORDER_SUBMIT' AND side='BUY' AND instrument_id=?
                      AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                    ORDER BY id DESC LIMIT 20
                    """, (coin, market.outcome_id),
                ).fetchall()
            for timestamp, raw_payload in rows:
                order_payload = json.loads(raw_payload or "{}")
                audit = order_payload.get("audit") if isinstance(order_payload, dict) else None
                if (
                    isinstance(audit, dict)
                    and audit.get("entry_policy_schema_version") == 1
                    and audit.get("entry_policy_kind") in {"s0_oi_spot_mark_confirmation", "s0_spot_mark_tier_b"}
                ):
                    return str(timestamp), audit
            return None
        except (KeyError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError):
            return None

    def _persisted_p3_exit_policy(self, *, market: OutcomeMarketSpec, coin: str) -> OutcomeP3CalibrationConfig | None:
        """Recover the policy that created a still-managed P3/S0 inventory."""
        evidence = self._persisted_entry_policy_evidence(market=market, coin=coin)
        if evidence is None:
            return None
        _, payload = evidence
        try:
            return OutcomeP3CalibrationConfig(
                max_daily_entries=1,
                target_return_pct=Decimal(str(payload["target_return_pct"])),
                loss_reprice_pct=Decimal(str(payload["loss_reprice_pct"])),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _persisted_p3_maker_fee(self, *, market: OutcomeMarketSpec, coin: str) -> Decimal | None:
        evidence = self._persisted_entry_policy_evidence(market=market, coin=coin)
        if evidence is None:
            return None
        try:
            _, payload = evidence
            raw_fee = payload.get("maker_close_fee_rate")
            fee = Decimal(str(raw_fee)) if raw_fee is not None else None
            return fee if fee is not None and Decimal("0") <= fee < Decimal("1") else None
        except (ArithmeticError, ValueError):
            return None

    def _strategy_exit_tier(self, *, market: OutcomeMarketSpec, coin: str) -> tuple[Decimal, Decimal | None] | None:
        """Return the elapsed-time S0 target and any permitted loss-band floor.

        The strategy starts with a strict +5% fee-after target.  It may narrow
        to +3% after the first configured age and +2% after the second.  Only
        at that final tier may a weak book use the fee-inclusive break-even
        floor.  This prevents a one-tick adverse quote from replacing the
        initial take-profit order with a near-cost sell.
        """
        evidence = self._persisted_entry_policy_evidence(market=market, coin=coin)
        if evidence is None:
            return None
        try:
            timestamp, payload = evidence
            # Pre-submit-audit S0 records used the strategy event type and
            # tier fields but did not yet carry ``sampling_policy``.  The
            # presence of the full tier contract identifies that legacy S0
            # shape without mistaking a P3 calibration record for S0.
            is_s0 = (
                str(payload.get("entry_policy_kind", "")) in {"s0_oi_spot_mark_confirmation", "s0_spot_mark_tier_b"}
                or str(payload.get("sampling_policy", "")) in {"oi_spot_mark_confirmation", "spot_mark_tier_b"}
                or all(key in payload for key in ("narrow_after_sec", "narrow_return_pct", "floor_after_sec", "floor_return_pct"))
            )
            if not is_s0:
                return None
            target = Decimal(str(payload["target_return_pct"]))
            narrow_after = float(payload["narrow_after_sec"])
            narrow = Decimal(str(payload["narrow_return_pct"]))
            floor_after = float(payload["floor_after_sec"])
            floor = Decimal(str(payload["floor_return_pct"]))
            # Age is anchored to the original entry evidence, not the latest
            # replacement timestamp; otherwise each rebook would silently
            # postpone the +3%/+2% time tiers.
            age = max(0.0, time.time() - datetime.fromisoformat(timestamp).timestamp())
            if age >= floor_after:
                # The loss band is only eligible after the two-hour floor
                # tier.  It is a fee-inclusive -5% passive quote, never an
                # immediate stop or a taker instruction.
                return floor, Decimal("0.05")
            if age >= narrow_after:
                return narrow, None
            return target, None
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None

    def _record_entry_gate_decision(self, *, market: OutcomeMarketSpec, entry_side_index: int | None,
                                    entry_reason: str, entry_evidence: dict[str, object],
                                    active: list[object]) -> None:
        """Persist the small S0 decision record needed for gate ablation.

        This is intentionally one event per live-strategy tick, not a raw
        WebSocket dump.  It records why an entry was or was not actionable,
        including the independent account/order blocker.
        """
        if self.ledger is None:
            return
        self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_ENTRY_GATE_DECISION", {
            "venue": "hyperliquid_outcome", "read_only": True,
            "outcome_id": market.outcome_id, "period": market.period,
            "entry_side_index": entry_side_index, "entry_reason": entry_reason,
            "entry_evidence": entry_evidence,
            "active_account_states": [
                {"coin": str(getattr(item, "coin", "")), "state": str(getattr(item, "state", "")),
                 "inventory": str(getattr(item, "inventory", "0")),
                 "buy_order_ids": list(getattr(item, "buy_order_ids", ())),
                 "sell_order_ids": list(getattr(item, "sell_order_ids", ()))}
                for item in active
            ],
            "execution_submitted": False,
        })

    def _record_entry_admission_decision(self, *, market: OutcomeMarketSpec,
                                         entry_side_index: int | None, entry_reason: str,
                                         entry_evidence: dict[str, object],
                                         admission: dict[str, object],
                                         result: LiveExecutionResult) -> None:
        """Persist the final S0 admission result, after every venue-side gate.

        ``OUTCOME_ENTRY_GATE_DECISION`` describes the upstream signal only.
        This record answers the operationally distinct question: did that
        signal reach a live ALO submit, and if not, precisely which later
        guard stopped it?  It is journal-only and never changes an order.
        """
        if self.ledger is None:
            return
        stream: dict[str, object]
        if self.stream_health is None:
            stream = {"ready": False, "reason": "ws_health_not_configured"}
        else:
            status = self.stream_health.check(market)
            stream = {"ready": status.ready, "reason": status.reason}
        self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_ENTRY_ADMISSION_DECISION", {
            "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
            "period": market.period, "read_only": True,
            "raw_signal_side_index": entry_side_index, "raw_signal_reason": entry_reason,
            "raw_signal_evidence": entry_evidence,
            "admission_inputs": admission,
            "stream_at_completion": stream,
            "final_state": result.state, "final_reason": result.detail,
            "execution_submitted": result.state == "buy_placed",
            "order_id": result.order_id,
        })

    def _maybe_requote_entry_buy(self, *, market: OutcomeMarketSpec, finding: object,
                                 entry_side_index: int | None, entry_reason: str,
                                 config: OutcomeLiveStrategyConfig) -> LiveExecutionResult | None:
        """Cancel one stale, auditable entry buy; the next tick re-evaluates/rebooks.

        No generic order is adopted.  This is deliberately one exchange
        mutation per tick, so a successful cancel is confirmed before a later
        fresh S0/risk/book evaluation can create the replacement.
        """
        if not self.entry_lifecycle_store or not self.entry_requote_controller:
            return None
        if Decimal(str(getattr(finding, "inventory", "0"))) != 0 or str(getattr(finding, "state", "")) != "buy_resting":
            return None
        buy_order_ids = tuple(getattr(finding, "buy_order_ids", ()))
        coin = str(getattr(finding, "coin", ""))
        if len(buy_order_ids) != 1 or coin not in {market.yes_coin, market.no_coin}:
            return LiveExecutionResult("blocked", "entry requote requires exactly one current-market buy order")
        side_index = 0 if coin == market.yes_coin else 1
        lifecycle = self.entry_lifecycle_store.recover_or_adopt_audited_submit(
            wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            open_orders=self.recovery.account.get_open_orders_sync(self.recovery.wallet),
        )
        if lifecycle is None or lifecycle.order_id != str(buy_order_ids[0]):
            return LiveExecutionResult("blocked", "entry requote refuses unrecorded buy ownership", str(buy_order_ids[0]))
        age = None if lifecycle.updated_at_ts is None else max(0.0, time.time() - lifecycle.updated_at_ts)
        fast_confirmed, fast_reason, fast_evidence = self._entry_fast_risk_decision(
            market=market, lifecycle=lifecycle, current_side_index=side_index,
            desired_side_index=entry_side_index, decision_reason=entry_reason,
        )
        # Ordinary quote maintenance retains the five-minute queue-preserving
        # interval.  Do not spend a fresh REST L2 request for that path.  The
        # separately confirmed risk path is the only early cancel authority.
        interval_plan = self.entry_planner.plan(EntryQuoteInput(
            current_side_index=side_index, existing_price=lifecycle.price,
            desired_side_index=None, desired_bid=None, decision_reason=entry_reason,
            order_age_sec=age, fast_risk_confirmed=fast_confirmed,
            fast_risk_reason=fast_reason,
        ))
        if interval_plan.action is EntryQuoteAction.CANCEL:
            result = self.entry_requote_controller.execute_cancel(
                market=market, side_index=side_index, lifecycle=lifecycle, plan=interval_plan,
            )
            if self.ledger:
                self.ledger.journal.log_order_event(
                    self.ledger.run_id, "ORDER_CANCEL", venue_order_id=lifecycle.order_id, side="BUY",
                    status="CANCELLED" if result.state == "cancelled" else "RECONCILE_REQUIRED",
                    instrument_id=coin, reason=result.detail,
                    payload={"venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
                             "coin": coin, "entry_requote_reason": interval_plan.reason,
                             "fast_risk": fast_evidence,
                             "execution_submitted": result.state == "cancelled"},
                )
            return LiveExecutionResult(result.state, result.detail, result.old_order_id)
        if interval_plan.action is EntryQuoteAction.KEEP and interval_plan.reason == "entry_requote_interval_not_elapsed":
            return LiveExecutionResult("buy_resting", f"entry requote keep: {interval_plan.reason}", lifecycle.order_id)
        desired_side, desired_bid, decision_reason = entry_side_index, None, entry_reason
        if desired_side in (0, 1):
            try:
                book = self.machine.gateway.fetch_order_book(market=market, side_index=desired_side)
                desired_bid = Decimal(str(book["bids"][0]["price"]))
                if desired_bid < config.min_entry_price:
                    desired_side, desired_bid, decision_reason = None, None, "selected_bid_in_no_trade_band"
            except (IndexError, KeyError, TypeError, ValueError):
                desired_bid = None
        plan = self.entry_planner.plan(EntryQuoteInput(
            current_side_index=side_index, existing_price=lifecycle.price,
            desired_side_index=desired_side, desired_bid=desired_bid,
            decision_reason=decision_reason, order_age_sec=age,
        ))
        if plan.action is EntryQuoteAction.KEEP:
            return LiveExecutionResult("buy_resting", f"entry requote keep: {plan.reason}", lifecycle.order_id)
        if plan.action is EntryQuoteAction.BLOCK:
            return LiveExecutionResult("blocked", f"entry requote blocked: {plan.reason}", lifecycle.order_id)
        result = self.entry_requote_controller.execute_cancel(
            market=market, side_index=side_index, lifecycle=lifecycle, plan=plan,
        )
        if self.ledger:
            self.ledger.journal.log_order_event(
                self.ledger.run_id, "ORDER_CANCEL", venue_order_id=lifecycle.order_id, side="BUY",
                status="CANCELLED" if result.state == "cancelled" else "RECONCILE_REQUIRED",
                instrument_id=coin, reason=result.detail,
                payload={"venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
                         "coin": coin, "entry_requote_reason": plan.reason,
                         "execution_submitted": result.state == "cancelled"},
            )
        return LiveExecutionResult(result.state, result.detail, result.old_order_id)

    def _cancel_filled_entry_and_place_protection(self, *, market: OutcomeMarketSpec,
                                                   finding: object) -> LiveExecutionResult | None:
        """Close an owned buy remainder and create its first sell in this call.

        This is intentionally outside the WebSocket new-entry gate.  The
        position is already real account inventory, and the state machine uses
        a fresh REST L2 book for the protective ALO sell after cancellation is
        confirmed.  Waiting for a later global strategy tick left a measured
        multi-minute unprotected window when synchronous account/settlement
        work delayed that loop.
        """
        if not self.entry_lifecycle_store or not self.entry_requote_controller:
            return None
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        buy_order_ids = tuple(getattr(finding, "buy_order_ids", ()))
        sell_order_ids = tuple(getattr(finding, "sell_order_ids", ()))
        coin = str(getattr(finding, "coin", ""))
        if inventory <= 0 or len(buy_order_ids) != 1 or sell_order_ids or coin not in {market.yes_coin, market.no_coin}:
            return None
        side_index = 0 if coin == market.yes_coin else 1
        lifecycle = self.entry_lifecycle_store.recover_or_adopt_audited_submit(
            wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            open_orders=self.recovery.account.get_open_orders_sync(self.recovery.wallet),
        )
        if lifecycle is None or lifecycle.order_id != str(buy_order_ids[0]):
            return LiveExecutionResult("blocked", "filled entry refuses unrecorded buy ownership", str(buy_order_ids[0]))
        cancel = self.entry_requote_controller.execute_cancel_after_fill(
            market=market, side_index=side_index, lifecycle=lifecycle,
        )
        if self.ledger:
            self.ledger.journal.log_order_event(
                self.ledger.run_id, "ORDER_CANCEL", venue_order_id=lifecycle.order_id, side="BUY",
                status="CANCELLED" if cancel.state in {"cancelled_after_fill", "flat"} else "RECONCILE_REQUIRED",
                instrument_id=coin, reason=cancel.detail,
                payload={
                    "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "coin": coin,
                    "filled_entry_cleanup": True, "execution_submitted": cancel.state in {"cancelled_after_fill", "flat"},
                },
            )
        if cancel.state != "cancelled_after_fill":
            return LiveExecutionResult(cancel.state, cancel.detail, cancel.old_order_id)
        # Reconcile once more after confirmed cancellation.  A cancel/fill
        # race may alter the inventory; only the refreshed account truth is
        # permitted to size the sell.
        refreshed = self.recovery.reconcile([market])
        current = next((item for item in refreshed.findings if item.coin == coin), None)
        if current is None or Decimal(str(current.inventory)) <= 0:
            return LiveExecutionResult("flat", "filled entry cancel confirmed and inventory is now flat", cancel.old_order_id)
        if tuple(current.buy_order_ids) or tuple(current.sell_order_ids):
            return LiveExecutionResult("blocked", "filled entry cancel reconciliation found remaining conflicting order", cancel.old_order_id)
        protective = self._advance_persisted_p3_exit(market=market, finding=current)
        if protective is None:
            return LiveExecutionResult("blocked", "filled entry has no persisted verified exit policy", cancel.old_order_id)
        return protective

    def _maybe_requote_p3_exit(self, *, market: OutcomeMarketSpec, finding: object) -> LiveExecutionResult | None:
        """E4 integration, disabled unless all execution gates include reprice.

        It only manages a sell lifecycle recorded by this runtime after E4;
        manual and legacy orders cannot be adopted merely because they appear
        in the configured wallet.
        """
        if not self.exit_requote_enabled() or not self.exit_lifecycle_store or not self.exit_requote_controller:
            return None
        # A resting entry or a just-filled position has no protective sell yet.
        # It must continue to the normal state machine, which is responsible
        # for creating that first ALO sell.  Treating every non-flat recovery
        # finding as an exit caused E4 to block this essential transition with
        # the misleading "unrecorded sell ownership" message.
        if not tuple(getattr(finding, "sell_order_ids", ())):
            return None
        coin = str(getattr(finding, "coin", ""))
        policy = self._persisted_p3_exit_policy(market=market, coin=coin)
        fee = self._persisted_p3_maker_fee(market=market, coin=coin)
        if policy is None or fee is None:
            return LiveExecutionResult("blocked", "exit reprice requires persisted verified P3 policy")
        lifecycle = self.exit_lifecycle_store.recover(wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin)
        if lifecycle is None:
            return LiveExecutionResult("blocked", "exit reprice refuses unrecorded sell ownership")
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        if vwap is None:
            return LiveExecutionResult("blocked", "exit reprice cannot verify fill VWAP", lifecycle.order_id)
        side_index = 0 if coin == market.yes_coin else 1
        try:
            ws_bbo = self.stream_health.fresh_bbo(market, coin) if self.stream_health else None
            if ws_bbo is not None:
                bid, ask = ws_bbo
            else:
                top = self._top_of_book(self._fresh_book_once(market=market, side_index=side_index))
                if top is None:
                    raise ValueError("invalid_book")
                bid, ask = top
        except (IndexError, KeyError, TypeError, ValueError):
            return LiveExecutionResult("blocked", "exit reprice book unavailable", lifecycle.order_id)

        def _as_decimal(name: str) -> Decimal | None:
            try:
                value = self._holding_context.get(market.outcome_id, {}).get(name)
                return Decimal(str(value)) if value is not None else None
            except (ValueError, ArithmeticError):
                return None

        raw_context = self._holding_context.get(market.outcome_id, {})
        try:
            oi_age_ms = int(raw_context.get("oi_age_ms"))
        except (TypeError, ValueError):
            oi_age_ms = -1
        context_fresh = 0 <= oi_age_ms <= 90_000
        reversal = self.reversal_classifier.classify(OutcomeReversalInput(
            side_index, vwap, bid, ask,
            _as_decimal("spot_strike_bps"), _as_decimal("mark_return_bps"), _as_decimal("oi_return_bps"),
            context_fresh, self._opposite_observation_counts.get((market.outcome_id, coin), 0),
        ))
        # S2/S3's same classifier is now also the authoritative loss-band
        # thesis gate.  It requires a fresh book, a -5% price breach (in the
        # planner), and three current, independent opposing observations.
        # If current spot/mark confirmation returns to the held side, its
        # observation count is reset by _capture_holding_path and this stays
        # false, which restores the normal elapsed-time +2% passive target.
        loss_band_authorized = reversal.state is OutcomeReversalState.REVERSAL_CONFIRMED
        # Persist a deliberately small decision-time signal snapshot with a
        # replacement.  The full market context is already represented by
        # compact entry/holding research rows; copying arbitrary raw payloads
        # here would recreate the DB-growth problem this runtime avoids.
        current_signal = {
            key: raw_context.get(key)
            for key in (
                "signal", "score", "spot_strike_bps", "mark_return_bps",
                "oi_return_bps", "oi_age_ms", "entry_tier", "reason",
            )
            if key in raw_context
        }
        replacement_context = {
            "trigger_bbo": {"best_bid": str(bid), "best_ask": str(ask), "timestamp_ms": int(time.time() * 1000)},
            "current_signal": {
                **current_signal, "reversal_state": str(reversal.state), "reversal_reason": reversal.reason,
                "opposite_observation_count": self._opposite_observation_counts.get((market.outcome_id, coin), 0),
                "loss_band_authorized": loss_band_authorized,
            },
        }
        if (
            self.exit_requote_canary_enabled()
            and lifecycle.order_id in self._e5_canary_eligible_order_ids
            and lifecycle.replacement_count == 0
        ):
            min_age = max(1.0, float(os.environ.get("OUTCOME_EXIT_REQUOTE_CANARY_MIN_AGE_SEC", "15")))
            if lifecycle.updated_at_ts is not None and time.time() - lifecycle.updated_at_ts >= min_age:
                # This is deliberately not a strategy repricing decision:
                # one tick upward preserves/raises the original target and
                # lets E2 prove cancel-confirm-rebook-ALO end to end.
                canary_plan = ExitQuotePlan(
                    ExitQuoteAction.CANCEL_REPLACE, "e5_one_tick_upward_canary",
                    lifecycle.target_price + self.exit_planner.config.tick_size,
                    lifecycle.target_price, inventory, "e5_canary",
                )
                result = self.exit_requote_controller.execute(
                    market=market, side_index=side_index, lifecycle=lifecycle, plan=canary_plan,
                    replacement_context=replacement_context,
                )
                self._e5_canary_eligible_order_ids.discard(lifecycle.order_id)
                return LiveExecutionResult(result.state, result.detail, result.new_order_id or result.old_order_id)
        strategy_tier = self._strategy_exit_tier(market=market, coin=coin)
        minimum_return_pct, loss_reprice_pct = strategy_tier or (policy.target_return_pct, policy.loss_reprice_pct)
        plan = self.exit_planner.plan(ExitQuoteInput(
            inventory=inventory, fill_vwap=vwap, maker_close_fee_rate=fee,
            minimum_return_pct=minimum_return_pct, loss_reprice_pct=loss_reprice_pct,
            existing_order_id=lifecycle.order_id, existing_price=lifecycle.target_price,
            best_bid=bid, best_ask=ask, book_age_sec=0.0, now_ts=time.time(),
            last_requote_ts=lifecycle.updated_at_ts, replacement_count=lifecycle.replacement_count,
            loss_band_authorized=loss_band_authorized,
        ))
        if plan.action.value == "KEEP":
            if plan.exit_mode == "loss_band" and lifecycle.state == "LOSS_BAND_RESTING":
                self.exit_lifecycle_store.record(
                    lifecycle, reason="loss_band_unfilled_passive_quote", extra={"state": "LOSS_BAND_UNFILLED"},
                )
            return LiveExecutionResult("sell_resting", f"exit reprice keep: {plan.reason}", lifecycle.order_id)
        if plan.action.value == "BLOCK":
            return LiveExecutionResult("blocked", f"exit reprice blocked: {plan.reason}", lifecycle.order_id)
        result = self.exit_requote_controller.execute(
            market=market, side_index=side_index, lifecycle=lifecycle, plan=plan,
            replacement_context=replacement_context,
        )
        return LiveExecutionResult(result.state, result.detail, result.new_order_id or result.old_order_id)

    def _advance_persisted_p3_exit(self, *, market: OutcomeMarketSpec, finding: object) -> LiveExecutionResult | None:
        coin = str(getattr(finding, "coin", ""))
        policy = self._persisted_p3_exit_policy(market=market, coin=coin)
        fee = self._persisted_p3_maker_fee(market=market, coin=coin)
        if policy is None or fee is None:
            return None
        side_index = 0 if coin == market.yes_coin else 1
        strategy_tier = self._strategy_exit_tier(market=market, coin=coin)
        # A newly-filled S0 order has no lifecycle yet, so this is the one
        # place that creates its first protective sell.  Do not pass the
        # break-even loss band before the final time tier: the first order
        # must be the configured +5% target, and the 1h tier remains +3%.
        minimum_return_pct, loss_reprice_pct = strategy_tier or (
            policy.target_return_pct,
            policy.loss_reprice_pct if self.exit_requote_enabled() else None,
        )
        result = self.machine.tick(
            market=market, side_index=side_index, entry_permitted=False,
            minimum_return_pct=minimum_return_pct, maker_close_fee_rate=fee,
            # The former one-shot loss cancellation is itself a dynamic
            # cancel/replace behavior.  E4 keeps it dormant until the new
            # confirmation/rebook controller is explicitly enabled.
            loss_reprice_pct=loss_reprice_pct,
        )
        return self._record(market, side_index, result)

    def tick(self, *, market: OutcomeMarketSpec, side_index: int, entry_permitted: bool) -> LiveExecutionResult:
        self._begin_tick()
        if not self.enabled():
            return LiveExecutionResult("disabled", "automated execution requires OUTCOME_AUTOMATED_EXECUTION_ENABLED=1 and OUTCOME_SDK_EXECUTION_ENABLED=1")
        if entry_permitted:
            return LiveExecutionResult("blocked", "generic live entry has no explicit verified exit policy; use a dedicated policy runtime")
        if entry_permitted:
            health_error = self._stream_ready(market)
            if health_error:
                return health_error
        report = self.recovery.reconcile([market])
        if not report.safe_for_new_entry:
            return LiveExecutionResult("blocked", f"account recovery blocked execution: {report.reason}")
        selected_coin = self.machine.gateway.outcome_coin(market, side_index)
        other_live = [finding for finding in report.findings if finding.coin != selected_coin and finding.state != "flat"]
        if other_live:
            return LiveExecutionResult("blocked", "other side of this Outcome already has live inventory or order")
        result: MakerTickResult = self.machine.tick(market=market, side_index=side_index, entry_permitted=entry_permitted)
        return self._record(market, side_index, result)

    def _stream_ready(self, market: OutcomeMarketSpec) -> LiveExecutionResult | None:
        if self.stream_health is None:
            return LiveExecutionResult("blocked", "market-data gate: ws_health_not_configured")
        stream = self.stream_health.check(market)
        if not stream.ready:
            return LiveExecutionResult("blocked", f"market-data gate: {stream.reason}")
        return None

    def _reconcile_exit_lifecycles(self, *, market: OutcomeMarketSpec, report: object) -> None:
        """Close durable sell ownership only from fresh account truth.

        This is read-only against the exchange.  It prevents a filled sell
        from remaining forever as ``SELL_RESTING`` in the journal and never
        adopts a manual order.
        """
        if self.exit_lifecycle_store is None:
            return
        try:
            open_orders = self.recovery.account.get_open_orders_sync(self.recovery.wallet)
            for finding in getattr(report, "findings", ()):
                if int(getattr(finding, "market_id", -1)) != market.outcome_id:
                    # Retiring markets are reconciled to block overlap; their
                    # exit lifecycle is never mutated through the new market.
                    continue
                previous = self.exit_lifecycle_store.recover(
                    wallet=self.recovery.wallet, outcome_id=market.outcome_id,
                    coin=str(getattr(finding, "coin", "")),
                )
                self.exit_lifecycle_store.reconcile_owned_sell(
                    wallet=self.recovery.wallet, outcome_id=market.outcome_id,
                    coin=str(getattr(finding, "coin", "")),
                    inventory=Decimal(str(getattr(finding, "inventory", "0"))), open_orders=open_orders,
                )
                inventory = Decimal(str(getattr(finding, "inventory", "0")))
                if (inventory <= 0 and previous is not None
                        and not any(str(row.get("oid")) == previous.order_id for row in open_orders)
                        and previous.state in {
                            "LOSS_BAND_RESTING", "LOSS_BAND_UNFILLED", "REVERSAL_CONFIRMED",
                            "EMERGENCY_EXIT_SUBMITTED",
                        }
                        and self.loss_reentry_gate is not None):
                    self.loss_reentry_gate.record_confirmed_loss_exit(
                        outcome_id=market.outcome_id, period=market.period,
                        coin=previous.coin, order_id=previous.order_id,
                    )
        except Exception:
            # The normal recovery report remains the execution gate.  Do not
            # infer a close from a failed read.
            return

    def _entry_fill_visibility_barrier(
        self, *, market: OutcomeMarketSpec, report: object, admission: dict[str, object],
    ) -> LiveExecutionResult | None:
        """Fail closed when an official entry fill precedes account visibility.

        Hyperliquid's user-fill feed can report a maker fill seconds before
        balances or open orders reflect its new inventory.  Treating that
        transient empty snapshot as flat used to permit a second BUY.  A
        durable official fill now blocks admission until account truth shows
        the inventory (or its protective sell); it is never used to infer a
        sell size.
        """
        if self.entry_lifecycle_store is None:
            return None
        findings = tuple(getattr(report, "findings", ()))
        for coin in (market.yes_coin, market.no_coin):
            lifecycle = self.entry_lifecycle_store.recover(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            )
            if lifecycle is None:
                continue
            finding = next((item for item in findings if getattr(item, "market_id", None) == market.outcome_id
                            and str(getattr(item, "coin", "")) == coin), None)
            if finding is None:
                continue
            inventory = Decimal(str(getattr(finding, "inventory", "0")))
            buy_ids = {str(value) for value in getattr(finding, "buy_order_ids", ())}
            sell_ids = tuple(getattr(finding, "sell_order_ids", ()))
            # Once the account confirms either the inventory or a covering
            # sell, the fill is reconciled and cannot block a later, flat
            # round trip.
            if inventory > 0 or sell_ids:
                if lifecycle.state in {"BUY_RESTING", "CANCEL_SUBMITTED", "FILL_PENDING_RECONCILIATION", "RECONCILE_REQUIRED"}:
                    self.entry_lifecycle_store.record(
                        lifecycle, reason="entry_account_visibility_reconciled",
                        extra={"state": "FILL_RECONCILED", "account_inventory": str(inventory),
                               "sell_order_ids": list(sell_ids)},
                    )
                continue
            if lifecycle.order_id in buy_ids:
                continue
            fill = self.entry_lifecycle_store.official_buy_fill(
                outcome_id=market.outcome_id, coin=coin, order_id=lifecycle.order_id,
            )
            if fill is not None:
                if lifecycle.state != "FILL_PENDING_RECONCILIATION":
                    self.entry_lifecycle_store.record(
                        lifecycle, reason="official_buy_fill_before_account_visibility",
                        extra={"state": "FILL_PENDING_RECONCILIATION", "official_fill": fill},
                    )
                admission["entry_fill_visibility_gate"] = {
                    "allowed": False, "reason": "official_buy_fill_pending_account_reconciliation",
                    "coin": coin, "order_id": lifecycle.order_id, "official_fill": fill,
                }
                self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_ENTRY_FILL_VISIBILITY_LAG_BLOCK", {
                    "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period,
                    "coin": coin, "order_id": lifecycle.order_id, "official_fill": fill,
                    "action": "new_buy_refused_until_account_inventory_or_protective_sell_visible",
                })
                return LiveExecutionResult("blocked", "official buy fill pending account reconciliation", lifecycle.order_id)
            # A durably owned entry disappearing without cancellation or fill
            # evidence is likewise unsafe to overwrite with another BUY.
            if lifecycle.state != "RECONCILE_REQUIRED":
                self.entry_lifecycle_store.record(
                    lifecycle, reason="owned_entry_absent_without_terminal_evidence",
                    extra={"state": "RECONCILE_REQUIRED"},
                )
            admission["entry_fill_visibility_gate"] = {
                "allowed": False, "reason": "owned_entry_absent_without_terminal_evidence",
                "coin": coin, "order_id": lifecycle.order_id,
            }
            return LiveExecutionResult("blocked", "owned entry absent without terminal reconciliation evidence", lifecycle.order_id)
        return None

    def _resolve_holding_entry_provenance(
        self, *, market: OutcomeMarketSpec, coin: str, inventory: Decimal, fill_vwap: Decimal,
    ) -> dict[str, object] | None:
        """Bind an open holding only to its exact official BUY fill.

        The report must never accidentally combine two same-coin round trips.
        Current live safety permits one inventory, but partial/multi-fill
        history can still be ambiguous after a restart; such a path remains
        unlabelled rather than being assigned to the latest strategy event.
        """
        if self.ledger is None:
            return None
        key = (market.outcome_id, coin, str(inventory), str(fill_vwap))
        cached = self._holding_entry_provenance.get(key)
        if cached is not None:
            return cached
        try:
            with sqlite3.connect(self.ledger.journal.db_path) as conn:
                row = conn.execute(
                    """SELECT id, ts, venue_order_id, price, qty, payload_json
                       FROM order_events
                       WHERE event_type='ORDER_FILLED' AND side='BUY' AND instrument_id=?
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=?
                         AND json_extract(payload_json, '$.actual_fill')=1
                       ORDER BY id DESC LIMIT 1""",
                    (coin, market.outcome_id, coin),
                ).fetchone()
                if row is None:
                    return None
                _, filled_at, order_id, price, quantity, fill_raw = row
                if Decimal(str(quantity)) != inventory or Decimal(str(price)) != fill_vwap:
                    return None
                entry_row = conn.execute(
                    """SELECT payload_json FROM strategy_events
                       WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED'
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=?
                         AND json_extract(payload_json, '$.order_id')=?
                       ORDER BY id DESC LIMIT 1""",
                    (market.outcome_id, coin, str(order_id)),
                ).fetchone()
            fill_payload = json.loads(fill_raw or "{}")
            entry_payload = json.loads(entry_row[0] or "{}") if entry_row else {}
            if not isinstance(fill_payload, dict) or not isinstance(entry_payload, dict):
                return None
            trade_id = str(fill_payload.get("trade_id") or "")
            if not trade_id:
                return None
            entry_side_index = int(entry_payload.get("side_index"))
            entry_tier = str(entry_payload.get("entry_tier") or "unknown")
            target_return = entry_payload.get("target_return_pct")
            filled_epoch = datetime.fromisoformat(str(filled_at)).timestamp()
            provenance = {
                "entry_lifecycle_id": f"official_buy:{order_id}:{trade_id}",
                "entry_order_id": str(order_id), "entry_trade_id": trade_id,
                "entry_filled_at": str(filled_at), "entry_side_index": entry_side_index,
                "entry_tier": entry_tier,
                "entry_target_return_pct": str(target_return) if target_return is not None else None,
                "entry_time_left_sec": max(0.0, float(market.expiry_timestamp) - filled_epoch),
            }
            self._holding_entry_provenance[key] = provenance
            return provenance
        except (TypeError, ValueError, ArithmeticError, sqlite3.Error, json.JSONDecodeError):
            return None

    def _update_holding_reversal(
        self, *, market: OutcomeMarketSpec, coin: str, side_index: int,
        vwap: Decimal, bid: Decimal, ask: Decimal, evidence: dict[str, object], book_source: str,
    ) -> None:
        def _decimal(name: str) -> Decimal | None:
            try:
                value = evidence.get(name)
                return Decimal(str(value)) if value is not None else None
            except (ValueError, ArithmeticError):
                return None

        spot_bps = _decimal("spot_strike_bps")
        mark_bps = _decimal("mark_return_bps")
        oi_bps = _decimal("oi_return_bps")
        opposite = False
        if spot_bps is not None and mark_bps is not None and oi_bps is not None:
            direction = Decimal("1") if side_index == 0 else Decimal("-1")
            opposite = direction * spot_bps < 0 and direction * mark_bps < 0 and oi_bps > 0
        key = (market.outcome_id, coin)
        self._opposite_observation_counts[key] = self._opposite_observation_counts.get(key, 0) + 1 if opposite else 0
        try:
            oi_age_ms = int(evidence.get("oi_age_ms"))
        except (TypeError, ValueError):
            oi_age_ms = -1
        context_fresh = 0 <= oi_age_ms <= 90_000
        decision = self.reversal_classifier.classify(OutcomeReversalInput(
            side_index, vwap, bid, ask, spot_bps, mark_bps, oi_bps, context_fresh,
            self._opposite_observation_counts[key],
        ))
        now = time.time()
        if decision.state.value == "REVERSAL_CONFIRMED":
            first_ts, last_independent_ts, count = self._emergency_reversal_windows.get(key, (now, 0.0, 0))
            if last_independent_ts <= 0 or now - last_independent_ts >= 60.0:
                count += 1
                last_independent_ts = now
            self._emergency_reversal_windows[key] = (first_ts, last_independent_ts, count)
        else:
            self._emergency_reversal_windows.pop(key, None)
        if self.ledger is not None:
            self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_REVERSAL_SHADOW_DECISION", {
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period,
                "coin": coin, "state": decision.state, "reason": decision.reason,
                "book_source": book_source, "oi_context_fresh": context_fresh,
                "emergency_independent_observations": self._emergency_reversal_windows.get(key, (0.0, 0.0, 0))[2],
                "execution_submitted": False,
            })
        if decision.state.value == "REVERSAL_CONFIRMED" and self.exit_lifecycle_store is not None:
            lifecycle = self.exit_lifecycle_store.recover(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
            )
            if lifecycle is not None:
                self.exit_lifecycle_store.record(
                    lifecycle, reason="reversal_classifier_shadow_confirmed",
                    extra={"state": "REVERSAL_CONFIRMED"},
                )

    def _observe_holding_reversal_ws(self, *, market: OutcomeMarketSpec, finding: object) -> bool:
        """Update the risk classifier from healthy WS BBO without waiting for research REST capture."""
        if self.stream_health is None:
            return False
        coin = str(getattr(finding, "coin", ""))
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        bbo = self.stream_health.fresh_bbo(market, coin)
        if inventory <= 0 or vwap is None or bbo is None:
            return False
        side_index = 0 if coin == market.yes_coin else 1
        evidence = dict(self._holding_context.get(market.outcome_id, {}))
        self._update_holding_reversal(
            market=market, coin=coin, side_index=side_index, vwap=vwap,
            bid=bbo[0], ask=bbo[1], evidence=evidence, book_source="fresh_ws_bbo",
        )
        return True

    def _capture_holding_path(self, *, market: OutcomeMarketSpec, finding: object,
                              update_reversal: bool = True) -> None:
        """Persist as-of open-inventory facts; never changes an order decision."""
        if self.holding_path_recorder is None or self.ledger is None:
            return
        coin = str(getattr(finding, "coin", ""))
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        fee = self._persisted_p3_maker_fee(market=market, coin=coin)
        if inventory <= 0 or vwap is None or fee is None:
            return
        side_index = 0 if coin == market.yes_coin else 1
        try:
            top = self._top_of_book(self._fresh_book_once(market=market, side_index=side_index))
            if top is None:
                return
            bid, ask = top
            if not Decimal("0") < bid < ask < Decimal("1"):
                return
            age = 0.0
            evidence: dict[str, object] = dict(self._holding_context.get(market.outcome_id, {}))
            with sqlite3.connect(self.ledger.journal.db_path) as conn:
                row = conn.execute(
                    """SELECT ts, payload_json FROM strategy_events
                       WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED'
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=? ORDER BY id DESC LIMIT 1""",
                    (market.outcome_id, coin),
                ).fetchone()
            if row:
                age = max(0.0, time.time() - datetime.fromisoformat(str(row[0])).timestamp())
                payload = json.loads(row[1])
                if not evidence:
                    evidence = payload.get("entry_evidence") if isinstance(payload.get("entry_evidence"), dict) else {}
            provenance = self._resolve_holding_entry_provenance(
                market=market, coin=coin, inventory=inventory, fill_vwap=vwap,
            ) or {}
            self.holding_path_recorder.record(OutcomeHoldingPathObservation(
                market.outcome_id, market.period, coin, inventory, vwap, bid, ask, fee, age,
                market.time_to_expiry_sec(), "fresh_rest_book", evidence,
                **provenance,
            ))
            if update_reversal:
                self._update_holding_reversal(
                    market=market, coin=coin, side_index=side_index, vwap=vwap,
                    bid=bid, ask=ask, evidence=evidence, book_source="fresh_rest_book",
                )
        except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
            return

    def _live_entry_age_sec(self, *, market: OutcomeMarketSpec, coin: str) -> float | None:
        if self.ledger is None:
            return None
        try:
            with sqlite3.connect(self.ledger.journal.db_path) as conn:
                row = conn.execute(
                    """SELECT ts FROM strategy_events
                       WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED'
                         AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                         AND json_extract(payload_json, '$.coin')=?
                       ORDER BY id DESC LIMIT 1""",
                    (market.outcome_id, coin),
                ).fetchone()
            return max(0.0, time.time() - datetime.fromisoformat(str(row[0])).timestamp()) if row else None
        except (TypeError, ValueError, sqlite3.Error):
            return None

    def _maybe_emergency_exit(self, *, market: OutcomeMarketSpec, finding: object) -> LiveExecutionResult | None:
        """Run S3 only after all passive/reversal/depth gates independently pass.

        A failed candidate deliberately leaves the current ALO lifecycle to the
        normal requote path; it never converts a stale/illiquid book into a
        market order.
        """
        if self.ledger is None or self.exit_lifecycle_store is None or self.emergency_exit_controller is None:
            return None
        if not tuple(getattr(finding, "sell_order_ids", ())):
            return None
        coin = str(getattr(finding, "coin", ""))
        inventory = Decimal(str(getattr(finding, "inventory", "0")))
        lifecycle = self.exit_lifecycle_store.recover(
            wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
        )
        if lifecycle is None or lifecycle.state == "EMERGENCY_EXIT_SUBMITTED":
            return None
        side_index = 0 if coin == market.yes_coin else 1
        entry_age = self._live_entry_age_sec(market=market, coin=coin)
        loss_since = self.exit_lifecycle_store.loss_band_first_seen_ts(
            wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
        )
        window = self._emergency_reversal_windows.get((market.outcome_id, coin), (0.0, 0.0, 0))
        cfg = self.emergency_exit_policy.config
        # Do not pay for fee/L2 reads on ordinary protected holdings.  These
        # are necessary-but-not-sufficient S3 gates and are entirely local.
        if entry_age is None or entry_age < cfg.min_holding_sec:
            return None
        if loss_since is None or time.time() - loss_since < cfg.min_loss_band_unfilled_sec:
            return None
        if window[2] < cfg.min_independent_reversal_observations or time.time() - window[0] < cfg.min_reversal_duration_sec:
            return None
        if self.exit_lifecycle_store.emergency_attempted(
            wallet=self.recovery.wallet, outcome_id=market.outcome_id, coin=coin,
        ):
            return None
        fill_vwap = self.machine._fill_vwap_for_inventory(coin=coin, inventory=inventory)
        try:
            fees = self.recovery.account.get_user_fees_sync(self.recovery.wallet)
            taker_fee = Decimal(str(fees["userSpotCrossRate"]))
            book = self._fresh_book_once(market=market, side_index=side_index)
            bids = parse_bid_levels(book)
            age = emergency_book_age_sec(book, now_ms=int(time.time() * 1000))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            bids, age, taker_fee = None, None, None
        item = OutcomeEmergencyExitInput(
            inventory=inventory, fill_vwap=fill_vwap, taker_close_fee_rate=taker_fee,
            bids=bids or (), book_age_sec=age, holding_age_sec=entry_age if entry_age is not None else -1.0,
            loss_band_unfilled_sec=(time.time() - loss_since) if loss_since is not None else None,
            reversal_independent_observations=window[2], reversal_duration_sec=(time.time() - window[0]) if window[0] > 0 else 0.0,
            already_attempted=False,
        )
        plan = self.emergency_exit_policy.plan(item)
        self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_EMERGENCY_EXIT_DECISION", {
            "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period,
            "coin": coin, "lifecycle_order_id": lifecycle.order_id, "action": plan.action,
            "reason": plan.reason, "inventory": str(inventory),
            "holding_age_sec": item.holding_age_sec, "loss_band_unfilled_sec": item.loss_band_unfilled_sec,
            "independent_reversal_observations": item.reversal_independent_observations,
            "reversal_duration_sec": item.reversal_duration_sec, "book_age_sec": item.book_age_sec,
            "limit_price": str(plan.limit_price) if plan.limit_price is not None else None,
            "executable_vwap": str(plan.executable_vwap) if plan.executable_vwap is not None else None,
            "net_return_pct": str(plan.net_return_pct) if plan.net_return_pct is not None else None,
            "execution_submitted": False,
        })
        if plan.action is not EmergencyExitAction.EXECUTE:
            return None
        result = self.emergency_exit_controller.execute(
            market=market, side_index=side_index, lifecycle=lifecycle, item=item, plan=plan,
        )
        if result.state == "emergency_exit_submitted":
            self.ledger.journal.log_order_event(
                self.ledger.run_id, "ORDER_SUBMIT", venue_order_id=result.emergency_order_id,
                side="SELL", status="IOC_SUBMITTED", instrument_id=coin, reason=result.detail,
                payload={
                    "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "coin": coin,
                    "execution_type": "s3_price_protected_fak_ioc", "old_order_id": result.old_order_id,
                    "limit_price": str(plan.limit_price), "planned_net_return_pct": str(plan.net_return_pct),
                },
            )
            fills = self.recovery.account.get_user_fills_sync(self.recovery.wallet)
            self.ledger.sync_fills(fills=fills, market_key=f"outcome:{market.outcome_id}", period=market.period)
        return LiveExecutionResult(result.state, result.detail, result.emergency_order_id or result.old_order_id)

    def tick_market(self, *, market: OutcomeMarketSpec, entry_side_index: int | None) -> LiveExecutionResult:
        """Advance existing exposure first; only a flat market accepts a signal."""
        self._begin_tick()
        if not self.enabled():
            return LiveExecutionResult("disabled", "automated execution requires OUTCOME_AUTOMATED_EXECUTION_ENABLED=1 and OUTCOME_SDK_EXECUTION_ENABLED=1")
        report = self.recovery.reconcile([market])
        self._reconcile_exit_lifecycles(market=market, report=report)
        active = [finding for finding in report.findings if finding.state != "flat"]
        if len(active) == 1:
            self._capture_holding_path(market=market, finding=active[0])
            requote = self._maybe_requote_p3_exit(market=market, finding=active[0])
            if requote is not None:
                return requote
            persisted_exit = self._advance_persisted_p3_exit(market=market, finding=active[0])
            if persisted_exit is not None:
                return persisted_exit
        if not report.safe_for_new_entry:
            return LiveExecutionResult("blocked", f"account recovery blocked execution: {report.reason}")
        if active:
            if len(active) != 1:
                return LiveExecutionResult("blocked", "multiple live Outcome sides require explicit reconciliation")
            side_index = 0 if active[0].coin == market.yes_coin else 1
            result = self.machine.tick(market=market, side_index=side_index, entry_permitted=False)
        elif entry_side_index is None:
            return LiveExecutionResult("flat", "no live exposure and no entry signal")
        else:
            # This generic method has no exit-policy parameters.  It may keep
            # observing/reconciling existing orders, but cannot create a buy
            # that would later be forced into a best-ask fallback sell.
            return LiveExecutionResult("blocked", "generic live entry has no explicit verified exit policy; use a dedicated policy runtime")
        side_index = 0 if result.state == "flat" and entry_side_index is None else (side_index if active else entry_side_index)
        assert side_index is not None
        return self._record(market, side_index, result)

    def tick_p3_calibration(self, *, market: OutcomeMarketSpec) -> LiveExecutionResult:
        """Advance one explicit P3 sampling lifecycle without a directional strategy.

        It can only be enabled by a third operator gate.  Existing inventory is
        always handled first; a new entry is one first-level ALO bid on the
        feasible side with the higher market midpoint consensus, subject to the daily cap and all normal
        account/stream/risk checks.  This intentionally bypasses *research*
        readiness because it is collecting the missing P3 evidence, not using
        it for strategy trading.
        """
        self._begin_tick()
        if not self.calibration_enabled():
            return LiveExecutionResult("disabled", "P3 calibration requires automated, SDK, and OUTCOME_P3_CALIBRATION_ENABLED=1 gates")
        if self.ledger is None:
            return LiveExecutionResult("blocked", "P3 calibration requires an execution ledger")
        health_error = self._stream_ready(market)
        if health_error:
            return health_error
        report = self.recovery.reconcile([market])
        self._reconcile_exit_lifecycles(market=market, report=report)
        config = OutcomeP3CalibrationConfig.from_env()
        active = [finding for finding in report.findings if finding.state != "flat"]
        if len(active) == 1:
            self._capture_holding_path(market=market, finding=active[0])
            requote = self._maybe_requote_p3_exit(market=market, finding=active[0])
            if requote is not None:
                return requote
            persisted_exit = self._advance_persisted_p3_exit(market=market, finding=active[0])
            if persisted_exit is not None:
                return persisted_exit
        if not report.safe_for_new_entry:
            return LiveExecutionResult("blocked", f"account recovery blocked calibration: {report.reason}")
        if active:
            if len(active) != 1:
                return LiveExecutionResult("blocked", "multiple live Outcome sides require explicit reconciliation")
            side_index = 0 if active[0].coin == market.yes_coin else 1
            fees = self.recovery.account.get_user_fees_sync(self.recovery.wallet)
            maker_close_fee = Decimal(str(fees["userSpotAddRate"]))
            result = self.machine.tick(
                market=market, side_index=side_index, entry_permitted=False,
                minimum_return_pct=config.target_return_pct, maker_close_fee_rate=maker_close_fee,
                loss_reprice_pct=config.loss_reprice_pct,
            )
            return self._record(market, side_index, result)
        if self._daily_calibration_entries() >= config.max_daily_entries:
            return LiveExecutionResult("flat", f"P3 calibration daily entry cap reached ({config.max_daily_entries})")
        fees = self.recovery.account.get_user_fees_sync(self.recovery.wallet)
        maker_close_fee = Decimal(str(fees["userSpotAddRate"]))
        books = {
            0: self.machine.gateway.fetch_order_book(market=market, side_index=0),
            1: self.machine.gateway.fetch_order_book(market=market, side_index=1),
        }
        bids = {
            side: Decimal(str(book["bids"][0]["price"]))
            for side, book in books.items() if book.get("bids")
        }
        mids = {
            side: (Decimal(str(book["bids"][0]["price"])) + Decimal(str(book["asks"][0]["price"]))) / Decimal("2")
            for side, book in books.items() if book.get("bids") and book.get("asks")
        }
        side_index = choose_consensus_calibration_side(
            mids=mids, entry_bids=bids,
            target_return_pct=config.target_return_pct, maker_close_fee_rate=maker_close_fee,
            tie_breaker=market.outcome_id,
        )
        if side_index is None:
            return LiveExecutionResult("flat", "no side can support the configured net take-profit below Outcome price ceiling")
        price = bids[side_index]
        shares = whole_share_size(price)
        account = self.recovery.account
        risk = self.risk_gate.evaluate(
            balances=account.get_spot_clearinghouse_state_sync(self.recovery.wallet).get("balances", []),
            open_orders=account.get_open_orders_sync(self.recovery.wallet), price=price, shares=shares,
        )
        if not risk.allowed:
            return LiveExecutionResult("blocked", f"risk gate: {risk.reason}")
        result = self.machine.tick(market=market, side_index=side_index, entry_permitted=True)
        if result.state == "buy_placed":
            self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_P3_CALIBRATION_ENTRY_PLACED", {
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period,
                "side_index": side_index, "coin": self.machine.gateway.outcome_coin(market, side_index),
                "price": str(price), "shares": shares, "target_return_pct": str(config.target_return_pct),
                "loss_reprice_pct": str(config.loss_reprice_pct), "maker_close_fee_rate": str(maker_close_fee),
                "order_id": result.order_id,
                "sampling_policy": "market_mid_consensus", "directional_signal_used": False,
            })
        return self._record(market, side_index, result)

    def tick_live_strategy(self, *, market: OutcomeMarketSpec, entry_side_index: int | None,
                           entry_reason: str, entry_evidence: dict[str, object],
                           retiring_markets: tuple[OutcomeMarketSpec, ...] = (),
                           market_context: dict[str, object] | None = None) -> LiveExecutionResult:
        """Run S0 and durably record its final admission or rejection reason."""
        self._begin_tick()
        started_at = time.monotonic()
        admission: dict[str, object] = {}
        result = self._tick_live_strategy(
            market=market, entry_side_index=entry_side_index, entry_reason=entry_reason,
            entry_evidence=entry_evidence, retiring_markets=retiring_markets,
            market_context=market_context, admission=admission,
        )
        admission["timing_runtime_before_journal_ms"] = round((time.monotonic() - started_at) * 1000, 3)
        journal_started_at = time.monotonic()
        self._record_entry_admission_decision(
            market=market, entry_side_index=entry_side_index, entry_reason=entry_reason,
            entry_evidence=entry_evidence, admission=admission, result=result,
        )
        journal_write_ms = round((time.monotonic() - journal_started_at) * 1000, 3)
        total_ms = round((time.monotonic() - started_at) * 1000, 3)
        if self.ledger is not None and (total_ms >= 1000 or result.state in {"buy_placed", "sell_placed", "sell_resting"}):
            self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_RUNTIME_TIMING", {
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id,
                "runtime_state": result.state, "total_ms": total_ms,
                "account_recovery_ms": admission.get("timing_account_recovery_ms"),
                "book_request_ms": admission.get("timing_entry_book_request_ms"),
                "journal_write_ms": journal_write_ms,
                "journal_writer_last_ms": dict(getattr(self.ledger.journal, "last_write_timing_ms", {})),
                "last_sdk_request": dict(getattr(self.machine.gateway, "last_sidecar_timing", {}) or {}),
            })
        return result

    def _tick_live_strategy(self, *, market: OutcomeMarketSpec, entry_side_index: int | None,
                            entry_reason: str, entry_evidence: dict[str, object],
                            retiring_markets: tuple[OutcomeMarketSpec, ...],
                            market_context: dict[str, object] | None,
                            admission: dict[str, object]) -> LiveExecutionResult:
        """Run one explicitly gated S0 live strategy lifecycle.

        The caller supplies a pure, already fail-closed OI/spot decision.  The
        runtime still owns stream health, account recovery, feasibility, risk,
        order submission and durable evidence.
        """
        if not self.live_strategy_enabled():
            admission["execution_gate"] = "live_strategy_disabled"
            return LiveExecutionResult("disabled", "live strategy requires automated, SDK, and OUTCOME_LIVE_STRATEGY_ENABLED gates")
        if self.ledger is None:
            return LiveExecutionResult("blocked", "live strategy requires an execution ledger")
        self._holding_context[market.outcome_id] = dict(market_context or entry_evidence)
        admission["market_regime_shadow"] = self._observe_market_regime_shadow(
            market=market, entry_side_index=entry_side_index,
            entry_evidence=entry_evidence, market_context=market_context,
        )
        # C1/C2 research capture is strictly public-data, bounded to one row
        # per 30 seconds and independent of whether the canary is enabled.
        self._capture_trend_continuation_path(
            market=market, entry_evidence=entry_evidence, market_context=market_context,
        )
        config = OutcomeLiveStrategyConfig.from_env()
        tracked_markets = (market, *retiring_markets)
        recovery_started_at = time.monotonic()
        report = self.recovery.reconcile(tracked_markets)
        admission["timing_account_recovery_ms"] = round((time.monotonic() - recovery_started_at) * 1000, 3)
        admission["account_recovery"] = {
            "safe_for_new_entry": bool(getattr(report, "safe_for_new_entry", False)),
            "reason": str(getattr(report, "reason", "unknown")),
        }
        # An IOC may fill between the previous tick and this account snapshot.
        # Import official fills before evaluating its durable loss/re-entry
        # consequence; otherwise an emergency close could be misclassified as
        # merely a missing order.
        try:
            self.ledger.sync_fills(
                fills=self.recovery.account.get_user_fills_sync(self.recovery.wallet),
                market_key=f"outcome:{market.outcome_id}", period=market.period,
            )
        except Exception:
            # Account recovery remains the hard safety source; missing fill
            # history means no inferred loss/re-entry transition.
            pass
        self._reconcile_exit_lifecycles(market=market, report=report)
        retired_active = [
            finding for finding in report.findings
            if finding.market_id != market.outcome_id and finding.state != "flat"
        ]
        if retired_active:
            admission["rollover_gate"] = "retiring_market_active"
            return LiveExecutionResult(
                "blocked",
                "market rollover pending: retiring Outcome has live inventory or order; new entry refused",
            )
        active = [finding for finding in report.findings if finding.market_id == market.outcome_id and finding.state != "flat"]
        admission["active_current_market_count"] = len(active)
        self._record_entry_gate_decision(
            market=market, entry_side_index=entry_side_index, entry_reason=entry_reason,
            entry_evidence=entry_evidence, active=active,
        )
        fill_visibility_barrier = self._entry_fill_visibility_barrier(
            market=market, report=report, admission=admission,
        )
        if fill_visibility_barrier is not None:
            return fill_visibility_barrier
        if len(active) == 1:
            # A filled buy plus an open owned remainder is the only state
            # where cancellation is more urgent than every observational
            # read.  Do it first so a slow holding-path REST capture cannot
            # extend the interval before the first protective sell.
            filled_entry_cleanup = self._cancel_filled_entry_and_place_protection(
                market=market, finding=active[0],
            )
            if filled_entry_cleanup is not None:
                return filled_entry_cleanup
            # Toxic-fill is deliberately observation-only in this milestone.
            # It must never delay the cancel-before-protective-sell boundary
            # above, nor can it authorize a repricing or taker exit.
            self._observe_toxic_fill_shadow(market=market, finding=active[0])
            holding_key = (market.outcome_id, str(getattr(active[0], "coin", "")))
            now = time.monotonic()
            reversal_observed = False
            if now - self._last_reversal_risk_observation_at.get(holding_key, float("-inf")) >= self._REVERSAL_RISK_MIN_INTERVAL_SEC:
                reversal_observed = self._observe_holding_reversal_ws(market=market, finding=active[0])
                if reversal_observed:
                    self._last_reversal_risk_observation_at[holding_key] = now
            if now - self._last_holding_path_capture_at.get(holding_key, float("-inf")) >= self._HOLDING_PATH_MIN_INTERVAL_SEC:
                self._capture_holding_path(
                    market=market, finding=active[0], update_reversal=not reversal_observed,
                )
                self._last_holding_path_capture_at[holding_key] = now
            # S3 deliberately uses a freshly fetched REST L2 depth walk, not
            # the WebSocket cache.  It may therefore assess an already-held
            # position even when the stream only blocks *new* entries.
            emergency = self._maybe_emergency_exit(market=market, finding=active[0])
            if emergency is not None:
                return emergency
            entry_requote = self._maybe_requote_entry_buy(
                market=market, finding=active[0], entry_side_index=entry_side_index,
                entry_reason=entry_reason, config=config,
            )
            if entry_requote is not None:
                return entry_requote
        health_error = self._stream_ready(market)
        if health_error:
            admission["market_data_gate"] = health_error.detail
            return health_error
        admission["market_data_gate"] = "ws_fresh"
        if len(active) == 1:
            requote = self._maybe_requote_p3_exit(market=market, finding=active[0])
            if requote is not None:
                return requote
            persisted_exit = self._advance_persisted_p3_exit(market=market, finding=active[0])
            if persisted_exit is not None:
                return persisted_exit
        if not report.safe_for_new_entry:
            return LiveExecutionResult("blocked", f"account recovery blocked live strategy: {report.reason}")
        if active:
            admission["account_gate"] = "existing_outcome_inventory_or_order"
            return LiveExecutionResult("blocked", "live strategy has existing Outcome inventory or order")
        if entry_side_index not in (0, 1):
            admission["signal_gate"] = "no_directional_signal"
            return LiveExecutionResult("flat", f"live strategy no entry: {entry_reason}")
        admission["selected_side_index"] = entry_side_index
        admission["selected_coin"] = self.machine.gateway.outcome_coin(market, entry_side_index)
        if self.entry_lifecycle_store is not None:
            cooldown = self.entry_lifecycle_store.fast_rebook_cooldown_remaining(
                wallet=self.recovery.wallet, outcome_id=market.outcome_id,
                coin=str(admission["selected_coin"]),
                cooldown_sec=self.entry_planner.config.fast_rebook_cooldown_sec,
            )
            admission["entry_fast_rebook_cooldown_remaining_sec"] = round(cooldown, 3)
            if cooldown > 0:
                return LiveExecutionResult(
                    "flat", f"live strategy no entry: fast_risk_rebook_cooldown ({cooldown:.1f}s remaining)",
                )
        fees = self.recovery.account.get_user_fees_sync(self.recovery.wallet)
        maker_close_fee = Decimal(str(fees["userSpotAddRate"]))
        book_started_at = time.monotonic()
        book = self.machine.gateway.fetch_order_book(market=market, side_index=entry_side_index)
        admission["timing_entry_book_request_ms"] = round((time.monotonic() - book_started_at) * 1000, 3)
        try:
            price = Decimal(str(book["bids"][0]["price"]))
            entry_ask = Decimal(str(book["asks"][0]["price"]))
        except (IndexError, KeyError, TypeError, ValueError):
            admission["book_gate"] = "selected_entry_book_unavailable"
            return LiveExecutionResult("blocked", "live strategy entry book unavailable")
        admission["selected_best_bid"] = str(price)
        # The opening 50/50 region is an uncertainty regime, not a bargain
        # by itself.  S0 is a momentum/confirmation experiment and therefore
        # never tries to call a reversal from the centre of the binary range.
        if price < config.min_entry_price:
            admission["entry_price_gate"] = "selected_bid_in_no_trade_band"
            return LiveExecutionResult(
                "flat",
                f"live strategy no-trade band: selected bid {price} < {config.min_entry_price}",
            )
        target_decision = OutcomeExitTargetPolicy(self.ledger.journal.db_path).decide(
            outcome_id=market.outcome_id, side_index=entry_side_index,
        )
        target_price_preview = take_profit_price(
            entry_price=price, target_return_pct=target_decision.target_return_pct,
            maker_close_fee_rate=maker_close_fee,
        )
        if target_price_preview is None:
            admission["target_gate"] = "fee_after_target_exceeds_price_ceiling"
            return LiveExecutionResult("flat", "live strategy dynamic fee-after target exceeds Outcome price ceiling")
        reentry = None
        if self.loss_reentry_gate is not None:
            reentry = self.loss_reentry_gate.evaluate(
                outcome_id=market.outcome_id,
                coin=admission["selected_coin"],
                candidate_bid=float(price),
            )
            admission["loss_reentry_gate"] = {
                "allowed": reentry.allowed,
                "reason": reentry.reason,
                "limited_reentry": reentry.is_limited_reentry,
                "prior_exit_price": reentry.prior_exit_price,
                "cooldown_remaining_sec": reentry.cooldown_remaining_sec,
            }
            if not reentry.allowed:
                return LiveExecutionResult("flat", f"live strategy no entry: {reentry.reason}")
        admission["target_return_pct"] = str(target_decision.target_return_pct)
        admission["target_policy_source"] = target_decision.source
        admission["target_price_preview"] = str(target_price_preview)
        # Persist the target decision on the ORDER_SUBMIT record itself.  The
        # follow-up strategy event remains useful for research queries, but
        # it is deliberately not the sole source of truth for an accepted
        # live entry order.
        entry_tier = str(entry_evidence.get("entry_tier") or "tier_a_spot_mark_oi")
        tier_b = entry_tier == "tier_b_spot_mark"
        continuation = entry_tier == "tier_c_trend_continuation"
        entry_policy_kind = (
            "s0_trend_continuation" if continuation
            else "s0_spot_mark_tier_b" if tier_b else "s0_oi_spot_mark_confirmation"
        )
        sampling_policy = (
            "trend_continuation" if continuation
            else "spot_mark_tier_b" if tier_b else "oi_spot_mark_confirmation"
        )
        if continuation:
            # C4 deliberately spends the one canary opportunity when the
            # official SDK accepts a resting BUY, not when a signal merely
            # appears.  The DB check makes restart behavior equally bounded.
            if self._continuation_entry_already_submitted(outcome_id=market.outcome_id):
                admission["continuation_gate"] = "one_submit_per_market_exhausted"
                return LiveExecutionResult("flat", "live strategy no entry: continuation_one_submit_per_market_exhausted")
            admission["continuation_gate"] = "one_submit_per_market_available"
        capacity_canary = (
            self.risk_gate.limits.max_entry_notional_usdc >= Decimal("20")
            and self.risk_gate.limits.max_total_outcome_exposure_usdc >= Decimal("20")
        )
        shares = whole_share_size(price)
        entry_max_submit_price: Decimal | None = None
        execution_audit: dict[str, object] = {"entry_capacity_canary_enabled": capacity_canary}
        if capacity_canary:
            min_opening_shares = whole_share_size(price)
            # Size to the configured dollar ceiling without rounding *above*
            # it.  The venue minimum is still separately enforced below.
            desired_shares = max(
                min_opening_shares,
                int((self.risk_gate.limits.max_entry_notional_usdc / price).to_integral_value(rounding=ROUND_FLOOR)),
            )
            gate = self.tier_b_execution_gate
            quality = gate.evaluate(
                bid=price, ask=entry_ask, bid_levels=book.get("bids", ()), requested_shares=Decimal(desired_shares),
                coin=str(admission["selected_coin"]),
            ) if gate is not None else None
            if quality is None:
                admission["entry_execution_gate"] = {"allowed": False, "reason": "entry_execution_gate_unavailable"}
                return LiveExecutionResult("blocked", "entry execution-quality gate unavailable")
            safe_shares = min(Decimal(desired_shares), quality.safe_max_shares or Decimal("0"))
            # Do not let whole_share_size round inadequate capacity up to the
            # venue minimum.  Capacity is a ceiling, never a hint.
            shares = int(safe_shares) if safe_shares >= min_opening_shares else 0
            execution_audit.update({
                "entry_execution_tier": entry_tier,
                "entry_spread_bps": str(quality.spread_bps) if quality.spread_bps is not None else None,
                "entry_top3_depth_shares": str(quality.top_depth_shares) if quality.top_depth_shares is not None else None,
                "entry_safe_max_shares": str(quality.safe_max_shares) if quality.safe_max_shares is not None else None,
                "entry_recent_trade_shares_5m": str(quality.recent_trade_shares) if quality.recent_trade_shares is not None else None,
                "entry_requested_shares": desired_shares,
                "entry_submitted_shares": shares,
                "entry_max_submit_drift_bps": str(quality.policy.max_submit_drift_bps),
                "entry_policy_sample_count": quality.policy.sample_count,
                "entry_policy_source": quality.policy.source,
                "entry_decision_bid": str(quality.decision_bid) if quality.decision_bid is not None else None,
            })
            admission["entry_execution_gate"] = {"allowed": quality.allowed, "reason": quality.reason, **execution_audit}
            if not quality.allowed or quality.max_submit_bid is None:
                return LiveExecutionResult("flat", f"live strategy no entry: {quality.reason}")
            if shares < min_opening_shares:
                return LiveExecutionResult("flat", "live strategy no entry: entry_safe_capacity_below_venue_minimum")
            entry_max_submit_price = quality.max_submit_bid
        elif tier_b:
            # Preserve the existing $11 Tier-B quality behavior until the
            # operator explicitly starts the $20 canary.
            gate = self.tier_b_execution_gate
            quality = gate.evaluate(
                bid=price, ask=entry_ask, bid_levels=book.get("bids", ()), requested_shares=Decimal(shares),
                coin=str(admission["selected_coin"]),
            ) if gate is not None else None
            if quality is None or not quality.allowed or quality.max_submit_bid is None:
                reason = quality.reason if quality is not None else "tier_b_execution_gate_unavailable"
                admission["tier_b_execution_gate"] = {"allowed": False, "reason": reason}
                return LiveExecutionResult("flat", f"live strategy no entry: {reason}")
            entry_max_submit_price = quality.max_submit_bid
            execution_audit.update({
                "tier_b_spread_bps": str(quality.spread_bps) if quality.spread_bps is not None else None,
                "tier_b_top_depth_shares": str(quality.top_depth_shares) if quality.top_depth_shares is not None else None,
                "tier_b_max_submit_drift_bps": str(quality.policy.max_submit_drift_bps),
                "tier_b_policy_sample_count": quality.policy.sample_count,
                "tier_b_policy_source": quality.policy.source,
                "tier_b_decision_bid": str(quality.decision_bid) if quality.decision_bid is not None else None,
            })
        risk = self.risk_gate.evaluate(
            balances=self.recovery.account.get_spot_clearinghouse_state_sync(self.recovery.wallet).get("balances", []),
            open_orders=self.recovery.account.get_open_orders_sync(self.recovery.wallet), price=price, shares=shares,
        )
        if not risk.allowed:
            admission["risk_gate"] = {
                "allowed": False, "reason": risk.reason,
                "entry_notional": str(risk.entry_notional),
                "available_collateral": str(risk.available_collateral),
                "current_exposure": str(risk.current_exposure),
            }
            return LiveExecutionResult("blocked", f"risk gate: {risk.reason}")
        admission["risk_gate"] = {
            "allowed": True, "reason": risk.reason,
            "entry_notional": str(risk.entry_notional),
            "available_collateral": str(risk.available_collateral),
            "current_exposure": str(risk.current_exposure),
        }
        portfolio = self.portfolio_guard.evaluate(
            outcome_id=market.outcome_id, prospective_notional=risk.entry_notional,
            phase_entry_cap=self.risk_gate.limits.max_entry_notional_usdc,
            phase_exposure_cap=self.risk_gate.limits.max_total_outcome_exposure_usdc,
        ) if self.portfolio_guard is not None else None
        if portfolio is None or not portfolio.allowed:
            reason = portfolio.reason if portfolio is not None else "portfolio_guard_unavailable"
            admission["portfolio_guard"] = {"allowed": False, "reason": reason}
            return LiveExecutionResult("flat", f"live strategy no entry: {reason}")
        admission["portfolio_guard"] = {
            "allowed": True, "reason": portfolio.reason, "enabled": portfolio.enabled,
            "daily_gross_entry_usdc": str(portfolio.daily_gross_entry_usdc),
            "daily_realized_net_usdc": str(portfolio.daily_realized_net_usdc),
            "daily_gross_entry_limit_usdc": str(portfolio.daily_gross_entry_limit_usdc),
            "daily_realized_loss_limit_usdc": str(portfolio.daily_realized_loss_limit_usdc),
            "consecutive_loss_exits": portfolio.consecutive_loss_exits,
            "market_loss_exits": portfolio.market_loss_exits,
        }
        entry_audit = {
            "entry_policy_schema_version": 1,
            "entry_policy_kind": entry_policy_kind,
            "entry_tier": entry_tier,
            "target_return_pct": str(target_decision.target_return_pct),
            "target_policy_source": target_decision.source,
            "target_estimated_move_pct": (
                str(target_decision.estimated_move_pct)
                if target_decision.estimated_move_pct is not None else None
            ),
            "target_volatility_sample_count": target_decision.sample_count,
            "maker_close_fee_rate": str(maker_close_fee),
            "loss_reprice_pct": "0.05",
            "narrow_after_sec": config.narrow_after_sec,
            "narrow_return_pct": str(config.narrow_return_pct),
            "floor_after_sec": config.floor_after_sec,
            "floor_return_pct": str(config.floor_return_pct),
            "entry_bid_at_decision": str(price),
            # A fill may occur away from this bid.  The protective sell is
            # recalculated from verified fill VWAP, so label this explicitly
            # as a decision-time preview rather than an asserted exit price.
            "target_price_preview_from_decision_bid": str(target_price_preview),
            "target_decision_at_ms": int(time.time() * 1000),
            "loss_reentry_policy": reentry.reason if reentry is not None else "unavailable",
            "loss_reentry_limited": bool(reentry.is_limited_reentry) if reentry is not None else False,
            "loss_reentry_prior_exit_price": (
                str(reentry.prior_exit_price) if reentry is not None and reentry.prior_exit_price is not None else None
            ),
            **execution_audit,
        }
        result = self.machine.tick(
            market=market, side_index=entry_side_index, entry_permitted=True,
            entry_audit=entry_audit, entry_max_submit_price=entry_max_submit_price,
            entry_requested_shares=Decimal(shares),
            entry_max_notional=self.risk_gate.limits.max_entry_notional_usdc,
        )
        if result.state == "buy_placed":
            if reentry is not None and reentry.is_limited_reentry and self.loss_reentry_gate is not None:
                # The token is consumed only after official SDK acceptance of
                # the new resting ALO BUY; a rejected quote must not lock the
                # market for the rest of the day.
                self.loss_reentry_gate.record_reentry_submitted(
                    outcome_id=market.outcome_id,
                    period=market.period,
                    coin=self.machine.gateway.outcome_coin(market, entry_side_index),
                    order_id=str(result.order_id),
                    bid=float(price),
                    target_price=float(target_price_preview),
                    entry_reason=entry_reason,
                )
            self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_LIVE_STRATEGY_ENTRY_PLACED", {
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period,
                "side_index": entry_side_index, "coin": self.machine.gateway.outcome_coin(market, entry_side_index),
                "price": str(price), "shares": shares, "target_return_pct": str(target_decision.target_return_pct),
                "target_policy_source": target_decision.source,
                "target_estimated_move_pct": str(target_decision.estimated_move_pct) if target_decision.estimated_move_pct is not None else None,
                "target_volatility_sample_count": target_decision.sample_count,
                "entry_policy_schema_version": 1,
                "order_submit_audit_persisted": True,
                # Prior to the two-hour floor tier no loss band is eligible.
                # Thereafter this is a fee-inclusive -5% passive quote only.
                "loss_reprice_pct": "0.05", "maker_close_fee_rate": str(maker_close_fee),
                "narrow_after_sec": config.narrow_after_sec, "narrow_return_pct": str(config.narrow_return_pct),
                "floor_after_sec": config.floor_after_sec, "floor_return_pct": str(config.floor_return_pct),
                "order_id": result.order_id, "entry_reason": entry_reason,
                "entry_evidence": entry_evidence, "entry_tier": entry_tier,
                "sampling_policy": sampling_policy,
                "directional_signal_used": True,
                "loss_reentry_policy": reentry.reason if reentry is not None else "unavailable",
                "loss_reentry_limited": bool(reentry.is_limited_reentry) if reentry is not None else False,
                "loss_reentry_prior_exit_price": (
                    str(reentry.prior_exit_price) if reentry is not None and reentry.prior_exit_price is not None else None
                ),
            })
        return self._record(market, entry_side_index, result)

    def cancel_resting_buys(
        self,
        *,
        market: OutcomeMarketSpec,
        tracked_markets: tuple[OutcomeMarketSpec, ...] = (),
    ) -> LiveExecutionResult:
        """Reduce-only transition: cancel owned entries, never sell/take."""
        if not self.enabled():
            return LiveExecutionResult("disabled", "automated execution is disabled")
        report = self.recovery.reconcile((market, *tracked_markets))
        if not report.safe_for_new_entry:
            return LiveExecutionResult("blocked", f"account recovery blocked cancellation: {report.reason}")
        cancelled: list[str] = []
        for side_index, coin in enumerate((market.yes_coin, market.no_coin)):
            finding = next(item for item in report.findings if item.market_id == market.outcome_id and item.coin == coin)
            for order_id in finding.buy_order_ids:
                self.machine.gateway.cancel_owned_order(market=market, side_index=side_index, order_id=order_id)
                self._account_reads.invalidate()
                cancelled.append(order_id)
        return LiveExecutionResult("cancelled" if cancelled else "flat", "cancelled owned entry buys" if cancelled else "no owned entry buy", cancelled[0] if cancelled else None)
