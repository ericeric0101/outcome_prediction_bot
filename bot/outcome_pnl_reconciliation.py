"""Canonical FIFO PnL and settlement reconciliation for Outcome fills.

No BTC price, UI PnL, or inferred winner is accepted as evidence.  Ordinary
sell PnL is derived only from deduplicated official ``userFills`` already in
the journal.  Remaining inventory is closed only when the official SDK says a
market settled *and* the winning payout or losing zero balance corroborates it.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping

from bot.outcome_event_bridge import parse_outcome_balance_coin, parse_outcome_coin
from bot.outcome_settlement import OutcomeSettlement
from monitoring.trade_journal_db import TradeJournalDB


@dataclass
class _Lot:
    trade_id: str
    qty: Decimal
    price: Decimal
    fee_remaining: Decimal


@dataclass(frozen=True)
class _Fill:
    event_id: int
    trade_id: str
    outcome_id: int
    side_index: int
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


class OutcomePnLReconciler:
    """Read official fill facts and write idempotent canonical PnL lots."""

    def __init__(self, journal: TradeJournalDB, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id

    def _fills(self) -> list[_Fill]:
        with sqlite3.connect(self.journal.db_path) as conn:
            rows = conn.execute(
                """SELECT id,payload_json,side,price,qty,commission_usdc FROM order_events
                   WHERE event_type='ORDER_FILLED' ORDER BY id ASC"""
            ).fetchall()
        output: list[_Fill] = []
        for event_id, raw, side, price, qty, fee in rows:
            try:
                payload = json.loads(raw or "{}")
                if payload.get("venue") != "hyperliquid_outcome" or payload.get("actual_fill") is not True:
                    continue
                outcome_id = int(payload["outcome_id"])
                side_index = int(payload["side_index"])
                trade_id = str(payload["trade_id"])
                if side not in {"BUY", "SELL"} or not trade_id or price is None or qty is None:
                    continue
                output.append(_Fill(event_id, trade_id, outcome_id, side_index, str(side), _d(qty), _d(price), _d(fee or 0)))
            except (KeyError, TypeError, ValueError, ArithmeticError, json.JSONDecodeError):
                continue
        return output

    def _open_lots(self) -> tuple[dict[tuple[int, int], deque[_Lot]], int]:
        books: dict[tuple[int, int], deque[_Lot]] = defaultdict(deque)
        written = 0
        for fill in self._fills():
            book = books[(fill.outcome_id, fill.side_index)]
            if fill.side == "BUY":
                book.append(_Lot(fill.trade_id, fill.qty, fill.price, fill.fee))
                continue
            remaining, sell_fee = fill.qty, fill.fee
            while remaining > 0 and book:
                opening = book[0]
                matched = min(remaining, opening.qty)
                opening_fee = opening.fee_remaining * matched / opening.qty
                close_fee = sell_fee * matched / remaining
                cost = opening.price * matched + opening_fee
                proceeds = fill.price * matched - close_fee
                if self.journal.record_outcome_realized_pnl_lot(
                    close_trade_id=fill.trade_id, open_trade_id=opening.trade_id,
                    outcome_id=fill.outcome_id, side_index=fill.side_index, close_kind="sell",
                    quantity=matched, cost_usdc=cost, proceeds_usdc=proceeds,
                    source={"fill_provenance": "hyperliquid_userFills", "close_order_event_id": fill.event_id},
                ):
                    written += 1
                opening.qty -= matched
                opening.fee_remaining -= opening_fee
                remaining -= matched
                sell_fee -= close_fee
                if opening.qty == 0:
                    book.popleft()
            # A sell with no local matching buy remains intentionally unmatched:
            # it may be a position opened before this journal started.
        return books, written

    def reconcile_sells(self) -> int:
        _, written = self._open_lots()
        return written

    def unresolved_outcome_ids(self) -> set[int]:
        books, _ = self._open_lots()
        return {
            outcome_id for (outcome_id, _side), lots in books.items()
            if sum((lot.qty for lot in lots), Decimal("0")) > 0
        }

    @staticmethod
    def _winner(settlement: OutcomeSettlement) -> int | None:
        if not settlement.settled or settlement.settle_fraction not in {Decimal("0"), Decimal("1")}:
            return None
        # HIP-4's official ``settleFraction`` is the YES/side-0 redemption
        # fraction.  Refuse non-binary values rather than guessing a winner.
        return 0 if settlement.settle_fraction == Decimal("1") else 1

    @staticmethod
    def _settlement_payouts(raw_fills: Iterable[Mapping[str, Any]], outcome_id: int) -> dict[int, Decimal]:
        payouts: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        for raw in raw_fills:
            try:
                if str(raw.get("dir", "")).strip().lower() != "settlement":
                    continue
                parsed_outcome_id, side_index = parse_outcome_coin(str(raw.get("coin", "")))
                if parsed_outcome_id != outcome_id:
                    continue
                price, qty = _d(raw["px"]), _d(raw["sz"])
                if price < 0 or price > 1 or qty <= 0:
                    continue
                payouts[side_index] += price * qty - _d(raw.get("fee") or 0)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
        return dict(payouts)

    @staticmethod
    def _zero_balance_sides(clearinghouse: Mapping[str, Any], outcome_id: int) -> set[int]:
        if not isinstance(clearinghouse, Mapping) or not isinstance(clearinghouse.get("balances"), list):
            # An empty/malformed HTTP payload is not evidence that a losing
            # token balance reached zero.
            return set()
        present: set[int] = set()
        for raw in clearinghouse["balances"]:
            try:
                found_id, side = parse_outcome_balance_coin(str(raw.get("coin", "")))
                if found_id == outcome_id and _d(raw.get("total", raw.get("balance", "0"))) > 0:
                    present.add(side)
            except (AttributeError, ValueError, ArithmeticError):
                continue
        return {0, 1} - present

    def reconcile_settlement(
        self, *, settlement: OutcomeSettlement, raw_fills: Iterable[Mapping[str, Any]],
        clearinghouse: Mapping[str, Any],
    ) -> str:
        winner = self._winner(settlement)
        if winner is None:
            return "blocked_nonbinary_or_unconfirmed_sdk_settlement"
        books, _ = self._open_lots()
        payouts = self._settlement_payouts(raw_fills, settlement.market_id)
        zero_sides = self._zero_balance_sides(clearinghouse, settlement.market_id)
        unmatched = [(side, sum((lot.qty for lot in books[(settlement.market_id, side)]), Decimal("0"))) for side in (0, 1)]
        for side, qty in unmatched:
            if qty <= 0:
                continue
            if side == winner and payouts.get(side, Decimal("0")) < qty:
                return "pending_winning_payout_evidence"
            if side != winner and side not in zero_sides:
                return "pending_losing_zero_balance_evidence"

        total_cost = total_payout = Decimal("0")
        for side, qty in unmatched:
            if qty <= 0:
                continue
            close_trade_id = f"settlement:{settlement.market_id}:{side}"
            payout_per_share = Decimal("1") if side == winner else Decimal("0")
            book = books[(settlement.market_id, side)]
            while book:
                lot = book.popleft()
                cost = lot.price * lot.qty + lot.fee_remaining
                proceeds = payout_per_share * lot.qty
                self.journal.record_outcome_realized_pnl_lot(
                    close_trade_id=close_trade_id, open_trade_id=lot.trade_id,
                    outcome_id=settlement.market_id, side_index=side, close_kind="settlement",
                    quantity=lot.qty, cost_usdc=cost, proceeds_usdc=proceeds,
                    source={"official_sdk_settle_fraction": str(settlement.settle_fraction),
                            "payout_observed_usdc": str(payouts.get(side, Decimal("0"))),
                            "zero_balance_confirmed": side in zero_sides},
                )
                total_cost += cost
                total_payout += proceeds
        payload = {
            "venue": "hyperliquid_outcome", "outcome_id": settlement.market_id,
            # Do not label Outcome side indices UP/DOWN without the saved
            # market specification.  The side index itself is the canonical
            # settlement fact used by the accounting ledger.
            "outcome": f"SIDE_{winner}", "winning_side_index": winner,
            "settlement_source": "official_sdk_settled_outcome+official_account_evidence",
            "settle_fraction": str(settlement.settle_fraction),
            "redeem_value_usdc": float(total_payout), "inventory_cost_usdc": float(total_cost),
            "settlement_pnl_usdc": float(total_payout - total_cost),
            "payout_evidence": {"payouts_usdc": {str(k): str(v) for k, v in payouts.items()},
                                "zero_balance_sides": sorted(zero_sides)},
        }
        wrote = self.journal.record_outcome_market_settlement_once(
            run_id=self.run_id, outcome_id=settlement.market_id, winning_side_index=winner,
            settlement_source=payload["settlement_source"], settle_fraction=settlement.settle_fraction,
            payout_evidence=payload["payout_evidence"], payload=payload,
        )
        return "recorded" if wrote else "already_recorded"
