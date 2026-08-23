# Hyperliquid Outcome (HIP-4) BTC 15-Minute Trading Bot

An institutional-grade, maker-first prediction market trading bot built for **Hyperliquid Outcome (HIP-4 Protocol)** BTC 15-minute markets.

[`hyperliquid_project_overview.md`](hyperliquid_project_overview.md) is the single authority for the architecture, quantitative models, execution semantics, and lifecycle rules.

Traditional Chinese translation: [繁體中文 README](docs/readme_ZH.md) / [readme_ZH.md](readme_ZH.md).

---

## 1. Architecture Highlights

1. **HyperCore L1 Native Matching**: Sub-200ms execution latency on Hyperliquid L1 with zero gas fees.
2. **Deterministic Target Price & Mark Price**: Direct Strike parsing from `OutcomeMarketSpec` (`targetPrice`) and real-time HyperCore BTC Mark Price linear interpolation settlement — completely eliminating external oracle TWAP crawl dependencies.
3. **Unified Margin & Auto-Settlement**: Seamless USDC margin shared across perps, spot, and outcomes. Winning outcome tokens auto-settle to 1.0 USDC directly into your clearinghouse balance without manual smart-contract claim/redeem steps.
4. **Two-Tier Agent Key Security**: Safe, silent L1 order and cancel signing using in-memory transient Agent Keys authenticated via EIP-712.
5. **Robust Quantitative Engine**: Shared `ForecastState` (volatility, time-decay, implied volatility floor), `SignalEngine` directional scoring, `StrongDirectionalRegime` resolution EV calibration, and single-entry per market budget enforcement.
6. **Execution & Risk Management**: Post-Only (ALO) GTC maker buy orders, 10 USDC min notional enforcement, passive tail-protect Take-Profit (@ 0.97), and two-stage invalidation recovery exit ladder (Stage 1: Passive Recovery SELL; Stage 2: IOC Marketable SELL).

---

## 2. Setup

Use the project's Python virtual environment:

```bash
git clone https://github.com/ericeric0101/Polymarket-15m-BTC-bot.git
cd Hyperliquid_prediction_bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp config/operator.env.example .env
```

Configure your `.env` with your Hyperliquid credentials (`HL_WALLET_ADDRESS`, `HL_PRIVATE_KEY`):

```ini
STRATEGY_PROFILE=btc15_twap_v3
VENUE=hyperliquid

HL_WALLET_ADDRESS=0xYourWalletAddress
HL_PRIVATE_KEY=0xYourPrivateKey
HL_AGENT_PRIVATE_KEY=
HL_TESTNET=0
HL_MIN_NOTIONAL_USDC=10.0
```

---

## 3. Run & Operations

### Preflight Check
Verify credentials, market discovery, and connectivity before launching:
```bash
./.venv/bin/python bot/launcher.py --preflight-only
```

### Dry-Run Simulation
Simulate live signal generation, orderbook drift, and order lifecycle without submitting real orders:
```bash
./.venv/bin/python bot/launcher.py --venue hyperliquid
```

### Live Trading
Start live trading on Hyperliquid Outcome (interactive confirmation required):
```bash
./.venv/bin/python bot/launcher.py --venue hyperliquid --live
```

### Terminal Dashboard
Run the live terminal dashboard:
```bash
./.venv/bin/python bot/launcher.py --venue hyperliquid --terminal-dashboard
# or standalone journal viewer:
DASHBOARD_THEME=light ./.venv/bin/python dashboard.py
```

### Run Test Suite
Run the full test suite (310+ tests):
```bash
./.venv/bin/pytest
```

---

## 4. Repository Structure

```text
hyperliquid_project_overview.md  Single authoritative architecture design document
bot/adapters/                    OutcomeAuth (Agent Key, EIP-712), OutcomeClient (REST & WS)
bot/lifecycle/                   OutcomeLifecycle (Market spec parsing & phase transitions)
bot/pricing/                     OutcomePricing (HyperCore Mark price, L2 book, fees & economics)
bot/execution/                   OutcomeExecutionAdapter (Maker BUY ALO, TP, Recovery Ladder, Settlement)
bot/                             Core quantitative brain (SignalEngine, ForecastState, PositionManager)
monitoring/                      TradeJournalDB (SQLite auditing and multi-dimensional analytics)
execution/                       Maker engine quote planning and fee models
config/profiles/                 Versioned strategy profiles (btc15_twap_v3.env)
tests/                           Comprehensive unit, integration, and regression test suites
```

---

## 5. Risk Disclaimer

Prediction markets carry substantial risk. Orders submitted with `--live` risk real funds. Always verify testnet operation and independently monitor positions.
