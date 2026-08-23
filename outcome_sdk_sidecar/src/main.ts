/*
 * JSON-lines process boundary for the official Outcome TypeScript SDK.
 *
 * P4 safety: the sidecar deliberately exposes only read-only commands today.
 * Order/cancel requests receive a deterministic rejection without initialising
 * an agent key or calling the trading adapter.
 */
import { createHIP4Adapter } from "@outcome.xyz/hip4";
import type { DefaultBinaryMarket } from "@outcome.xyz/hip4";
import { privateKeyToAccount } from "viem/accounts";
import { createInterface } from "node:readline";

type Request = {
  id: string;
  command:
    | "health"
    | "fetch_markets"
    | "auth_status"
    | "place_limit_order"
    | "cancel_order"
    | "canary_preflight"
    | "canary_limit_buy";
  testnet?: boolean;
  payload?: Record<string, unknown>;
};

type Response = {
  id: string;
  ok: boolean;
  result?: unknown;
  error?: { code: string; message: string };
};

const EXECUTION_DISABLED_MESSAGE =
  "P4 canary is hard-disabled: this sidecar cannot initialize auth, place orders, or cancel orders.";

const ONE_SHOT_CANARY = {
  marketId: "1153",
  outcome: "#11530",
  side: "buy" as const,
  price: "0.60",
  // One-shot user-authorised precision canary: $0.60 × 16.666667 ≈ $10.
  amount: "16.666667",
  timeInForce: "ALO" as const,
};

function requireMainnet(request: Request): void {
  if (request.testnet === true) throw new Error("This one-shot canary is mainnet-only");
}

function canaryEnabled(): boolean {
  return process.env.OUTCOME_SIDECAR_CANARY_EXECUTION === "1";
}

function requireExactCanaryPayload(payload: Record<string, unknown> | undefined): void {
  for (const [key, expected] of Object.entries(ONE_SHOT_CANARY)) {
    if (payload?.[key] !== expected) throw new Error(`canary payload ${key} must be ${expected}`);
  }
}

async function getCanaryPreflight(hip4: ReturnType<typeof createHIP4Adapter>): Promise<{
  market: DefaultBinaryMarket;
  bestAsk: number;
}> {
  const markets = (await hip4.events.fetchMarkets({ type: "defaultBinary" })) as DefaultBinaryMarket[];
  const market = markets.find((candidate) => candidate.outcomeId === Number(ONE_SHOT_CANARY.marketId));
  if (!market) throw new Error(`Outcome market #${ONE_SHOT_CANARY.marketId} is not active`);
  if (market.underlying !== "BTC" || market.sides[0]?.coin !== ONE_SHOT_CANARY.outcome) {
    throw new Error("Outcome market metadata does not match the approved BTC Up canary");
  }
  const book = await hip4.marketData.fetchOrderBook(ONE_SHOT_CANARY.marketId, 0);
  const bestAsk = Number(book.asks[0]?.price);
  if (!Number.isFinite(bestAsk) || bestAsk <= 0) throw new Error("No valid best ask for BTC Up");
  if (Number(ONE_SHOT_CANARY.price) >= bestAsk) {
    throw new Error(`${ONE_SHOT_CANARY.price} is not maker-safe: best ask is ${bestAsk}`);
  }
  return { market, bestAsk };
}

function respond(response: Response): void {
  process.stdout.write(`${JSON.stringify(response)}\n`);
}

function parseRequest(line: string): Request {
  const value: unknown = JSON.parse(line);
  if (!value || typeof value !== "object") throw new Error("request must be an object");
  const request = value as Partial<Request>;
  if (typeof request.id !== "string" || request.id.length === 0) throw new Error("request.id is required");
  if (typeof request.command !== "string") throw new Error("request.command is required");
  return request as Request;
}

