/**
 * x402 checkout for Coinbase tokenized equities on Base.
 *
 * Pay USDC to `/paid/buy/:ticker`, receive the shares at the address that
 * signed the payment. There is no account, no session and no place for us to
 * hold your asset: the swap's recipient is the payer, recovered from the
 * signature rather than taken from the request body.
 */

import express from "express";
import { isAddress } from "viem";
import {
  BASE_CAIP2,
  BASE_EXPLORER,
  MARKETS,
  USDC,
  formatUsdc,
  market as findMarket,
  usdcAtomic,
} from "./chain.mjs";
import { DEFAULT_SLIPPAGE_BPS, quoteBuy, readPoolPrice } from "./quote.mjs";
import { buildPaymentRequirements, challengeBody, decodePaymentHeader, facilitatorAccount, sendChallenge } from "./x402.mjs";
import { fulfilOrder } from "./fulfil.mjs";
import { Journal, nullJournal } from "./journal.mjs";

const DEFAULT_FEE_BPS = 100;
// A market order into a concentrated-liquidity band moves the price it is
// filling at. The ceiling is not politeness: past it, the buyer is paying for
// their own impact and the 1.5% floor starts rejecting honest fills.
const DEFAULT_MAX_USD = 250;
const DEFAULT_MIN_USD = 1;

export function config(env = process.env) {
  const payTo = String(env.BASE_PAY_TO || "").trim();
  const key = String(env.BASE_FACILITATOR_KEY || "").trim();
  const account = key ? facilitatorAccount(key) : null;

  if (payTo && !isAddress(payTo)) {
    throw new Error("BASE_PAY_TO is not an address");
  }
  // The settlement pays USDC to payTo, and the swap spends from the
  // facilitator account. If those are two different addresses the money lands
  // somewhere this process cannot spend, and every order refunds. Better to
  // never start than to discover that per order.
  if (account && payTo && account.address.toLowerCase() !== payTo.toLowerCase()) {
    throw new Error(
      `BASE_PAY_TO (${payTo}) must be the facilitator address (${account.address}): ` +
        "settlement pays it and the swap spends from it",
    );
  }

  return {
    payTo: payTo || account?.address || null,
    hasKey: Boolean(key),
    feeBps: number(env.BASE_FEE_BPS, DEFAULT_FEE_BPS),
    maxUsd: number(env.BASE_MAX_USD, DEFAULT_MAX_USD),
    minUsd: number(env.BASE_MIN_USD, DEFAULT_MIN_USD),
    slippageBps: number(env.BASE_SLIPPAGE_BPS, DEFAULT_SLIPPAGE_BPS),
    baseUrl: String(env.BASE_PUBLIC_URL || "").trim() || null,
  };
}

function number(value, fallback) {
  const raw = String(value ?? "").trim();
  return raw ? Number(raw) : fallback;
}

/** The price of an order: what the buyer swaps, plus our cut. */
export function priceOrder(usd, { feeBps }) {
  const amountInAtomic = BigInt(usdcAtomic(usd.toFixed(6)));
  const feeAtomic = (amountInAtomic * BigInt(feeBps)) / 10_000n;
  return { amountInAtomic, feeAtomic, totalAtomic: amountInAtomic + feeAtomic };
}

