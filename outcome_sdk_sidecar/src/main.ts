/* JSON-lines boundary for the official Outcome HIP-4 TypeScript SDK. */
import { createHIP4Adapter, HIP4Client } from "@outcome.xyz/hip4";
import type { DefaultBinaryMarket } from "@outcome.xyz/hip4";
import { privateKeyToAccount } from "viem/accounts";
import { createInterface } from "node:readline";

type Command = "health" | "fetch_markets" | "fetch_order_book" | "fetch_settled_outcome" | "fetch_account_snapshot" | "place_limit_order" | "cancel_order" | "merge_outcome";
type Request = { id: string; command: Command; testnet?: boolean; payload?: Record<string, unknown> };
type Response = { id: string; ok: boolean; result?: unknown; error?: { code: string; message: string } };
type LimitOrderPayload = { marketId: string; outcome: string; side: "buy" | "sell"; price: string; amount: string; timeInForce?: "GTC" | "GTD" | "FOK" | "FAK" | "ALO"; skipMinNotionalCheck?: boolean };
type CancelPayload = { marketId: string; outcome: string; orderId: string };
type MergePayload = { marketId: string; amount: string };

function respond(response: Response): void { process.stdout.write(`${JSON.stringify(response)}\n`); }
function parseRequest(line: string): Request {
  const value: unknown = JSON.parse(line);
  if (!value || typeof value !== "object") throw new Error("request must be an object");
  const request = value as Partial<Request>;
  if (typeof request.id !== "string" || !request.id) throw new Error("request.id is required");
  if (typeof request.command !== "string") throw new Error("request.command is required");
  return request as Request;
}
function executionEnabled(): boolean { return process.env.OUTCOME_SDK_EXECUTION_ENABLED === "1"; }
function getWalletAndSigner() {
  const wallet = process.env.HL_WALLET_ADDRESS;
  const agentKey = process.env.HL_AGENT_PRIVATE_KEY;
  const mainKey = process.env.HL_PRIVATE_KEY;
  if (!wallet || !(agentKey || mainKey)) throw new Error("HL_WALLET_ADDRESS and a signing key are required");
  const signer = privateKeyToAccount((agentKey || mainKey) as `0x${string}`);
  if (!agentKey && signer.address.toLowerCase() !== wallet.toLowerCase()) {
    throw new Error("HL_PRIVATE_KEY does not match HL_WALLET_ADDRESS; configure an approved HL_AGENT_PRIVATE_KEY");
  }
  return { wallet, signer };
}
function requireString(value: unknown, name: string): string {
  if (typeof value !== "string" || !value) throw new Error(`${name} is required`);
  return value;
}
function parseLimitPayload(payload: Record<string, unknown> | undefined): LimitOrderPayload {
  const marketId = requireString(payload?.marketId, "payload.marketId");
  const outcome = requireString(payload?.outcome, "payload.outcome");
  const price = requireString(payload?.price, "payload.price");
  const amount = requireString(payload?.amount, "payload.amount");
  const side = payload?.side;
  const timeInForce = payload?.timeInForce ?? "GTC";
  const skipMinNotionalCheck = payload?.skipMinNotionalCheck;
  if (side !== "buy" && side !== "sell") throw new Error("payload.side must be buy or sell");
  if (!["GTC", "GTD", "FOK", "FAK", "ALO"].includes(String(timeInForce))) throw new Error("invalid timeInForce");
  if (skipMinNotionalCheck !== undefined && typeof skipMinNotionalCheck !== "boolean") throw new Error("payload.skipMinNotionalCheck must be boolean");
  if (skipMinNotionalCheck && side !== "sell") throw new Error("skipMinNotionalCheck is allowed only for reduce-only sell flows");
  if (!/^\d+$/.test(amount) || Number(amount) <= 0) throw new Error("Outcome amount must be a positive integer number of shares");
  const numericPrice = Number(price);
  if (!Number.isFinite(numericPrice) || numericPrice <= 0 || numericPrice >= 1) throw new Error("price must be strictly between 0 and 1");
  if (numericPrice * Number(amount) < 10 && !skipMinNotionalCheck) throw new Error("order notional must be at least 10 USDC unless this is a reduce-only sell");
  return { marketId, outcome, side, price, amount, timeInForce: timeInForce as LimitOrderPayload["timeInForce"], skipMinNotionalCheck: Boolean(skipMinNotionalCheck) };
}
function parseCancelPayload(payload: Record<string, unknown> | undefined): CancelPayload {
  const marketId = requireString(payload?.marketId, "payload.marketId");
  const outcome = requireString(payload?.outcome, "payload.outcome");
  const orderId = requireString(payload?.orderId, "payload.orderId");
  if (!/^\d+$/.test(orderId)) throw new Error("payload.orderId must be numeric");
  return { marketId, outcome, orderId };
}
function parseMergePayload(payload: Record<string, unknown> | undefined): MergePayload {
  const marketId = requireString(payload?.marketId, "payload.marketId");
  const amount = requireString(payload?.amount, "payload.amount");
  if (!/^\d+(\.\d+)?$/.test(amount) || Number(amount) <= 0) throw new Error("payload.amount must be positive");
  return { marketId, amount };
}
async function requireMarketSide(hip4: ReturnType<typeof createHIP4Adapter>, marketId: string, outcome: string): Promise<number> {
  const markets = (await hip4.events.fetchMarkets({ type: "defaultBinary" })) as DefaultBinaryMarket[];
  const market = markets.find((candidate) => String(candidate.outcomeId) === marketId);
  if (!market) throw new Error(`active defaultBinary market ${marketId} not found`);
  const sideIndex = market.sides.findIndex((side) => side.coin === outcome);
  if (sideIndex < 0) throw new Error(`outcome ${outcome} is not a side of market ${marketId}`);
  return sideIndex;
}
async function requireAloIsMaker(hip4: ReturnType<typeof createHIP4Adapter>, payload: LimitOrderPayload, sideIndex: number): Promise<void> {
  if (payload.timeInForce !== "ALO") return;
  const book = await hip4.marketData.fetchOrderBook(payload.marketId, sideIndex);
  const price = Number(payload.price);
  const opposing = payload.side === "buy" ? Number(book.asks[0]?.price) : Number(book.bids[0]?.price);
  if (!Number.isFinite(opposing) || opposing <= 0) throw new Error("no valid opposing best price for ALO validation");
  if ((payload.side === "buy" && price >= opposing) || (payload.side === "sell" && price <= opposing)) throw new Error(`ALO would cross the book at ${opposing}`);
}

