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
from decimal import Decimal

from bot.adapters.outcome_client import OutcomeClient
from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_account_recovery import OutcomeAccountRecovery
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


@dataclass(frozen=True)
class LiveExecutionResult:
    state: str
    detail: str
    order_id: str | None = None


class OutcomeLiveExecutionRuntime:
    def __init__(self, *, account: OutcomeClient, wallet: str, gateway: OutcomeExecutionGateway | None = None, risk_gate: OutcomePreTradeRiskGate | None = None, stream_health: OutcomeStreamHealth | None = None, ledger: OutcomeExecutionLedger | None = None, research_gate: OutcomeResearchGate | None = None, exit_planner: OutcomeExitQuotePlanner | None = None, exit_lifecycle_store: OutcomeExitLifecycleStore | None = None, exit_requote_controller: OutcomeExitRequoteController | None = None) -> None:
        self.recovery = OutcomeAccountRecovery(account=account, wallet=wallet)
        self.machine = OutcomeMakerStateMachine(account=account, gateway=gateway or OutcomeExecutionGateway(), wallet=wallet)
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
            OutcomeExitRequoteController(account=account, gateway=self.machine.gateway, store=self.exit_lifecycle_store, wallet=wallet)
            if self.exit_lifecycle_store else None
        )
        # E5 is deliberately process-local.  A restart must never resume a
        # canary against an old position without a new explicit operator run.
        self._e5_canary_eligible_order_ids: set[str] = set()

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

    def _persisted_p3_exit_policy(self, *, market: OutcomeMarketSpec, coin: str) -> OutcomeP3CalibrationConfig | None:
        """Recover the policy that created a still-managed P3 inventory.

        Entry and exit must not depend on which dispatcher happens to run on
        the next tick.  The entry event is durable evidence of the approved
        policy; malformed or absent evidence is deliberately not guessed.
        """
        if self.ledger is None:
            return None
        try:
            with sqlite3.connect(self.ledger.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT payload_json FROM strategy_events
                    WHERE event_type IN ('OUTCOME_P3_CALIBRATION_ENTRY_PLACED', 'OUTCOME_LIVE_STRATEGY_ENTRY_PLACED')
                      AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                      AND json_extract(payload_json, '$.coin')=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (market.outcome_id, coin),
                ).fetchone()
            if not row:
                return None
            payload = json.loads(row[0])
            return OutcomeP3CalibrationConfig(
                max_daily_entries=1,
                target_return_pct=Decimal(str(payload["target_return_pct"])),
                loss_reprice_pct=Decimal(str(payload["loss_reprice_pct"])),
            )
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            return None

    def _persisted_p3_maker_fee(self, *, market: OutcomeMarketSpec, coin: str) -> Decimal | None:
        if self.ledger is None:
            return None
        try:
            with sqlite3.connect(self.ledger.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT json_extract(payload_json, '$.maker_close_fee_rate')
                    FROM strategy_events
                    WHERE event_type IN ('OUTCOME_P3_CALIBRATION_ENTRY_PLACED', 'OUTCOME_LIVE_STRATEGY_ENTRY_PLACED')
                      AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                      AND json_extract(payload_json, '$.coin')=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (market.outcome_id, coin),
                ).fetchone()
            fee = Decimal(str(row[0])) if row and row[0] is not None else None
            return fee if fee is not None and Decimal("0") <= fee < Decimal("1") else None
        except (ArithmeticError, sqlite3.Error):
            return None

    def _strategy_exit_tier(self, *, market: OutcomeMarketSpec, coin: str) -> tuple[Decimal, Decimal | None] | None:
        """Return the elapsed-time S0 target and any permitted loss-band floor.

        The strategy starts with a strict +5% fee-after target.  It may narrow
        to +3% after the first configured age and +2% after the second.  Only
        at that final tier may a weak book use the fee-inclusive break-even
        floor.  This prevents a one-tick adverse quote from replacing the
        initial take-profit order with a near-cost sell.
        """
        if self.ledger is None:
            return None
        try:
            with sqlite3.connect(self.ledger.journal.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT ts, payload_json FROM strategy_events
                    WHERE event_type='OUTCOME_LIVE_STRATEGY_ENTRY_PLACED'
                      AND CAST(json_extract(payload_json, '$.outcome_id') AS INTEGER)=?
                      AND json_extract(payload_json, '$.coin')=?
                    ORDER BY id DESC LIMIT 1
                    """, (market.outcome_id, coin),
                ).fetchone()
            if not row:
                return None
            payload = json.loads(row[1])
            target = Decimal(str(payload["target_return_pct"]))
            narrow_after = float(payload["narrow_after_sec"])
            narrow = Decimal(str(payload["narrow_return_pct"]))
            floor_after = float(payload["floor_after_sec"])
            floor = Decimal(str(payload["floor_return_pct"]))
            # Age is anchored to the original entry evidence, not the latest
            # replacement timestamp; otherwise each rebook would silently
            # postpone the +3%/+2% time tiers.
            age = max(0.0, time.time() - datetime.fromisoformat(str(row[0])).timestamp())
            if age >= floor_after:
                # The loss band is only eligible after the two-hour floor
                # tier.  It is a fee-inclusive -5% passive quote, never an
                # immediate stop or a taker instruction.
                return floor, Decimal("0.05")
            if age >= narrow_after:
                return narrow, None
            return target, None
        except (KeyError, TypeError, ValueError, ArithmeticError, sqlite3.Error, json.JSONDecodeError):
            return None

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
            book = self.machine.gateway.fetch_order_book(market=market, side_index=side_index)
            bid = Decimal(str(book["bids"][0]["price"])); ask = Decimal(str(book["asks"][0]["price"]))
        except (IndexError, KeyError, TypeError, ValueError):
            return LiveExecutionResult("blocked", "exit reprice book unavailable", lifecycle.order_id)
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
                result = self.exit_requote_controller.execute(market=market, side_index=side_index, lifecycle=lifecycle, plan=canary_plan)
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
        ))
        if plan.action.value == "KEEP":
            return LiveExecutionResult("sell_resting", f"exit reprice keep: {plan.reason}", lifecycle.order_id)
        if plan.action.value == "BLOCK":
            return LiveExecutionResult("blocked", f"exit reprice blocked: {plan.reason}", lifecycle.order_id)
        result = self.exit_requote_controller.execute(market=market, side_index=side_index, lifecycle=lifecycle, plan=plan)
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
                self.exit_lifecycle_store.reconcile_owned_sell(
                    wallet=self.recovery.wallet, outcome_id=market.outcome_id,
                    coin=str(getattr(finding, "coin", "")),
                    inventory=Decimal(str(getattr(finding, "inventory", "0"))), open_orders=open_orders,
                )
        except Exception:
            # The normal recovery report remains the execution gate.  Do not
            # infer a close from a failed read.
            return

    def tick_market(self, *, market: OutcomeMarketSpec, entry_side_index: int | None) -> LiveExecutionResult:
        """Advance existing exposure first; only a flat market accepts a signal."""
        if not self.enabled():
            return LiveExecutionResult("disabled", "automated execution requires OUTCOME_AUTOMATED_EXECUTION_ENABLED=1 and OUTCOME_SDK_EXECUTION_ENABLED=1")
        report = self.recovery.reconcile([market])
        self._reconcile_exit_lifecycles(market=market, report=report)
        active = [finding for finding in report.findings if finding.state != "flat"]
        if len(active) == 1:
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
                           retiring_markets: tuple[OutcomeMarketSpec, ...] = ()) -> LiveExecutionResult:
        """Run one explicitly gated S0 live strategy lifecycle.

        The caller supplies a pure, already fail-closed OI/spot decision.  The
        runtime still owns stream health, account recovery, feasibility, risk,
        order submission and durable evidence.
        """
        if not self.live_strategy_enabled():
            return LiveExecutionResult("disabled", "live strategy requires automated, SDK, and OUTCOME_LIVE_STRATEGY_ENABLED gates")
        if self.ledger is None:
            return LiveExecutionResult("blocked", "live strategy requires an execution ledger")
        health_error = self._stream_ready(market)
        if health_error:
            return health_error
        config = OutcomeLiveStrategyConfig.from_env()
        tracked_markets = (market, *retiring_markets)
        report = self.recovery.reconcile(tracked_markets)
        self._reconcile_exit_lifecycles(market=market, report=report)
        retired_active = [
            finding for finding in report.findings
            if finding.market_id != market.outcome_id and finding.state != "flat"
        ]
        if retired_active:
            return LiveExecutionResult(
                "blocked",
                "market rollover pending: retiring Outcome has live inventory or order; new entry refused",
            )
        active = [finding for finding in report.findings if finding.market_id == market.outcome_id and finding.state != "flat"]
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
            return LiveExecutionResult("blocked", "live strategy has existing Outcome inventory or order")
        if entry_side_index not in (0, 1):
            return LiveExecutionResult("flat", f"live strategy no entry: {entry_reason}")
        fees = self.recovery.account.get_user_fees_sync(self.recovery.wallet)
        maker_close_fee = Decimal(str(fees["userSpotAddRate"]))
        book = self.machine.gateway.fetch_order_book(market=market, side_index=entry_side_index)
        try:
            price = Decimal(str(book["bids"][0]["price"]))
        except (IndexError, KeyError, TypeError, ValueError):
            return LiveExecutionResult("blocked", "live strategy entry book unavailable")
        # The opening 50/50 region is an uncertainty regime, not a bargain
        # by itself.  S0 is a momentum/confirmation experiment and therefore
        # never tries to call a reversal from the centre of the binary range.
        if price < config.min_entry_price:
            return LiveExecutionResult(
                "flat",
                f"live strategy no-trade band: selected bid {price} < {config.min_entry_price}",
            )
        target_decision = OutcomeExitTargetPolicy(self.ledger.journal.db_path).decide(
            outcome_id=market.outcome_id, side_index=entry_side_index,
        )
        if take_profit_price(entry_price=price, target_return_pct=target_decision.target_return_pct,
                             maker_close_fee_rate=maker_close_fee) is None:
            return LiveExecutionResult("flat", "live strategy dynamic fee-after target exceeds Outcome price ceiling")
        shares = whole_share_size(price)
        risk = self.risk_gate.evaluate(
            balances=self.recovery.account.get_spot_clearinghouse_state_sync(self.recovery.wallet).get("balances", []),
            open_orders=self.recovery.account.get_open_orders_sync(self.recovery.wallet), price=price, shares=shares,
        )
        if not risk.allowed:
            return LiveExecutionResult("blocked", f"risk gate: {risk.reason}")
        result = self.machine.tick(market=market, side_index=entry_side_index, entry_permitted=True)
        if result.state == "buy_placed":
            self.ledger.journal.log_strategy_event(self.ledger.run_id, "OUTCOME_LIVE_STRATEGY_ENTRY_PLACED", {
                "venue": "hyperliquid_outcome", "outcome_id": market.outcome_id, "period": market.period,
                "side_index": entry_side_index, "coin": self.machine.gateway.outcome_coin(market, entry_side_index),
                "price": str(price), "shares": shares, "target_return_pct": str(target_decision.target_return_pct),
                "target_policy_source": target_decision.source,
                "target_estimated_move_pct": str(target_decision.estimated_move_pct) if target_decision.estimated_move_pct is not None else None,
                "target_volatility_sample_count": target_decision.sample_count,
                # Prior to the two-hour floor tier no loss band is eligible.
                # Thereafter this is a fee-inclusive -5% passive quote only.
                "loss_reprice_pct": "0.05", "maker_close_fee_rate": str(maker_close_fee),
                "narrow_after_sec": config.narrow_after_sec, "narrow_return_pct": str(config.narrow_return_pct),
                "floor_after_sec": config.floor_after_sec, "floor_return_pct": str(config.floor_return_pct),
                "order_id": result.order_id, "entry_reason": entry_reason,
                "entry_evidence": entry_evidence, "sampling_policy": "oi_spot_mark_confirmation",
                "directional_signal_used": True,
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
                cancelled.append(order_id)
        return LiveExecutionResult("cancelled" if cancelled else "flat", "cancelled owned entry buys" if cancelled else "no owned entry buy", cancelled[0] if cancelled else None)
