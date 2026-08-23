# Hyperliquid Outcome (HIP-4) BTC 15 分鐘預測市場量化做市機器人

專為 **Hyperliquid 原生預測市場平台 —— Outcome (HIP-4 協議)** 打造的專業級 Maker-First 15 分鐘 BTC 方向性做市與量化套利系統。

[`hyperliquid_project_overview.md`](hyperliquid_project_overview.md) 為本系統唯一的架構、量化模型、執行語意與生命週期規則權威規範文件。

English version: [README.md](README.md).

---

## 一、 核心架構亮點 (Architecture Highlights)

1. **HyperCore L1 原生撮合**：於 Hyperliquid L1 鏈上撮合，延遲 `< 200ms`，且全鏈無 Gas 消耗。
2. **確定性 Strike 與 Mark Price 結算**：直接自 `OutcomeMarketSpec` 解析 `targetPrice` 作為精確 Strike，結算採用 HyperCore BTC Mark Price 線性插值，徹底告別外部 Oracle 爬蟲與 TWAP 延遲錯位痛點。
3. **全網統一保證金與原生自動結算**：USDC 在現貨、永續與預測市場通用。合約到期時協議自動將贏家派發 1.0 USDC 結算至帳戶餘額，完全無需手動智能合約 Claim / Redeem。
4. **雙層 Agent Key 安全機制**：主錢包（EOA）簽名授權一次，後續由記憶體暫態 Agent Key 進行 EIP-712 靜默簽名，毫秒級下單無彈窗。
5. **完整繼承頂級量化資產**：保留 `ForecastState`（雙 Sigma 波動度、時間衰減、隱含波動度下限）、`SignalEngine`（方向評分 `side_score`）、`StrongDirectionalRegime`（勝率分桶期望值）以及單一市場單次進場預算管制。
6. **做市執行與失效止損階梯**：Maker Post-Only (ALO) GTC 限價買單、10 USDC 最小名義價值校準、被動尾盤止盈（@ 0.97），以及兩階段 Invalidation 階梯止損（第一階段被動 GTC 賣單，第二階段 IOC 市價立即離場）。

---

## 二、 安裝與環境設定 (Setup)

推薦直接使用本專案的 Python 虛擬環境：

```bash
git clone https://github.com/ericeric0101/Polymarket-15m-BTC-bot.git
cd Hyperliquid_prediction_bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp config/operator.env.example .env
```

在 `.env` 中設定您的 Hyperliquid 帳戶憑證（`HL_WALLET_ADDRESS`, `HL_PRIVATE_KEY`）：

```ini
STRATEGY_PROFILE=btc15_twap_v3
VENUE=hyperliquid

HL_WALLET_ADDRESS=0x您的錢包地址
HL_PRIVATE_KEY=0x您的私鑰
HL_AGENT_PRIVATE_KEY=
HL_TESTNET=0
HL_MIN_NOTIONAL_USDC=10.0
```

---

## 三、 運行與操作指令 (Run & Operations)

### 1. 安全性預檢 (Preflight Check)
啟動前檢查密鑰有效性、市場發現與網路連線：
```bash
./.venv/bin/python bot/launcher.py --preflight-only
```

### 2. 模擬影子交易 (Dry-Run Simulation)
在不發送真實訂單的情況下，完整模擬信號計算、盤口漂移與訂單生命週期：
```bash
./.venv/bin/python bot/launcher.py --venue hyperliquid
```

### 3. 實盤交易模式 (Live Trading)
在 Hyperliquid Outcome 上進行真實資金交易（需互動輸入 `yes` 確認）：
```bash
./.venv/bin/python bot/launcher.py --venue hyperliquid --live
```

### 4. 終端即時儀表板 (Terminal Dashboard)
啟動終端即時視覺化監控：
```bash
./.venv/bin/python bot/launcher.py --venue hyperliquid --terminal-dashboard
# 或獨立啟動日誌分析儀表板：
DASHBOARD_THEME=light ./.venv/bin/python dashboard.py
```

### 5. 運行完整測試套件 (Automated Tests)
執行包含單元、整合與回歸測試在內的 310+ 測試項目：
```bash
./.venv/bin/pytest
```

---

## 四、 專案目錄結構 (Repository Map)

```text
hyperliquid_project_overview.md  單一架構權威設計與量化規範文件
bot/adapters/                    OutcomeAuth (Agent Key / EIP-712 簽名) 與 OutcomeClient (REST & WS)
bot/lifecycle/                   OutcomeLifecycle (class:priceBinary 規格解析與狀態機)
bot/pricing/                     OutcomePricing (HyperCore Mark Price、L2 盤口與 10 USDC 經濟門檻)
bot/execution/                   OutcomeExecutionAdapter (Maker BUY ALO、TP、階梯止損與結算)
bot/                             核心量化決策大腦 (SignalEngine, ForecastState, PositionManager)
monitoring/                      TradeJournalDB (本地 SQLite 交易事件日誌與審計)
execution/                       做市報價引擎與手續費模型
config/profiles/                 版本化策略設定檔 (btc15_twap_v3.env)
tests/                           310+ 完整測試套件
```

---

## 五、 風險聲明 (Risk Disclaimer)

預測市場交易具有高度財務風險。以 `--live` 模式運行時將涉及真實資金。請務必先在測試網進行充分驗證，並自主監控倉位風險。本開源代碼不構成任何投資建議。