async function handle(request: Request): Promise<Response> {
  if (request.command === "health") return { id: request.id, ok: true, result: { protocol: "outcome-sdk-sidecar/v1", execution: "disabled_by_default" } };
  if (!(["fetch_markets", "fetch_order_book", "fetch_settled_outcome", "fetch_account_snapshot", "place_limit_order", "cancel_order", "merge_outcome"] as string[]).includes(request.command)) return { id: request.id, ok: false, error: { code: "UNKNOWN_COMMAND", message: request.command } };
  if ((request.command === "place_limit_order" || request.command === "cancel_order" || request.command === "merge_outcome") && !executionEnabled()) return { id: request.id, ok: false, error: { code: "EXECUTION_DISABLED", message: "Set OUTCOME_SDK_EXECUTION_ENABLED=1 after explicit operator approval." } };
  const hip4 = createHIP4Adapter({ testnet: request.testnet ?? false });
  await hip4.initialize();
  if (request.command === "fetch_markets") {
    const markets = (await hip4.events.fetchMarkets({ type: "defaultBinary" })) as DefaultBinaryMarket[];
    return { id: request.id, ok: true, result: markets.map((market) => ({ outcomeId: market.outcomeId, name: market.name, underlying: market.underlying, targetPrice: market.targetPrice, period: market.period, expiry: market.expiry.toISOString(), sides: market.sides.map((side) => ({ name: side.name, coin: side.coin, asset: side.asset })) })) };
  }
  if (request.command === "fetch_order_book") {
    const marketId = requireString(request.payload?.marketId, "payload.marketId");
    const outcome = requireString(request.payload?.outcome, "payload.outcome");
    const sideIndex = await requireMarketSide(hip4, marketId, outcome);
    const book = await hip4.marketData.fetchOrderBook(marketId, sideIndex);
    return { id: request.id, ok: true, result: { marketId, outcome, bids: book.bids, asks: book.asks, timestamp: book.timestamp } };
  }
  if (request.command === "fetch_settled_outcome") {
    const marketId = requireString(request.payload?.marketId, "payload.marketId");
    const client = new HIP4Client({ testnet: request.testnet ?? false });
    return { id: request.id, ok: true, result: await client.fetchSettledOutcome(Number(marketId)) };
  }
  if (request.command === "fetch_account_snapshot") {
    const wallet = requireString(request.payload?.wallet, "payload.wallet");
    const [positions, balances, orders, activity] = await Promise.all([
      hip4.account.fetchPositions(wallet), hip4.account.fetchBalance(wallet), hip4.account.fetchOpenOrders(wallet), hip4.account.fetchActivity(wallet),
    ]);
    return { id: request.id, ok: true, result: { positions, balances, openOrders: orders, activity } };
  }
  const { wallet, signer } = getWalletAndSigner();
  await hip4.auth.initAuth(wallet, signer);
  if (request.command === "place_limit_order") {
    const payload = parseLimitPayload(request.payload);
    const sideIndex = await requireMarketSide(hip4, payload.marketId, payload.outcome);
    await requireAloIsMaker(hip4, payload, sideIndex);
    const result = await hip4.trading.placeOrder({ ...payload, type: "limit" });
    return result.success ? { id: request.id, ok: true, result } : { id: request.id, ok: false, result, error: { code: "ORDER_REJECTED", message: result.error ?? "Outcome rejected order" } };
  }
  if (request.command === "merge_outcome") {
    if (process.env.OUTCOME_SETTLEMENT_ACTION_ENABLED !== "1") return { id: request.id, ok: false, error: { code: "SETTLEMENT_ACTION_DISABLED", message: "Set OUTCOME_SETTLEMENT_ACTION_ENABLED=1 after explicit operator approval." } };
    const merge = parseMergePayload(request.payload);
    const client = new HIP4Client({ testnet: request.testnet ?? false });
    if (!(await client.fetchSettledOutcome(Number(merge.marketId)))) return { id: request.id, ok: false, error: { code: "NOT_SETTLED", message: "Outcome is not settled; merge is not a redemption substitute." } };
    const result = await hip4.trading.mergeOutcome({ outcome: Number(merge.marketId), amount: merge.amount });
    return result.success ? { id: request.id, ok: true, result } : { id: request.id, ok: false, result, error: { code: "MERGE_REJECTED", message: result.error ?? "merge rejected" } };
  }
  const payload = parseCancelPayload(request.payload);
  const openOrders = await hip4.account.fetchOpenOrders(wallet);
  const ownedOrder = openOrders.find((order) => String(order.oid) === payload.orderId);
  if (!ownedOrder || ownedOrder.coin !== payload.outcome) throw new Error("order is not an open order owned by the configured wallet with the requested outcome");
  const result = await hip4.trading.cancelOrder([payload]);
  return { id: request.id, ok: true, result };
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  try { respond(await handle(parseRequest(line))); }
  catch (error) { respond({ id: "invalid-request", ok: false, error: { code: "INVALID_REQUEST", message: error instanceof Error ? error.message : String(error) } }); }
}
