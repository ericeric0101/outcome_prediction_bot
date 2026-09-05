#!/usr/bin/env python3
"""
trade_journal.db 的 PnL 對帳報表（僅分析，不影響交易行為）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from monitoring.trade_journal_db import TradeJournalDB


def _to_iso_utc(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _fmt_num(v: Optional[float], digits: int = 6) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="交易 PnL 與結算 PnL 對帳報表")
    parser.add_argument("--db", default="./logs/trade_journal.db", help="SQLite 資料庫路徑")
    parser.add_argument("--run-id", default=None, help="只分析指定 run_id")
    parser.add_argument("--hours", type=int, default=6, help="回溯小時數（<=0 表示全期間）")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"找不到資料庫：{db_path}")
        return 1

    # Apply additive journal migrations before querying canonical Outcome lots.
    TradeJournalDB(str(db_path))

    cutoff = _to_iso_utc(args.hours) if args.hours and args.hours > 0 else None

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    order_where = []
    order_params = []
    strategy_where = []
    strategy_params = []

    if args.run_id:
        order_where.append("run_id = ?")
        strategy_where.append("run_id = ?")
        order_params.append(args.run_id)
        strategy_params.append(args.run_id)
    if cutoff:
        order_where.append("ts >= ?")
        strategy_where.append("ts >= ?")
        order_params.append(cutoff)
        strategy_params.append(cutoff)

    order_clause = f"WHERE {' AND '.join(order_where)}" if order_where else ""
    strategy_clause = f"WHERE {' AND '.join(strategy_where)}" if strategy_where else ""

    realized = cur.execute(
        f"""
        SELECT
          COUNT(*) AS total_fill_rows,
          SUM(CASE WHEN json_extract(payload_json, '$.realized_net_usdc') IS NOT NULL THEN 1 ELSE 0 END) AS realized_rows,
          SUM(CASE WHEN json_extract(payload_json, '$.realized_net_usdc') > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN json_extract(payload_json, '$.realized_net_usdc') < 0 THEN 1 ELSE 0 END) AS losses,
          SUM(COALESCE(json_extract(payload_json, '$.realized_net_usdc'), 0)) AS realized_pnl_usdc,
          SUM(COALESCE(commission_usdc, 0)) AS fill_commission_usdc
        FROM order_events
        {order_clause} {"AND" if order_clause else "WHERE"} event_type = 'ORDER_FILLED'
        """,
        order_params,
    ).fetchone()

    settlement = cur.execute(
        f"""
        SELECT
          COUNT(*) AS settlement_rows,
          SUM(COALESCE(json_extract(payload_json, '$.settlement_pnl_usdc'), 0)) AS settlement_pnl_usdc,
          SUM(COALESCE(json_extract(payload_json, '$.redeem_value_usdc'), 0)) AS redeem_value_usdc,
          SUM(COALESCE(json_extract(payload_json, '$.inventory_cost_usdc'), 0)) AS inventory_cost_usdc
        FROM strategy_events
        {strategy_clause} {"AND" if strategy_clause else "WHERE"} event_type = 'MARKET_SETTLEMENT'
        """,
        strategy_params,
    ).fetchone()

    # Outcome fills are immutable venue facts.  Their realised PnL is stored
    # as idempotent FIFO allocations instead of mutating historical fill rows.
    # The table is journal-global, so a --run-id filter cannot safely pretend
    # it is a per-run result.
    canonical = cur.execute(
        """
        SELECT close_kind, COUNT(*) AS rows,
               SUM(CAST(realized_net_usdc AS REAL)) AS pnl_usdc,
               SUM(CAST(cost_usdc AS REAL)) AS cost_usdc,
               SUM(CAST(proceeds_usdc AS REAL)) AS proceeds_usdc
        FROM outcome_realized_pnl_lots
        GROUP BY close_kind
        """
    ).fetchall()

    quality = cur.execute(
        f"""
        SELECT
          COUNT(*) AS settlement_rows,
          SUM(
            CASE
              WHEN COALESCE(json_extract(payload_json, '$.spot'), 0) > 0
               AND COALESCE(json_extract(payload_json, '$.spot'), 0) < 2
               AND COALESCE(json_extract(payload_json, '$.strike'), 0) > 1000
              THEN 1 ELSE 0
            END
          ) AS tiny_spot_rows,
          SUM(CASE WHEN json_extract(payload_json, '$.outcome') = 'DOWN' THEN 1 ELSE 0 END) AS down_rows,
          MIN(COALESCE(json_extract(payload_json, '$.spot'), 0)) AS min_spot,
          MAX(COALESCE(json_extract(payload_json, '$.spot'), 0)) AS max_spot
        FROM strategy_events
        {strategy_clause} {"AND" if strategy_clause else "WHERE"} event_type = 'MARKET_SETTLEMENT'
        """,
        strategy_params,
    ).fetchone()

    auto_redeem = cur.execute(
        f"""
        SELECT
          COUNT(*) AS runs,
          SUM(CASE WHEN COALESCE(json_extract(payload_json, '$.apply'), 0) IN (1, '1', 'true', 'True') THEN 1 ELSE 0 END) AS apply_on_runs
        FROM strategy_events
        {strategy_clause} {"AND" if strategy_clause else "WHERE"} event_type = 'AUTO_REDEEM_RUN'
        """,
        strategy_params,
    ).fetchone()

    total_fill_rows = int(realized["total_fill_rows"] or 0)
    realized_rows = int(realized["realized_rows"] or 0)
    wins = int(realized["wins"] or 0)
    losses = int(realized["losses"] or 0)
    realized_pnl = float(realized["realized_pnl_usdc"] or 0.0)
    commissions = float(realized["fill_commission_usdc"] or 0.0)

    settlement_rows = int(settlement["settlement_rows"] or 0)
    settlement_pnl = float(settlement["settlement_pnl_usdc"] or 0.0)
    redeem_value = float(settlement["redeem_value_usdc"] or 0.0)
    inventory_cost = float(settlement["inventory_cost_usdc"] or 0.0)

    combined_pnl = realized_pnl + settlement_pnl
    win_rate = (wins / realized_rows * 100.0) if realized_rows > 0 else 0.0

    print("=" * 88)
    print("PnL 對帳報表")
    print("=" * 88)
    print(f"資料庫：{db_path}")
    print(f"run_id：{args.run_id or '（全部）'}")
    print(f"回溯小時：{args.hours if args.hours and args.hours > 0 else '（全期間）'}")
    if cutoff:
        print(f"UTC 起算時間：{cutoff}")

    print("\n[交易損益]")
    print(f"成交筆數（ORDER_FILLED）：{total_fill_rows}")
    print(f"已實現樣本數（realized 非空）：{realized_rows}")
    print(f"勝/負：{wins}/{losses}（勝率={win_rate:.2f}%）")
    print(f"已實現損益（USDC）：{_fmt_num(realized_pnl)}")
    print(f"成交手續費總額（USDC）：{_fmt_num(commissions)}")

    canonical_by_kind = {str(row[0]): row for row in canonical}
    canonical_sell = canonical_by_kind.get("sell")
    canonical_settlement = canonical_by_kind.get("settlement")
    canonical_sell_pnl = float(canonical_sell[2] or 0.0) if canonical_sell else 0.0
    canonical_settlement_pnl = float(canonical_settlement[2] or 0.0) if canonical_settlement else 0.0
    print("\n[Outcome canonical FIFO 對帳（全 journal；不以 run_id 切分）]")
    print(f"已賣出 lot 數：{int(canonical_sell[1] or 0) if canonical_sell else 0}")
    print(f"已賣出 canonical PnL（USDC）：{_fmt_num(canonical_sell_pnl)}")
    print(f"已結算 lot 數：{int(canonical_settlement[1] or 0) if canonical_settlement else 0}")
    print(f"已結算 canonical PnL（USDC）：{_fmt_num(canonical_settlement_pnl)}")
    print(f"Outcome canonical 合計（USDC）：{_fmt_num(canonical_sell_pnl + canonical_settlement_pnl)}")

    print("\n[結算損益]")
    print(f"結算事件筆數（MARKET_SETTLEMENT）：{settlement_rows}")
    print(f"結算損益合計（USDC）：{_fmt_num(settlement_pnl)}")
    print(f"redeem 金額合計（USDC）：{_fmt_num(redeem_value)}")
    print(f"庫存成本合計（USDC）：{_fmt_num(inventory_cost)}")

    print("\n[合併損益]")
    print(f"合併損益（交易 + 結算，USDC）：{_fmt_num(combined_pnl)}")

    print("\n[自動 Redeem 事件]")
    print(f"自動 redeem 執行次數：{int(auto_redeem['runs'] or 0)}")
    print(f"apply=ON 的執行次數：{int(auto_redeem['apply_on_runs'] or 0)}")

    q_rows = int(quality["settlement_rows"] or 0)
    tiny_spot_rows = int(quality["tiny_spot_rows"] or 0)
    down_rows = int(quality["down_rows"] or 0)
    min_spot = float(quality["min_spot"] or 0.0)
    max_spot = float(quality["max_spot"] or 0.0)

    print("\n[資料品質]")
    print(f"檢查的結算筆數：{q_rows}")
    print(f"結算 spot 範圍：min={_fmt_num(min_spot, 6)} max={_fmt_num(max_spot, 6)}")
    print(f"疑似尺度錯誤筆數（spot<2 且 strike>1000）：{tiny_spot_rows}")
    print(f"結果為 DOWN 的筆數：{down_rows}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