async function handle(request: Request): Promise<Response> {
  if (request.command === "health") {
    return {
      id: request.id,
      ok: true,
      result: { protocol: "outcome-sdk-sidecar/v1", execution: "hard_disabled" },
    };
  }
  if (request.command === "place_limit_order" || request.command === "cancel_order" || request.command === "auth_status") {
    return {
      id: request.id,
      ok: false,
      error: { code: "P4_HARD_DISABLED", message: EXECUTION_DISABLED_MESSAGE },
    };
  }
  if (request.command !== "fetch_markets" && request.command !== "canary_preflight" && request.command !== "canary_limit_buy") {
    return { id: request.id, ok: false, error: { code: "UNKNOWN_COMMAND", message: request.command } };
  }
  if (request.command === "canary_limit_buy" && !canaryEnabled()) {
    return { id: request.id, ok: false, error: { code: "CANARY_EXECUTION_NOT_ENABLED", message: "Set OUTCOME_SIDECAR_CANARY_EXECUTION=1 for the approved one-shot canary." } };
  }
  const hip4 = createHIP4Adapter({ testnet: request.testnet ?? false });
  await hip4.initialize();
  if (request.command === "canary_preflight") {
    requireMainnet(request);
    const { market, bestAsk } = await getCanaryPreflight(hip4);
    return {
      id: request.id,
      ok: true,
      result: {
        marketId: market.outcomeId,
        underlying: market.underlying,
        outcome: ONE_SHOT_CANARY.outcome,
        bestAsk: String(bestAsk),
        approvedPrice: ONE_SHOT_CANARY.price,
        approvedAmount: ONE_SHOT_CANARY.amount,
        expectedNotional: "10.0000002",
        makerSafe: true,
      },
    };
  }
  if (request.command === "canary_limit_buy") {
    requireMainnet(request);
    requireExactCanaryPayload(request.payload);
    const { bestAsk } = await getCanaryPreflight(hip4);
    const wallet = process.env.HL_WALLET_ADDRESS;
    const signerPrivateKey = process.env.HL_AGENT_PRIVATE_KEY || process.env.HL_PRIVATE_KEY;
    if (!wallet || !signerPrivateKey) throw new Error("HL_WALLET_ADDRESS and a signing key are required");
    const signer = privateKeyToAccount(signerPrivateKey as `0x${string}`);
    if (signer.address.toLowerCase() !== wallet.toLowerCase()) {
      throw new Error("No approved HL_AGENT_PRIVATE_KEY is configured, and HL_PRIVATE_KEY does not match HL_WALLET_ADDRESS");
    }
    await hip4.auth.initAuth(wallet, signer);
    const order = await hip4.trading.placeOrder({ ...ONE_SHOT_CANARY, type: "limit" });
    return {
      id: request.id,
      ok: order.success,
      result: { ...order, preflight: { bestAsk: String(bestAsk), expectedNotional: "10.0000002", timeInForce: "ALO" } },
      ...(order.success ? {} : { error: { code: "ORDER_REJECTED", message: order.error ?? "Outcome rejected order" } }),
    };
  }
  const markets = (await hip4.events.fetchMarkets({ type: "defaultBinary" })) as DefaultBinaryMarket[];
  // Do not return the full raw object across the Python boundary.  Preserve
  // only typed discovery fields required by the strategy research layer.
  return {
    id: request.id,
    ok: true,
    result: markets.map((market) => ({
      outcomeId: market.outcomeId,
      name: market.name,
      underlying: "underlying" in market ? market.underlying : undefined,
      targetPrice: "targetPrice" in market ? market.targetPrice : undefined,
      period: "period" in market ? market.period : undefined,
      expiry: "expiry" in market && market.expiry ? market.expiry.toISOString() : undefined,
      sides: market.sides.map((side) => ({ name: side.name, coin: side.coin, asset: side.asset })),
    })),
  };
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  try {
    const request = parseRequest(line);
    respond(await handle(request));
  } catch (error) {
    respond({
      id: "invalid-request",
      ok: false,
      error: { code: "INVALID_REQUEST", message: error instanceof Error ? error.message : String(error) },
    });
  }
}
