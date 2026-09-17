"""Non-blocking, reconciled maker lifecycle for one Outcome side.

``tick`` performs at most one exchange mutation.  It is safe for a strategy
loop to call repeatedly: account state is the source of truth, not process
memory.  The only order styles emitted are ALO limit orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Mapping, Protocol

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_execution_gateway import OutcomeExecutionGateway
from bot.outcome_order_mutation import cancel_and_confirm

if TYPE_CHECKING:
    from monitoring.trade_journal_db import TradeJournalDB
    from bot.outcome_exit_lifecycle import OutcomeExitLifecycleStore

from bot.outcome_sdk_sidecar import OutcomeSdkAmbiguousExecutionError


class AccountReader(Protocol):
    def get_spot_clearinghouse_state_sync(self, user: str) -> dict[str, Any]: ...
    def get_open_orders_sync(self, user: str) -> list[dict[str, Any]]: ...
    def get_user_fills_sync(self, user: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class MakerTickResult:
    state: Literal["flat", "buy_resting", "sell_resting", "buy_placed", "sell_placed", "blocked"]
    detail: str
    order_id: str | None = None
    audit: dict[str, str] | None = None


class OutcomeMakerStateMachine:
    """Venue state machine; strategy decides only whether entry is permitted."""

    def __init__(self, *, account: AccountReader, gateway: OutcomeExecutionGateway, wallet: str,
                 journal: "TradeJournalDB | None" = None,
                 exit_lifecycle_store: "OutcomeExitLifecycleStore | None" = None) -> None:
        self.account = account
        self.gateway = gateway
        self.wallet = wallet
        self.journal = journal
        self.exit_lifecycle_store = exit_lifecycle_store

    def _invalidate_account_reads(self) -> None:
        invalidate = getattr(self.account, "invalidate", None)
        if callable(invalidate):
            invalidate()

    def _fresh_orders_before_submit(self) -> list[dict[str, Any]]:
        """Require REST account truth immediately before creating exposure."""
        force = getattr(self.account, "force_open_orders_reconciliation_sync", None)
        if callable(force):
            return force(self.wallet)
        return self.account.get_open_orders_sync(self.wallet)

    @staticmethod
    def _coin_position(state: dict[str, Any], coin: str) -> tuple[Decimal, Decimal]:
        # HIP-4 books use ``#<id>`` while spot-clearinghouse inventory is
        # returned as ``+<id>``.  Fixtures may use either representation.
        balance_coin = "+" + coin[1:] if coin.startswith("#") else coin
        for balance in state.get("balances", []):
            if balance.get("coin") in {coin, balance_coin}:
                return Decimal(str(balance.get("total", "0"))), Decimal(str(balance.get("entryNtl", "0")))
        return Decimal("0"), Decimal("0")

    def _orders(self, coin: str) -> list[dict[str, Any]]:
        return [order for order in self.account.get_open_orders_sync(self.wallet) if order.get("coin") == coin]

    @staticmethod
    def _best(levels: list[dict[str, Any]], label: str) -> Decimal:
        if not levels:
            raise RuntimeError(f"Outcome book has no {label}")
        price = Decimal(str(levels[0]["price"]))
        if not Decimal("0") < price < Decimal("1"):
            raise RuntimeError(f"invalid best {label}: {price}")
        return price

    @staticmethod
    def _target_sell_price(
        *, avg_entry_price: Decimal, minimum_return_pct: Decimal | None, maker_close_fee_rate: Decimal | None,
    ) -> Decimal | None:
        if minimum_return_pct is None:
            return None
        if avg_entry_price <= 0 or minimum_return_pct < 0 or maker_close_fee_rate is None:
            return None
        target = avg_entry_price * (Decimal("1") + minimum_return_pct) / (Decimal("1") - maker_close_fee_rate)
        if target >= Decimal("0.99999"):
            return None
        return target

    @staticmethod
    def _loss_reprice_floor(
        *, avg_entry_price: Decimal, loss_reprice_pct: Decimal | None, maker_close_fee_rate: Decimal | None,
    ) -> Decimal | None:
        if loss_reprice_pct is None:
            return None
        if avg_entry_price <= 0 or loss_reprice_pct < 0 or maker_close_fee_rate is None:
            return None
        floor = avg_entry_price * (Decimal("1") - loss_reprice_pct) / (Decimal("1") - maker_close_fee_rate)
        return floor if Decimal("0") < floor < Decimal("0.99999") else None

    def _fill_vwap_for_inventory(self, *, coin: str, inventory: Decimal) -> Decimal | None:
        """Reconstruct the remaining long inventory from exchange-confirmed fills.

        ``entryNtl`` is useful account metadata, but a take-profit calculation
        must not silently rely on it when it disagrees with the exchange's
        actual fills.  FIFO reconstruction is intentionally conservative: a
        partial or incomplete fill history returns ``None`` rather than an
        unverifiable price.
        """
        get_fills = getattr(self.account, "get_user_fills_sync", None)
        if inventory <= 0:
            return None
        lots: list[list[Decimal]] = []
        try:
            fills = sorted(get_fills(self.wallet), key=lambda fill: int(fill.get("time", 0))) if callable(get_fills) else []
        except (TypeError, ValueError):
            fills = []
        for raw in fills:
            if str(raw.get("coin")) != coin:
                continue
            try:
                quantity = Decimal(str(raw.get("sz")))
                price = Decimal(str(raw.get("px")))
            except (ArithmeticError, ValueError):
                continue
            if quantity <= 0 or not Decimal("0") < price < Decimal("1"):
                continue
            side = str(raw.get("side", "")).upper()
            if side in {"B", "BUY"}:
                lots.append([quantity, price])
            elif side in {"A", "SELL"}:
                remaining = quantity
                while remaining > 0 and lots:
                    lot = lots[0]
                    consumed = min(remaining, lot[0])
                    lot[0] -= consumed
                    remaining -= consumed
                    if lot[0] == 0:
                        lots.pop(0)
                if remaining > 0:
                    lots = []
                    break
        reconstructed_quantity = sum((lot[0] for lot in lots), Decimal("0"))
        if reconstructed_quantity == inventory:
            return sum((lot[0] * lot[1] for lot in lots), Decimal("0")) / inventory
        # ``userFills`` can be temporarily incomplete after a restart or API
        # window change.  A durable locally verified stream may repair that
        # only when it exactly reconciles the current exchange inventory.
        if self.journal is not None:
            return self.journal.verified_outcome_fill_vwap_for_inventory(coin=coin, inventory=inventory)
        return None

    def tick(
        self, *, market: OutcomeMarketSpec, side_index: int, entry_permitted: bool,
        minimum_return_pct: Decimal | None = None, maker_close_fee_rate: Decimal | None = None,
        loss_reprice_pct: Decimal | None = None,
        loss_band_authorized: bool = False,
        entry_audit: Mapping[str, object] | None = None,
        entry_max_submit_price: Decimal | None = None,
        entry_min_submit_price: Decimal | None = None,
        entry_requested_shares: Decimal | None = None,
        entry_max_notional: Decimal | None = None,
    ) -> MakerTickResult:
        coin = self.gateway.outcome_coin(market, side_index)
        inventory, entry_notional = self._coin_position(self.account.get_spot_clearinghouse_state_sync(self.wallet), coin)
        orders = self._orders(coin)
        buys = [order for order in orders if order.get("side") == "B"]
        sells = [order for order in orders if order.get("side") == "A"]

        if inventory > 0:
            account_entry_vwap = (entry_notional / inventory) if inventory else Decimal("0")
            fill_entry_vwap = self._fill_vwap_for_inventory(coin=coin, inventory=inventory)
            # Calibration exits must use exchange-confirmed fill VWAP.  This
            # prevents a stale or semantically different ``entryNtl`` value
            # from generating a sell below the configured net target.
            if minimum_return_pct is not None and fill_entry_vwap is None:
                return MakerTickResult(
                    "blocked", "cannot verify fill VWAP for calibration exit; explicit reconciliation required",
                    audit={"account_entry_vwap": str(account_entry_vwap), "inventory": str(inventory)},
                )
            avg_entry = fill_entry_vwap or account_entry_vwap
            audit = {
                "inventory": str(inventory),
                "account_entry_vwap": str(account_entry_vwap),
                "fill_entry_vwap": str(fill_entry_vwap) if fill_entry_vwap is not None else "unavailable",
                "pricing_basis": "exchange_fill_vwap" if fill_entry_vwap is not None else "account_entry_ntl",
            }
            profit_target = self._target_sell_price(
                avg_entry_price=avg_entry, minimum_return_pct=minimum_return_pct, maker_close_fee_rate=maker_close_fee_rate,
            )
            loss_floor = self._loss_reprice_floor(
                avg_entry_price=avg_entry, loss_reprice_pct=loss_reprice_pct, maker_close_fee_rate=maker_close_fee_rate,
            )
            if minimum_return_pct is not None and profit_target is None:
                return MakerTickResult("blocked", "calibration take-profit target is not executable; inventory requires explicit reconciliation", audit=audit)
            # A stale or manual same-coin SELL must never become a substitute
            # for the lifecycle-owned protection.  In particular, do not use
            # ``next(...)`` here: picking the first covering order would make
            # a later loss-band cancellation capable of cancelling the wrong
            # order.  The durable lifecycle store accepts exactly one
            # account-truth SELL only.
            covering = None
            if sells:
                if len(sells) != 1:
                    return MakerTickResult(
                        "blocked", "multiple same-coin sells require explicit reconciliation", audit=audit,
                    )
                candidate = sells[0]
                if Decimal(str(candidate.get("sz", "0"))) < inventory:
                    return MakerTickResult(
                        "blocked", "existing sell does not cover verified inventory; explicit reconciliation required", audit=audit,
                    )
                if self.exit_lifecycle_store is not None:
                    owned = self.exit_lifecycle_store.reconcile_owned_sell(
                        wallet=self.wallet, outcome_id=market.outcome_id, coin=coin,
                        inventory=inventory, open_orders=orders,
                    )
                    if owned is None or owned.order_id != str(candidate.get("oid")):
                        return MakerTickResult(
                            "blocked", "existing sell is not uniquely lifecycle-owned; reconciliation required",
                            str(candidate.get("oid")), audit,
                        )
                covering = candidate
            if covering:
                # ALO cannot guarantee an immediate stop.  Once the midpoint
                # has crossed the configured loss threshold, cancel the old
                # profit quote once and let the next tick rest a new maker-only
                # protection price.  Never cross the bid or submit a taker.
                book = self.gateway.fetch_order_book(market=market, side_index=side_index)
                bid, ask = self._best(book["bids"], "bid"), self._best(book["asks"], "ask")
                midpoint = (bid + ask) / Decimal("2")
                loss_triggered = bool(
                    loss_band_authorized and loss_floor is not None
                    and midpoint <= avg_entry * (Decimal("1") - loss_reprice_pct)
                )
                existing_price = Decimal(str(covering.get("limitPx", covering.get("px", "0"))))
                if loss_triggered and existing_price > loss_floor:
                    cancellation = cancel_and_confirm(
                        account=self.account, gateway=self.gateway, wallet=self.wallet,
                        market=market, side_index=side_index, order_id=str(covering["oid"]),
                    )
                    if not cancellation.confirmed:
                        return MakerTickResult(
                            "blocked", f"loss threshold crossed; cancellation requires reconciliation: {cancellation.reason}",
                            str(covering["oid"]), audit,
                        )
                    return MakerTickResult("blocked", "loss threshold crossed; cancelled old profit sell for maker-only protection reprice", str(covering["oid"]), audit)
                return MakerTickResult("sell_resting", "inventory is protected by owned ALO sell", str(covering.get("oid")), audit)
            if buys:
                # Never add exposure after any fill.  The next tick will see
                # the cancelled remainder and then post the protective sale.
                order = buys[0]
                cancellation = cancel_and_confirm(
                    account=self.account, gateway=self.gateway, wallet=self.wallet,
                    market=market, side_index=side_index, order_id=str(order["oid"]),
                )
                if not cancellation.confirmed:
                    return MakerTickResult(
                        "blocked", f"partial-fill buy cancellation requires reconciliation: {cancellation.reason}",
                        str(order["oid"]), audit,
                    )
                return MakerTickResult("blocked", "cancelled unfilled buy remainder before protective sell", str(order["oid"]), audit)
            # There is no safe generic fallback sell.  In particular, using
            # current best ask here can realize a loss while the journal calls
            # it a take-profit.  Cancel any residual buy first, then require
            # an explicit policy before creating a new sell.
            if minimum_return_pct is None:
                return MakerTickResult(
                    "blocked", "inventory has no explicit verified exit policy; refusing best-ask fallback sell",
                    audit=audit,
                )
            book = self.gateway.fetch_order_book(market=market, side_index=side_index)
            bid, ask = self._best(book["bids"], "bid"), self._best(book["asks"], "ask")
            midpoint = (bid + ask) / Decimal("2")
            loss_triggered = bool(
                loss_band_authorized and loss_floor is not None
                and midpoint <= avg_entry * (Decimal("1") - loss_reprice_pct)
            )
            target = loss_floor if loss_triggered else profit_target
            assert target is not None or minimum_return_pct is None
            target = target or ask
            requested_price = max(ask, target)
            audit.update({
                "requested_price": str(requested_price),
                "take_profit_price": str(profit_target) if profit_target is not None else "unavailable",
                "loss_reprice_floor": str(loss_floor) if loss_floor is not None else "unavailable",
                "exit_mode": "loss_band" if loss_triggered else "take_profit",
            })
            if any(order.get("coin") == coin for order in self._fresh_orders_before_submit()):
                return MakerTickResult(
                    "blocked", "pre-submit REST found an existing order; reconciliation required", audit=audit,
                )
            intent: tuple[str, int] | None = None
            if self.exit_lifecycle_store is not None:
                intent = self.exit_lifecycle_store.record_submit_intent(
                    wallet=self.wallet, outcome_id=market.outcome_id, coin=coin,
                    order_kind="initial_protective_alo", price=requested_price, shares=inventory,
                    old_order_id=None, replacement_count=0, intended_state="SELL_RESTING",
                    context={"pricing_basis": audit.get("pricing_basis"), "exit_mode": audit.get("exit_mode")},
                )
                if intent is None:
                    return MakerTickResult("blocked", "durable protective SELL intent unavailable", audit=audit)
                audit["exit_intent_id"], audit["exit_intent_event_id"] = intent
            try:
                result = self.gateway.place_alo(
                    market=market,
                    side_index=side_index,
                    is_buy=False,
                    price=requested_price,
                    requested_shares=inventory,
                    # ``inventory`` came from this tick's wallet reconciliation;
                    # allow the official SDK's documented residual-close exception.
                    reduce_only=True,
                )
            except OutcomeSdkAmbiguousExecutionError as exc:
                if self.exit_lifecycle_store is None or intent is None:
                    raise
                intent_id, intent_event_id = intent
                ambiguity_id = self.exit_lifecycle_store.record_ambiguous_submit(
                    wallet=self.wallet, outcome_id=market.outcome_id, coin=coin,
                    intent_id=intent_id, intent_event_id=intent_event_id,
                    order_kind="initial_protective_alo", price=requested_price, shares=inventory,
                    old_order_id=None, replacement_count=0, intended_state="SELL_RESTING",
                    sidecar_request_id=exc.request_id, command=exc.command, detail=str(exc),
                )
                if ambiguity_id is None:
                    raise RuntimeError("ambiguous protective SELL could not persist reconciliation fence") from exc
                audit["exit_ambiguity_event_id"] = ambiguity_id
                return MakerTickResult(
                    "blocked", "ambiguous protective SELL; account-truth reconciliation required", audit=audit,
                )
            except Exception as exc:
                if self.exit_lifecycle_store is not None and intent is not None:
                    self.exit_lifecycle_store.finalize_submit_intent(
                        intent_id=intent[0], order_id=None,
                        reason=f"safe_sdk_rejection:{type(exc).__name__}",
                    )
                raise
            self._invalidate_account_reads()
            acknowledged_order_id = str(result.get("orderId") or "")
            if not acknowledged_order_id:
                if self.exit_lifecycle_store is not None and intent is not None:
                    self.exit_lifecycle_store.record_ambiguous_submit(
                        wallet=self.wallet, outcome_id=market.outcome_id, coin=coin,
                        intent_id=intent[0], intent_event_id=intent[1],
                        order_kind="initial_protective_alo", price=requested_price, shares=inventory,
                        old_order_id=None, replacement_count=0, intended_state="SELL_RESTING",
                        sidecar_request_id="sdk_ack_missing_order_id", command="place_limit_order",
                        detail="acknowledged initial protective SELL has no order id",
                    )
                return MakerTickResult("blocked", "initial protective SELL ACK lacks order identity; reconciliation required", audit=audit)
            # Initial protection and later replacements share the same
            # authority rule: the SDK response is transport evidence, while a
            # fresh account snapshot proves the currently resting OID.  This
            # catches a rotated/stale ACK before the runtime records ownership.
            if self.exit_lifecycle_store is not None and intent is not None:
                refreshed_orders = self._fresh_orders_before_submit()
                exact_ack = [
                    row for row in refreshed_orders
                    if (str(row.get("oid")) == acknowledged_order_id and row.get("coin") == coin
                        and row.get("side") == "A" and Decimal(str(row.get("sz", "0"))) >= inventory)
                ]
                same_coin_sells = [
                    row for row in refreshed_orders if row.get("coin") == coin and row.get("side") == "A"
                ]
                if len(exact_ack) != 1 or len(same_coin_sells) != 1:
                    ambiguity_id = self.exit_lifecycle_store.record_ambiguous_submit(
                        wallet=self.wallet, outcome_id=market.outcome_id, coin=coin,
                        intent_id=intent[0], intent_event_id=intent[1],
                        order_kind="initial_protective_alo", price=requested_price, shares=inventory,
                        old_order_id=None, replacement_count=0, intended_state="SELL_RESTING",
                        sidecar_request_id="sdk_ack_oid_unverified", command="place_limit_order",
                        detail="acknowledged initial protective SELL OID absent or ambiguous in fresh account truth",
                    )
                    if ambiguity_id is None:
                        raise RuntimeError("initial protective SELL could not persist account-truth fence")
                    status, adopted = self.exit_lifecycle_store.reconcile_ambiguous_submit(
                        wallet=self.wallet, outcome_id=market.outcome_id, coin=coin,
                        inventory=inventory, open_orders=refreshed_orders,
                    )
                    if status == "adopted" and adopted is not None:
                        return MakerTickResult(
                            "sell_resting", "account_truth_adopted_initial_protective_sell_oid",
                            adopted.order_id, audit,
                        )
                    return MakerTickResult(
                        "blocked", "initial protective SELL ACK OID not verified from account truth", audit=audit,
                    )
            timing = getattr(self.gateway, "last_sidecar_timing", None)
            if isinstance(timing, dict):
                audit["sdk_submit_timing"] = dict(timing)
            detail = "placed maker-only loss-band protection sell" if loss_triggered else "placed net take-profit ALO sell for reconciled inventory"
            return MakerTickResult("sell_placed", detail, acknowledged_order_id, audit)

        if sells:
            return MakerTickResult("blocked", "wallet has sell order without inventory; reconcile explicitly", str(sells[0].get("oid")))
        if buys:
            return MakerTickResult("buy_resting", "owned ALO buy remains resting", str(buys[0].get("oid")))
        if not entry_permitted:
            return MakerTickResult("flat", "strategy did not permit a new entry")

        book = self.gateway.fetch_order_book(market=market, side_index=side_index)
        bid = self._best(book["bids"], "bid")
        requested_shares = entry_requested_shares
        if requested_shares is not None and (requested_shares <= 0 or requested_shares != requested_shares.to_integral_value()):
            return MakerTickResult("blocked", "entry requested shares must be positive whole inventory")
        if entry_max_notional is not None and requested_shares is not None and bid * requested_shares > entry_max_notional:
            return MakerTickResult("flat", "entry submit notional exceeds configured cap after price drift")
        # The no-trade band is an execution constraint, not merely a
        # decision-time heuristic.  A fresh sidecar/L2 read can move lower
        # between strategy selection and the actual ALO submit; do not turn a
        # valid 0.55 decision into a 0.545 opening order.
        if entry_min_submit_price is not None and bid < entry_min_submit_price:
            audit = dict(entry_audit or {})
            audit.update({
                "entry_submit_bid": str(bid),
                "entry_min_submit_price": str(entry_min_submit_price),
            })
            return MakerTickResult("flat", "entry submit price fell below configured minimum", audit=audit)
        if entry_max_submit_price is not None and bid > entry_max_submit_price:
            audit = dict(entry_audit or {})
            decision_bid = audit.get("entry_bid_at_decision")
            audit.update({
                "entry_submit_bid": str(bid), "entry_max_submit_bid": str(entry_max_submit_price),
                "tier_b_submit_drift_bps": str(
                    (bid / Decimal(str(decision_bid)) - Decimal("1")) * Decimal("10000")
                ) if decision_bid is not None and Decimal(str(decision_bid)) > 0 else None,
            })
            return MakerTickResult("flat", "entry submit price drift exceeds calibrated ceiling", audit=audit)
        if any(order.get("coin") == coin for order in self._fresh_orders_before_submit()):
            return MakerTickResult(
                "blocked", "pre-submit REST found an existing order; reconciliation required", audit=dict(entry_audit or {}),
            )
        result = self.gateway.place_alo(
            market=market, side_index=side_index, is_buy=True, price=bid,
            requested_shares=requested_shares,
        )
        self._invalidate_account_reads()
        # The ledger records this ``audit`` payload on the same durable
        # ORDER_SUBMIT row as the exchange order id.  In particular, a live
        # strategy's chosen exit target must not exist only in a later,
        # best-effort strategy event: a process crash after order acceptance
        # would otherwise make the order impossible to audit accurately.
        return MakerTickResult(
            "buy_placed", "placed first-level ALO buy", str(result["orderId"]),
            audit={
                **dict(entry_audit or {}), "entry_submit_bid": str(bid),
                "entry_submitted_shares": str(result.get("shares")),
                "entry_submit_drift_bps": str(
                    (bid / Decimal(str((entry_audit or {}).get("entry_bid_at_decision"))) - Decimal("1")) * Decimal("10000")
                ) if (entry_audit or {}).get("entry_bid_at_decision") is not None
                and Decimal(str((entry_audit or {}).get("entry_bid_at_decision"))) > 0 else None,
                "sdk_submit_timing": dict(getattr(self.gateway, "last_sidecar_timing", {}) or {}),
            },
        )
