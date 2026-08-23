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

### Read-Only Outcome Shadow Collection
The supported pre-live collection path. It reads Outcome market/account endpoints,
feeds the existing position and exit-risk components, and records
`OUTCOME_SHADOW_CYCLE` telemetry in SQLite. It does not import an execution
adapter, sign an action, simulate a fill, or call `/exchange`.
Each cycle journals the raw two-sided book snapshot plus ForecastState fair
probabilities, SignalEngine diagnostics, proposed side, entry eligibility, and
existing exit-risk decisions for later research/backtesting.

```bash
# One connectivity and journal smoke test
./.venv/bin/python scripts/outcome_shadow.py --cycles 1

# Continuous collection; stop safely with Ctrl-C
./.venv/bin/python scripts/outcome_shadow.py --interval-sec 5

# P0 + P1: also retain raw market specs and read-only L2/mid/trade stream evidence
./.venv/bin/python scripts/outcome_shadow.py --interval-sec 5 --ws \
  --journal-path logs/outcome_shadow.db
```

`--ws` never submits an order: it only subscribes to public market data. On
every WebSocket reconnect, the next REST cycle records an
`OUTCOME_WS_REST_RESYNC` event after refreshing mids and both books. Check the
collection in a second terminal:

```bash
sqlite3 logs/outcome_shadow.db "select event_type, count(*) from strategy_events where event_type like 'OUTCOME_%' group by event_type order by event_type;"
```

P4 strategy activation remains deliberately non-live. The preflight reports
missing evidence and writes an audit event; it does not activate execution:

```bash
./.venv/bin/python scripts/outcome_canary_preflight.py --db logs/outcome_shadow.db --record
```

### Official SDK sidecar

The Python strategy remains the source of decisions. `outcome_sdk_sidecar/` is
an isolated JSON-lines boundary around the official TypeScript SDK. It exposes
typed market discovery plus formal limit-order and cancel operations. Execution
requires both `allow_execution=True` in Python and the explicit operator
environment gate `OUTCOME_SDK_EXECUTION_ENABLED=1`; cancelling additionally
verifies that the target is an open order owned by the configured wallet.

```bash
cd outcome_sdk_sidecar
npm install
npm run build
npm run health
```

### One bounded maker cycle

The formal runner obtains the current first bid, posts an integer-share ALO
buy that meets the 10 USDC minimum, then only after an account-confirmed fill
cancels any remainder and posts the acquired shares at the then-current first
ask with ALO. It never takes liquidity or sells without confirmed inventory.
Both execution gates must be set explicitly by the operator.

```bash
OUTCOME_MAKER_CYCLE_ENABLED=1 OUTCOME_SDK_EXECUTION_ENABLED=1 \
  ./.venv/bin/python -m bot.outcome_maker_cycle \
  --market-id 1153 --outcome '#11530' --timeout-sec 900
```

It prints an immediate monitoring status and a periodic update every 30
seconds. `Ctrl+C` stops monitoring cleanly and intentionally does **not**
cancel a live order; reconcile or cancel that order explicitly.

On restart, an already-held position with a covering ALO sell is reported as
protected and the runner exits without placing another order.

### Outcome Shadow Dashboard
With the collector running, open a second terminal and view its SQLite
telemetry. Use the same journal path passed to the collector.

```bash
./.venv/bin/python scripts/outcome_shadow_dashboard.py --db logs/outcome_shadow.db
```

Use `--once` for a static snapshot. The dashboard is read-only and lets you
compare BTC mark, strike, and YES/NO bid-ask directly against Outcome's UI.

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
