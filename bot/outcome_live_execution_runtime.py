"""Single official execution runtime invoked by a strategy loop.

It is deliberately disabled unless the operator enables all execution gates.
It makes the account-recovery report a precondition for each state-machine
tick, so restart behaviour never depends on stale local order IDs.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
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
from bot.outcome_p3_calibration import OutcomeP3CalibrationConfig, choose_consensus_calibration_side


@dataclass(frozen=True)
class LiveExecutionResult:
    state: str
    detail: str
    order_id: str | None = None


class OutcomeLiveExecutionRuntime:
    def __init__(self, *, account: OutcomeClient, wallet: str, gateway: OutcomeExecutionGateway | None = None, risk_gate: OutcomePreTradeRiskGate | None = None, stream_health: OutcomeStreamHealth | None = None, ledger: OutcomeExecutionLedger | None = None, research_gate: OutcomeResearchGate | None = None) -> None:
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

    def _record(self, market: OutcomeMarketSpec, side_index: int, result: MakerTickResult) -> LiveExecutionResult:
        coin = self.machine.gateway.outcome_coin(market, side_index)
        if self.ledger:
            self.ledger.record_transition(market_id=market.outcome_id, coin=coin, result=result)
            fills = getattr(self.recovery.account, "get_user_fills_sync", lambda _user: [])(self.recovery.wallet)
            self.ledger.sync_fills(fills=fills, market_key=f"outcome:{market.outcome_id}", period=market.period)
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

    def _daily_calibration_entries(self) -> int:
        if not self.ledger:
            return 0
        with sqlite3.connect(self.ledger.journal.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM strategy_events WHERE event_type='OUTCOME_P3_CALIBRATION_ENTRY_PLACED' AND date(ts)=date('now')"
            ).fetchone()
        return int(row[0] or 0)

    def tick(self, *, market: OutcomeMarketSpec, side_index: int, entry_permitted: bool) -> LiveExecutionResult:
        if not self.enabled():
            return LiveExecutionResult("disabled", "automated execution requires OUTCOME_AUTOMATED_EXECUTION_ENABLED=1 and OUTCOME_SDK_EXECUTION_ENABLED=1")
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

    def tick_market(self, *, market: OutcomeMarketSpec, entry_side_index: int | None) -> LiveExecutionResult:
        """Advance existing exposure first; only a flat market accepts a signal."""
        if not self.enabled():
            return LiveExecutionResult("disabled", "automated execution requires OUTCOME_AUTOMATED_EXECUTION_ENABLED=1 and OUTCOME_SDK_EXECUTION_ENABLED=1")
        report = self.recovery.reconcile([market])
        if not report.safe_for_new_entry:
            return LiveExecutionResult("blocked", f"account recovery blocked execution: {report.reason}")
        active = [finding for finding in report.findings if finding.state != "flat"]
        if active:
            if len(active) != 1:
                return LiveExecutionResult("blocked", "multiple live Outcome sides require explicit reconciliation")
            side_index = 0 if active[0].coin == market.yes_coin else 1
            result = self.machine.tick(market=market, side_index=side_index, entry_permitted=False)
        elif entry_side_index is None:
            return LiveExecutionResult("flat", "no live exposure and no entry signal")
        else:
            health_error = self._stream_ready(market)
            if health_error:
                return health_error
            book = self.machine.gateway.fetch_order_book(market=market, side_index=entry_side_index)
            if not book.get("bids"):
                return LiveExecutionResult("blocked", "risk gate: no executable best bid")
            price = Decimal(str(book["bids"][0]["price"]))
            shares = whole_share_size(price)
            account = self.recovery.account
            balance_state = account.get_spot_clearinghouse_state_sync(self.recovery.wallet)
            risk = self.risk_gate.evaluate(
                balances=balance_state.get("balances", []), open_orders=account.get_open_orders_sync(self.recovery.wallet),
                price=price, shares=shares,
            )
            if not risk.allowed:
                return LiveExecutionResult("blocked", f"risk gate: {risk.reason}")
            research = self.research_gate.check(market.period)
            if not research.allowed:
                return LiveExecutionResult("blocked", research.reason)
            result = self.machine.tick(market=market, side_index=entry_side_index, entry_permitted=True)
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
        if not report.safe_for_new_entry:
            return LiveExecutionResult("blocked", f"account recovery blocked calibration: {report.reason}")
        config = OutcomeP3CalibrationConfig.from_env()
        active = [finding for finding in report.findings if finding.state != "flat"]
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
                "sampling_policy": "market_mid_consensus", "directional_signal_used": False,
            })
        return self._record(market, side_index, result)

    def cancel_resting_buys(self, *, market: OutcomeMarketSpec) -> LiveExecutionResult:
        """Reduce-only transition: cancel owned entries, never sell/take."""
        if not self.enabled():
            return LiveExecutionResult("disabled", "automated execution is disabled")
        report = self.recovery.reconcile([market])
        if not report.safe_for_new_entry:
            return LiveExecutionResult("blocked", f"account recovery blocked cancellation: {report.reason}")
        cancelled: list[str] = []
        for side_index, coin in enumerate((market.yes_coin, market.no_coin)):
            finding = next(item for item in report.findings if item.coin == coin)
            for order_id in finding.buy_order_ids:
                self.machine.gateway.cancel_owned_order(market=market, side_index=side_index, order_id=order_id)
                cancelled.append(order_id)
        return LiveExecutionResult("cancelled" if cancelled else "flat", "cancelled owned entry buys" if cancelled else "no owned entry buy", cancelled[0] if cancelled else None)
