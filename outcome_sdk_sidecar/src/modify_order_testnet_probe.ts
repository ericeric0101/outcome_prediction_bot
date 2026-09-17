/* Explicit testnet-only probe for the official SDK's modifyOrder contract.
 *
 * This is intentionally not part of the long-lived Python sidecar command
 * surface.  It cannot target mainnet, defaults to dry-run, and requires both
 * a CLI flag and a dedicated environment opt-in before it can mutate a
 * pre-existing testnet order.
 */
import { createHIP4Adapter, type DefaultBinaryMarket } from "@outcome.xyz/hip4";
import { privateKeyToAccount } from "viem/accounts";

type Args = Record<string, string | boolean>;
function args(): Args {
  const values: Args = {};
  for (let index = 2; index < process.argv.length; index += 1) {
    const token = process.argv[index];
    if (!token.startsWith("--")) continue;
    const key = token.slice(2);
    if (key === "execute") values[key] = true;
    else values[key] = process.argv[++index] ?? "";
  }
  return values;
}
function required(value: string | boolean | undefined, name: string): string {
  if (typeof value !== "string" || !value) throw new Error(`${name} is required`);
  return value;
}
function wholeShareText(value: string, name: string): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0 || !Number.isInteger(parsed)) {
    throw new Error(`${name} must be a positive whole-share count`);
  }
  return String(parsed);
}
function output(value: unknown): never { console.log(JSON.stringify(value, null, 2)); process.exit(0); }

const input = args();
const execute = input.execute === true;
const marketId = required(input["market-id"], "--market-id");
const outcome = required(input.outcome, "--outcome");
const orderId = required(input["order-id"], "--order-id");
const newPrice = typeof input["new-price"] === "string" ? input["new-price"] : undefined;
const newSize = typeof input["new-size"] === "string" ? input["new-size"] : undefined;
if (!newPrice && !newSize) throw new Error("provide --new-price and/or --new-size");
if (!execute) {
  output({ mode: "dry_run", testnet: true, mutation_sent: false, marketId, outcome, orderId, newPrice, newSize,
    instruction: "Review this payload, then add --execute and OUTCOME_MODIFY_TESTNET_EXECUTE=1 for a testnet-only mutation." });
}
if (process.env.OUTCOME_MODIFY_TESTNET_EXECUTE !== "1") throw new Error("OUTCOME_MODIFY_TESTNET_EXECUTE=1 is required with --execute");
const wallet = required(process.env.HL_WALLET_ADDRESS, "HL_WALLET_ADDRESS");
const signingKey = process.env.HL_AGENT_PRIVATE_KEY || process.env.HL_PRIVATE_KEY;
if (!signingKey) throw new Error("HL_AGENT_PRIVATE_KEY or HL_PRIVATE_KEY is required");
const signer = privateKeyToAccount(signingKey as `0x${string}`);
if (!process.env.HL_AGENT_PRIVATE_KEY && signer.address.toLowerCase() !== wallet.toLowerCase()) {
  throw new Error("HL_PRIVATE_KEY must match HL_WALLET_ADDRESS; use an approved testnet agent key otherwise");
}
const hip4 = createHIP4Adapter({ testnet: true });
await hip4.initialize();
await hip4.auth.initAuth(wallet, signer);
const [orders, markets] = await Promise.all([
  hip4.account.fetchOpenOrders(wallet), hip4.events.fetchMarkets({ type: "defaultBinary" }) as Promise<DefaultBinaryMarket[]>,
]);
const owned = orders.find((order) => String(order.oid) === orderId);
if (!owned || owned.coin !== outcome) throw new Error("target is not an owned, open testnet order for --outcome");
const market = markets.find((candidate) => String(candidate.outcomeId) === marketId);
if (!market || !market.sides.some((side) => side.coin === outcome)) throw new Error("market/outcome is not an active testnet defaultBinary side");
const price = newPrice ?? owned.limitPx;
const amount = wholeShareText(newSize ?? owned.sz, "effective amount");
if (!Number.isFinite(Number(price)) || Number(price) <= 0 || Number(price) >= 1) throw new Error("effective price must be strictly between 0 and 1");
const result = await hip4.trading.modifyOrder({
  marketId, outcome, orderId, side: owned.side === "B" ? "buy" : "sell", type: "limit", price, amount,
  timeInForce: "GTC",
});
const after = await hip4.account.fetchOpenOrders(wallet);
output({ mode: "executed", testnet: true, mutation_sent: true, orderId, previous: { price: owned.limitPx, size: owned.sz },
  requested: { price, size: amount }, size_only_edit: price === owned.limitPx && amount !== owned.sz,
  price_change_requeues_expected: price !== owned.limitPx, sdk_result: result,
  post_read_matching_orders: after.filter((order) => order.coin === outcome && (String(order.oid) === orderId || (order.limitPx === price && order.sz === amount))),
});
