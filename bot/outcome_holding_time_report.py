"""Read-only holding-time and target-reachability report for Outcome fills."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def _bucket(seconds: float) -> str:
    if seconds < 300: return "under_5m"
    if seconds < 1800: return "5m_to_30m"
    if seconds < 7200: return "30m_to_2h"
    if seconds < 21600: return "2h_to_6h"
    return "6h_plus"


@dataclass(frozen=True)
class HoldingTimeReport:
    completed_round_trips: int
    holding_time_buckets: dict[str, int]
    average_holding_sec: float | None
    max_holding_sec: float | None
    net_return_mean: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "completed_round_trips": self.completed_round_trips,
            "holding_time_buckets": self.holding_time_buckets,
            "average_holding_sec": self.average_holding_sec,
            "max_holding_sec": self.max_holding_sec,
            "net_return_mean": self.net_return_mean,
        }


class OutcomeHoldingTimeAnalyzer:
    """FIFO-match real fills; no exchange or journal writes."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    def report(self, *, period: str = "1d") -> HoldingTimeReport:
        with sqlite3.connect(f"file:{Path(self.db_path).resolve()}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                """
                SELECT ts, instrument_id, side, price, qty, commission_usdc, payload_json
                FROM order_events WHERE event_type='ORDER_FILLED'
                ORDER BY ts, id
                """
            ).fetchall()
        lots: dict[str, deque[tuple[Decimal, Decimal, datetime]]] = defaultdict(deque)
        holds: list[float] = []
        returns: list[float] = []
        for ts, coin, side, price, qty, commission, raw in rows:
            try:
                payload = json.loads(raw or "{}")
                if payload.get("period") != period:
                    continue
                timestamp = datetime.fromisoformat(str(ts))
                px, size = Decimal(str(price)), Decimal(str(qty))
                fee = Decimal(str(commission or 0))
                if px <= 0 or size <= 0:
                    continue
            except (ValueError, TypeError, ArithmeticError, json.JSONDecodeError):
                continue
            if side == "BUY":
                lots[str(coin)].append([size, px, fee, timestamp])
                continue
            if side != "SELL":
                continue
            remaining = size
            while remaining > 0 and lots[str(coin)]:
                lot_size, buy_px, buy_fee, buy_ts = lots[str(coin)][0]
                used = min(remaining, lot_size)
                proportion = used / size
                cost = used * buy_px + buy_fee * (used / lot_size)
                proceeds = used * px - fee * proportion
                holds.append((timestamp - buy_ts).total_seconds())
                if cost > 0:
                    returns.append(float(proceeds / cost - Decimal("1")))
                remaining -= used
                lot_size -= used
                if lot_size <= 0:
                    lots[str(coin)].popleft()
                else:
                    lots[str(coin)][0][0] = lot_size
        buckets: dict[str, int] = {key: 0 for key in ("under_5m", "5m_to_30m", "30m_to_2h", "2h_to_6h", "6h_plus")}
        for seconds in holds:
            buckets[_bucket(seconds)] += 1
        return HoldingTimeReport(
            len(holds), buckets,
            sum(holds) / len(holds) if holds else None,
            max(holds) if holds else None,
            sum(returns) / len(returns) if returns else None,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Outcome holding-time report")
    parser.add_argument("--db", default="logs/outcome_shadow.db")
    parser.add_argument("--period", default="1d")
    args = parser.parse_args()
    print(json.dumps(OutcomeHoldingTimeAnalyzer(args.db).report(period=args.period).as_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
