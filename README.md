# Hyperliquid Outcome BTC Daily Bot

This repository operates one Hyperliquid Outcome BTC **daily** market through
the official SDK sidecar. `hyperliquid_project_overview.md` is the sole
authority for strategy, risk, execution, and research decisions.

The historical Polymarket/Nautilus implementation has been removed from the
working tree. It is not a fallback venue and cannot be selected at runtime.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
npm ci --prefix outcome_sdk_sidecar
npm run build --prefix outcome_sdk_sidecar
cp .env.example .env
```

Set `HL_WALLET_ADDRESS`, `HL_PRIVATE_KEY` (or an approved
`HL_AGENT_PRIVATE_KEY`), and `HL_TESTNET=0` in `.env`. The supported local
configuration surface is intentionally limited to the keys in `.env.example`.

## Run

Preflight only:

```bash
./.venv/bin/python -m bot.launcher --preflight-only
```

Live trading (interactive confirmation required):

```bash
./.venv/bin/python -u -m bot.launcher --live
```

The live launcher itself enables the official SDK execution path; there are no
separate manual execution flags. Current normal limits remain `$11` per entry
and `$11` Outcome exposure. Do not raise them to `$20` until the operator
explicitly authorizes the F5 canary.

## Verification

```bash
./.venv/bin/python -m pytest -q
```

The Python suite exercises the official SDK sidecar health protocol, so its
TypeScript dependencies must be installed and built first. GitHub Actions runs
the same `npm ci`, build, and pytest sequence on every push and pull request.