export function createServer(settings = config(), log = settings.hasKey ? new Journal() : nullJournal) {
  const app = express();
  app.use(express.json({ limit: "256kb" }));

  app.get("/health", (_request, response) => {
    response.json({
      ok: true,
      service: "base-stock-x402",
      network: BASE_CAIP2,
      asset: USDC.address,
      payTo: settings.payTo,
      payable: Boolean(settings.payTo && settings.hasKey),
      markets: Object.keys(MARKETS),
      feeBps: settings.feeBps,
      limits: { minUsd: settings.minUsd, maxUsd: settings.maxUsd },
      // A count, never the orders themselves: this route is public and the
      // entries carry payer addresses. `npm run orders` prints the details.
      unresolvedOrders: log.unresolved().length,
    });
  });

  app.get("/.well-known/x402", (request, response) => {
    const origin = settings.baseUrl ?? `${request.protocol}://${request.get("host")}`;
    response.json({
      x402Version: 2,
      network: BASE_CAIP2,
      asset: USDC.address,
      resources: Object.values(MARKETS).map((entry) => ({
        resource: `${origin}/paid/buy/${entry.ticker}`,
        method: "POST",
        description:
          `Buy ${entry.symbol} (${entry.name}) on Base. Pay USDC, the shares are ` +
          "delivered to the address that signed the payment.",
        priceNote: `usd query parameter, $${settings.minUsd}–$${settings.maxUsd}, plus ${settings.feeBps / 100}% fee`,
        extensions: bazaarExtension(entry, settings),
      })),
    });
  });

  // Unpaid, so an agent can size an order before it commits to one. Reads the
  // same pool the paid route will trade against.
  app.get("/quote/:ticker", async (request, response) => {
    const entry = findMarket(request.params.ticker);
    if (!entry) return response.status(404).json({ error: unknownTicker(request.params.ticker) });
    const usd = orderSize(request, settings);
    if (usd.error) return response.status(400).json({ error: usd.error });

    try {
      const price = await readPoolPrice(entry);
      const { amountInAtomic, feeAtomic, totalAtomic } = priceOrder(usd.value, settings);
      const quote = quoteBuy({
        market: entry,
        amountInAtomic,
        usdPerShare: price.usdPerShare,
        slippageBps: settings.slippageBps,
      });
      response.json({
        ticker: entry.ticker,
        symbol: entry.symbol,
        usdPerShare: +price.usdPerShare.toFixed(4),
        youPay: formatUsdc(totalAtomic),
        feeIncluded: formatUsdc(feeAtomic),
        estimatedShares: +quote.expectedShares.toFixed(8),
        // Named a floor rather than a guarantee: it is the point below which
        // the trade reverts and you are refunded, not a promised fill.
        refundBelowShares: Number(quote.minOutAtomic) / 10 ** entry.decimals,
        note: "estimate from the live pool; the fill is whatever the pool gives when the swap lands",
      });
    } catch (error) {
      response.status(502).json({ error: "pool_unreadable", detail: error.message });
    }
  });

  app.all("/paid/buy/:ticker", async (request, response) => {
    const entry = findMarket(request.params.ticker);
    if (!entry) return response.status(404).json({ error: unknownTicker(request.params.ticker) });
    if (!settings.payTo) {
      return response.status(503).json({ error: "not_configured", detail: "BASE_PAY_TO is not set" });
    }

    const usd = orderSize(request, settings);
    if (usd.error) return response.status(400).json({ error: usd.error });

    const { amountInAtomic, totalAtomic } = priceOrder(usd.value, settings);
    const origin = settings.baseUrl ?? `${request.protocol}://${request.get("host")}`;
    const requirements = buildPaymentRequirements({
      resource: `${origin}/paid/buy/${entry.ticker}?usd=${usd.value}`,
      priceAtomic: String(totalAtomic),
      description: `Buy ~$${usd.value} of ${entry.symbol}, delivered to the payer`,
      payTo: settings.payTo,
    });

    const payload = decodePaymentHeader(request.get("X-PAYMENT"));
    if (!payload) {
      return sendChallenge(response, requirements, undefined, bazaarExtension(entry, settings));
    }
    if (!settings.hasKey) {
      return response.status(503).json({ error: "facilitator_not_configured" });
    }

    const result = await fulfilOrder({
      payload,
      requirements,
      market: entry,
      amountInAtomic,
      slippageBps: settings.slippageBps,
      journal: log,
    });

    if (result.ok) {
      return response.json({
        ...result,
        settlementUrl: `${BASE_EXPLORER}/tx/${result.settlementTx}`,
        swapUrl: `${BASE_EXPLORER}/tx/${result.swapTx}`,
      });
    }
    // A pre-settlement refusal is the buyer's problem to fix and repeats the
    // challenge. A post-settlement failure is ours, and says what happened to
    // the money.
    if (result.stage === "pre-settlement") {
      response.set("x402-detail", String(result.detail ?? "").slice(0, 200));
      return sendChallenge(response, requirements, result.error, bazaarExtension(entry, settings));
    }
    return response.status(502).json(result);
  });

  app.use((_request, response) => response.status(404).json({ error: "not found" }));
  return app;
}

