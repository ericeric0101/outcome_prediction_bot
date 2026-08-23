# Polymarket ➜ Hyperliquid Outcome (HIP-4) 完整遷移架構計劃書

> **文件版本**：1.0.0  
> **建立日期**：2026-08-23  
> **目標系統**：Hyperliquid Outcome (HIP-4 Prediction Markets)  
> **基準架構**：依據 [`project_overview.md`](project_overview.md) 權威規範與現行量化做市代碼庫

---

## 零、 官方文檔與權威參考資源 (Official Docs & Resources)

### 1. Outcome 官方核心文檔 (Outcome Official Docs)
- 🌐 **Outcome 官方文檔總索引**: [https://docs.outcome.xyz/llms.txt](https://docs.outcome.xyz/llms.txt)
- 📊 **市場類型與規格 (Market Types)**: [https://docs.outcome.xyz/market-types](https://docs.outcome.xyz/market-types)
- ⚙️ **Outcome & Hyperliquid 架構簡介**: [https://docs.outcome.xyz/outcome-and-hyperliquid](https://docs.outcome.xyz/outcome-and-hyperliquid)
- 🪙 **代幣定價與概率機制 (Pricing & Probabilities)**: [https://docs.outcome.xyz/outcome-tokens-and-pricing](https://docs.outcome.xyz/outcome-tokens-and-pricing)
- 🔄 **週期性市場 (Recurring Markets)**: [https://docs.outcome.xyz/recurring-markets](https://docs.outcome.xyz/recurring-markets)
- 📖 **盤口與訂單簿 (Order Book)**: [https://docs.outcome.xyz/reading-the-order-book](https://docs.outcome.xyz/reading-the-order-book)
- 📝 **訂單類型 (Order Types & TIF)**: [https://docs.outcome.xyz/order-types](https://docs.outcome.xyz/order-types)
- ⏱️ **訂單生命週期 (Order Lifecycle)**: [https://docs.outcome.xyz/order-lifecycle](https://docs.outcome.xyz/order-lifecycle)
- ⚖️ **市場結算機制 (Resolution & Settlement)**: [https://docs.outcome.xyz/resolution-and-settlement](https://docs.outcome.xyz/resolution-and-settlement)
- 💰 **費率與折扣 (Fees & Discounts)**: [https://docs.outcome.xyz/fees](https://docs.outcome.xyz/fees)

### 2. Outcome HIP-4 SDK 開發參考 (SDK Reference & Guides)
- 📦 **SDK 簡介 (SDK Introduction)**: [https://docs.outcome.xyz/sdk/introduction](https://docs.outcome.xyz/sdk/introduction)
- 🔑 **身分驗證與 Agent Key (Authentication)**: [https://docs.outcome.xyz/sdk/concepts/authentication](https://docs.outcome.xyz/sdk/concepts/authentication)
- 🏷️ **幣種命名與 Asset ID (Coin Naming & Asset IDs)**: [https://docs.outcome.xyz/sdk/concepts/coin-naming](https://docs.outcome.xyz/sdk/concepts/coin-naming)
- 📉 **市場數據適配器 (Market Data Adapter)**: [https://docs.outcome.xyz/sdk/reference/market-data-adapter](https://docs.outcome.xyz/sdk/reference/market-data-adapter)
- 🛒 **交易執行適配器 (Trading Adapter)**: [https://docs.outcome.xyz/sdk/reference/trading-adapter](https://docs.outcome.xyz/sdk/reference/trading-adapter)
- 🔀 **代幣轉換原語 (Conversions: Split/Merge/Negate)**: [https://docs.outcome.xyz/sdk/guides/conversions](https://docs.outcome.xyz/sdk/guides/conversions)
- 📡 **WebSocket 即時串流 (Real-time WebSocket Data)**: [https://docs.outcome.xyz/sdk/guides/real-time-data](https://docs.outcome.xyz/sdk/guides/real-time-data)

### 3. Hyperliquid 官方協議與代碼庫 (Hyperliquid Protocol & SDK)
- 📑 **Hyperliquid HIP-4 協議標準**: [https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-4-outcome-markets](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-4-outcome-markets)
- 🐍 **Hyperliquid 官方 Python SDK**: [https://github.com/hyperliquid-dex/hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
- 🐙 **Outcome HIP-4 官方 TypeScript SDK 倉庫**: [https://github.com/Outcome-xyz/hip4](https://github.com/Outcome-xyz/hip4)

---

## 壹、 執行摘要與遷移背景 (Executive Summary)

本專案旨在將現有的 **Polymarket BTC 15 分鐘預測市場量化做市交易系統**，全面升級並遷移至 **Hyperliquid 即將上線的原生預測市場平台 —— Outcome (HIP-4 協議)**。

透過此次架構遷移，交易系統將從原先「Polygon EVM + 外部中心化 CLOB + 複雜手動合約贖回」的混合模式，升級為「Hyperliquid HyperCore L1 原生鏈上撮合 + 統一保證金 + 自動結算」的高效架構。**系統將 100% 完整保留並繼承核心量化資產（信號引擎、波動度預測、勝率校準分桶與風險風控），同時徹底剔除超過 40% 脆弱的鏈下爬蟲、外部 Oracle 依賴與手動 Redeem 腳本。**

---

## 貳、 核心架構對比與技術優勢評估

| 評估維度 | 原 Polymarket 實作 (當前倉庫) | Hyperliquid Outcome (HIP-4) | 遷移帶來的量化與工程優勢 |
| :--- | :--- | :--- | :--- |
| **底層撮合架構** | Polygon EVM + 中心化 CLOB 伺服器 (`py_clob_client_v2`) | **Hyperliquid HyperCore L1 原生撮合** | 撮合延遲從 ~300ms 降至 **< 200ms**，消除中心化 API 尖峰排隊與丟單風險。 |
| **Strike 與現貨基準** | 需透過 Gamma API 與 `/api/crypto/crypto-price` 抓取 TWAP 60s 開盤價，易發生參數錯位（如 D.3 痛點）。 | **合約 Spec 直接標明 `targetPrice`，結算使用 HyperCore BTC Mark Price 線性插值**。 | **彻底消除 Strike 抓取失敗與 Oracle 延遲風險！** `SpotPricer` 直接採用 Hyperliquid 原生 BTC Mark Price。 |
| **帳戶與保證金** | Polygon 獨立 pUSD/USDC，需管理 Token Allowances 與鏈上 Gas。 | **全網統一保證金（Unified Margin）**，USDC 在現貨、永續與預測市場通用。 | 資金利用率大幅提升，全鏈無 Gas 消耗。 |
| **到期結算機制** | 需常駐 `check_positions_and_redeem.py` 呼叫智能合約手動 Claim / Redeem。 | **協議原生自動結算（Auto Native Settlement）**：到期自動派發 1 USDC，掛單自動取消。 | **可徹底刪除所有複雜的手動 Redeem / Merge 腳本與鏈上呼叫！** |
| **身分驗證與簽名** | API Key / Secret / Passphrase + EIP-712 每次計算。 | **暫態 Agent Key 機制**：一次 EIP-712 授權，後續以 Agent Key 靜默 MessagePack+EIP-712 簽名。 | 毫秒級無感下單，密鑰僅留記憶體，安全性更高。 |
| **合約代幣命名** | 複雜的 Condition ID 與 16 進位 Token ID。 | **標準化編碼**：AMM 價格 `@outcomeId`，交易儀表 `#outcomeId0` (YES) / `#outcomeId1` (NO)。 | 標的識別與資產映射邏輯大幅簡化。 |

---

## 參、 模組重用度與代碼處置矩陣 (Codebase Asset Reuse Matrix)

```mermaid
flowchart TD
    subgraph 完全保留 (100% Core Reuse)
        SE["SignalEngine / side_score"]
        FS["ForecastState (Sigma, Vol, Delta)"]
        SDR["Strong Directional Regime (分桶結算 EV)"]
        DB["TradeJournalDB (事件追蹤與 PnL 核算)"]
        PM["PositionManager (單市場 1 筆預算管制)"]
    end

    subgraph 徹底廢棄/刪除 (Delete & Eliminate)
        DEL_RED["check_positions_and_redeem.py (自動結算替代)"]
        DEL_POLY["Polygon Web3, Allowances, CTF Contracts"]
        DEL_GAM["Gamma API & crypto-price 脆弱爬蟲"]
        DEL_PAT["Nautilus Polymarket 專用補丁"]
    end

    subgraph 全新適配層 (New Outcome/HIP-4 Adapters)
        AD_AUTH["OutcomeAuth: Agent Key 授權管理"]
        AD_CLIENT["HIP4Client: 原生 Python REST & WS 通訊"]
        AD_DISC["OutcomeLifecycle: 解析 class:priceBinary / 15m"]
        AD_MKT["OutcomeMarketData: L2 Book / allMids / Mark Price"]
        AD_EXEC["OutcomeExecutionAdapter: Agent Key 下單/撤單"]
    end

    AD_DISC --> SE
    AD_MKT --> FS
    FS --> SE
    SE --> SDR
    SDR --> PM
    PM --> AD_EXEC
    AD_EXEC --> DB
```

### 1. 100% 完整保留並重用的核心量化資產
- **`bot/signal_engine.py`**：BTC 價格相對於 Strike 的概率評估與 `side_score` 計算。
- **`bot/forecast_state.py`**：`sigma` 波動度估計、Time-Decay、Implied Volatility Floor 與 Delta 計算。
- **`bot/strong_directional_regime.py`**：勝率校準模型、`10_30` 與 `30_60` 分桶真實結算期望值（Resolution EV）機制。
- **`monitoring/trade_journal_db.py`**：SQLite 本地日誌與多維度事件記錄（`ENTRY_REGIME_OBSERVATION`、`MARKET_SETTLEMENT`、`LIVE_SIGNAL_COMPARE`）。
- **`bot/position_manager.py`**：D.1/D.2 確立的「單一市場一次性進場」預算與倉位管理。

### 2. 徹底廢棄與刪除的歷史負債
- 🗑️ `scripts/check_positions_and_redeem.py`、`scripts/backfill_redeem_activity.py`：Outcome 到期自動結算至錢包，不再需要鏈上 Redeem。
- 🗑️ `bot.compat_patches` & Nautilus Polymarket 專用補丁：替換為輕量級原生 Python HIP-4 客戶端。
- 🗑️ Polygon 相關合約互動代碼與 Token 授權檢查（Allowance）。
- 🗑️ Gamma API 輪詢與 `/api/crypto/crypto-price` 爬蟲代碼。

---

## 肆、 Hyperliquid HIP-4 核心規範與微觀規則

### 1. 15 分鐘 BTC 市場規格 (Market Spec)
Outcome 上的 15 分鐘 BTC 價格合約規格格式為：
```text
class:priceBinary|underlying:BTC|expiry:20260823-1015|targetPrice:78213|period:15m
```
- **Side 0 (`#outcomeId0`)**：YES / UP（結算時 Mark Price $\ge$ `targetPrice` 則賠付 1 USDC）。
- **Side 1 (`#outcomeId1`)**：NO / DOWN（結算時 Mark Price $<$ `targetPrice` 則賠付 1 USDC）。
- **結算價計算（線性插值）**：直接讀取到期點前後的 Mark Price：
  $$\text{Settlement Price} = \text{markPrice}_0 + \frac{\text{settlementTime} - t_0}{t_1 - t_0} \times (\text{markPrice}_1 - \text{markPrice}_0)$$

### 2. 資產 ID 與代幣編碼公式
- **Wire 協議層 Asset ID 計算**：
  $$\text{Asset ID} = 100\,000\,000 + (\text{outcomeId} \times 10) + \text{sideIndex}$$
  *(例：Outcome ID 為 516，Side 0 的 Asset ID 為 `100005160`，Side 1 為 `100005161`)*
- **價格精度（Tick-Alignment）**：價格需對齊至 5 位有效數字，介於 `0.0001` 與 `0.9999`。
- **最小名義價值（Min Notional）**：
  - 開倉訂單名義價值需 $\ge \mathbf{10\text{ USDC}}$（`price × shares >= 10`）。
  - 平倉/止損時需標記 `skipMinNotionalCheck: true`，以確保剩餘碎股能完全清空。

### 3. 身分驗證模型（雙層 Agent Key 架構）
1. **外層 EOA**：用戶錢包持有資金，僅需在啟動時簽署一次 EIP-712 授權（`submitAgentApproval`）。
2. **內層 Agent Key**：程式在記憶體中生成暫態私鑰，後續下單、撤單均透過 Agent Key 進行 L1 靜默簽名（`MessagePack` 序列化 + `keccak-256` + EIP-712 on `chainId: 1337`），零彈窗、零延遲。

---

## 伍、 各子系統遷移設計細節

### 1. 通訊與驗證層 (`bot/adapters/outcome_client.py`)
- 開發原生 Python 非同步客戶端，直接使用 `websockets` 與 `httpx`，免去 Node.js/TypeScript 橋接延遲。
- 封裝 Hyperliquid `/info` 介面（獲取 `outcomeMeta`、`allMids`、`l2Book`、`userState`）。
- 封裝 Hyperliquid `/exchange` 介面（發送 L1 Agent 簽名的 `order`、`cancel`、`userOutcome`）。

### 2. 市場生命週期層 (`bot/lifecycle/outcome_lifecycle.py`)
- 監聽 Outcome 15 分鐘 BTC 市場滾動切換。
- 自動解析 `targetPrice` 作為精確 Strike，完全擺脫原有的 D.3 外部 TWAP 爬蟲。
- 市場生命週期狀態機：
  $$\text{WAITING (開盤前 1m)} \longrightarrow \text{ACTIVE (開盤至 5m left)} \longrightarrow \text{REDUCE\_ONLY (5m 至 1m left)} \longrightarrow \text{SETTLED (結算)}$$

### 3. 實時行情與定價層 (`bot/pricing/outcome_pricing.py`)
- WebSocket 訂閱 HyperCore BTC Mark Price，直接供給 `SpotPricer` 作為全域唯一的權威現貨。
- WebSocket 訂閱 `#outcomeId0` 與 `#outcomeId1` 的 L2 Order Book，維持微觀盤口撮合。
- `QuoteEconomics` 費用模型調整：從 Polymarket S-curve 轉為 Hyperliquid 階梯費率 + 4% 推薦碼折扣 + Builder Fee 計算。

### 4. 執行與止損階梯層 (`bot/execution/outcome_execution.py`)
- **Maker BUY 下單**：發送 Post-Only GTC 限價單，嚴格維持 Queue Priority。
- **Invalidation 止損階梯**：
  - 階段一：撤銷 Take-Profit (TP) 掛單，提交 GTC Passive Recovery SELL。
  - 階段二：尾盤或跌破停損點時，升級為 IOC / FrontendMarket 立即出場。

### 5. 到期核算與日誌表記錄 (`monitoring/trade_journal_db.py`)
- 市場到期時，直接監聽 Hyperliquid 帳戶 USDC 變動事件。
- 自動寫入 `MARKET_SETTLEMENT` 事件，無縫保留所有的勝率統計與 PnL 審計報表。

---

## 陸、 五階段遷移實施路徑 (5-Stage Implementation Roadmap)

```text
2026-08-23                                                            上線完成
┌──────────────┬──────────────┬──────────────┬──────────────┬─────────────┐
│   Phase 1    │   Phase 2    │   Phase 3    │   Phase 4    │   Phase 5   │
│  HIP-4 客戶端 │ 市場發現與行情 │ 定價與經濟學  │ 執行與止損階梯 │ 測試網影子回測│
│  (Day 1-2)   │   (Day 3)    │   (Day 4)    │   (Day 5)    │  (Day 6-7)  │
└──────────────┴──────────────┴──────────────┴──────────────┴─────────────┘
```

### 【Phase 1】HIP-4 原生 Python 客戶端建設 (Day 1-2)
- **目標**：完成 Python 原生 Agent Key 簽名與 Hyperliquid REST/WS 基礎通訊庫。
- **交付產物**：
  - `bot/adapters/outcome_auth.py`
  - `bot/adapters/outcome_client.py`
  - 單元測試套件：驗證 Asset ID 編碼、Tick-Alignment、EIP-712 簽名正確性。

### 【Phase 2】市場發現與生命週期同步 (Day 3)
- **目標**：實現 15 分鐘 BTC 市場的自動發現與 Strike 解析。
- **交付產物**：
  - `bot/lifecycle/outcome_lifecycle.py`
  - 驗證 `class:priceBinary` 15m 規格的自動滾動與狀態流轉。

### 【Phase 3】行情、定價與經濟性門檻對齊 (Day 4)
- **目標**：將 Hyperliquid BTC Mark Price 與盤口深度接入 `ForecastState` 與 `StrongDirectionalRegime`。
- **交付產物**：
  - `bot/pricing/outcome_pricing.py`
  - 更新 `QuoteEconomics`，適配 Hyperliquid 費率結構與 10 USDC 最小下單金額校準。

### 【Phase 4】訂單執行、止損階梯與自動結算記帳 (Day 5)
- **目標**：打通 Maker GTC 下單、Requote、IOC 止損與到期自動記帳。
- **交付產物**：
  - `bot/execution/outcome_execution.py`
  - 升級 `TradeJournalDB`，對接帳戶餘額原生結算事件。

### 【Phase 5】Testnet 影子交易與主網上線切換 (Day 6-7)
- **目標**：在 Hyperliquid Testnet 進行 48 小時無人值守測試，驗證端到端穩定性。
- **驗證指標**：
  - 100% 成功開單並進入強方向窗口（`300s-600s / $10-$60`）。
  - 自動結算金額與 `TradeJournalDB` 記帳 0 誤差。
  - 通過所有回歸測試（`278+ passed`）後，切換主網上線。

---

## 柒、 風險控制與回滾機制 (Risk & Rollback Plan)

1. **Testnet 沙盒隔離**：所有適配代碼優先在 Hyperliquid Testnet 執行，只有在積累超過 50 場完整生命週期測試且零異常後才切換主網。
2. **最小金額保護**：主網初期配置單筆 10 USDC（滿足協議最小門檻即可），驗證實盤撮合與費率。
3. **保留雙適配器架構**：抽象基類保留 Polymarket 適配介面，若 Outcome 主網遭遇不可抗力延期，可一鍵切換回 Polymarket 運行。