/**
 * The machine-readable "how do I call this".
 *
 * Copied in shape from Massive, which is the best example of it in the wild:
 * the challenge itself carries the input schema and an example response, so an
 * agent that has never seen this endpoint can call it correctly on the first
 * try without finding documentation.
 */
export function bazaarExtension(entry, settings) {
  return {
    bazaar: {
      info: {
        input: {
          type: "http",
          method: "POST",
          pathParams: { ticker: entry.ticker },
          queryParams: { usd: 100 },
        },
        output: {
          type: "json",
          example: {
            ok: true,
            ticker: entry.ticker,
            symbol: entry.symbol,
            payer: "0x0000000000000000000000000000000000000000",
            paid: "101.000000 USDC",
            delivered: `0.45433407 ${entry.symbol}`,
            usdPerShare: 219.99,
            settlementTx: "0x…",
            swapTx: "0x…",
          },
        },
      },
      schema: {
        $schema: "https://json-schema.org/draft/2020-12/schema",
        type: "object",
        required: ["input"],
        properties: {
          input: {
            type: "object",
            required: ["type", "method"],
            additionalProperties: false,
            properties: {
              type: { const: "http", type: "string" },
              method: { enum: ["GET", "POST"], type: "string" },
              pathParams: {
                type: "object",
                required: ["ticker"],
                properties: {
                  ticker: {
                    type: "string",
                    enum: Object.keys(MARKETS),
                    description: "Which equity to buy.",
                  },
                },
              },
              queryParams: {
                type: "object",
                required: ["usd"],
                properties: {
                  usd: {
                    type: "number",
                    minimum: settings.minUsd,
                    maximum: settings.maxUsd,
                    description:
                      "How many dollars of the equity to buy. The 402 asks for this " +
                      `plus a ${settings.feeBps / 100}% fee. Shares are delivered to the ` +
                      "address that signed the payment.",
                  },
                },
              },
            },
          },
        },
      },
      routeTemplate: "/paid/buy/:ticker",
    },
  };
}

function unknownTicker(ticker) {
  return `unknown ticker ${String(ticker).toUpperCase()}; this node sells ${Object.keys(MARKETS).join(", ")}`;
}

function orderSize(request, settings) {
  const raw = request.query?.usd ?? request.body?.usd;
  const value = Number(String(raw ?? "").trim());
  if (!Number.isFinite(value) || value <= 0) {
    return { error: "usd must be a positive number, e.g. ?usd=100" };
  }
  if (value < settings.minUsd || value > settings.maxUsd) {
    return { error: `usd must be between ${settings.minUsd} and ${settings.maxUsd}` };
  }
  return { value };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const settings = config();
  const port = Number(process.env.PORT || 8413);
  const log = settings.hasKey ? new Journal() : nullJournal;
  const open = log.unresolved();
  if (open.length) {
    // Loud, at startup, once: these are orders where somebody paid and the
    // process stopped before it finished. They do not resolve themselves.
    console.error(`WARNING: ${open.length} unresolved order(s) in ${log.path}. Run: npm run orders`);
  }
  createServer(settings, log).listen(port, () => {
    console.log(`base-stock-x402 on :${port} — payTo ${settings.payTo ?? "(unset)"}, payable=${settings.hasKey}`);
  });
}
