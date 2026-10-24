// SingIt search: Exa's web search, page contents and cited answers, sold over x402 at Exa's list price + 30% + the
// network's settlement fee, and bought from Exa with SingIt's own Exa API key, as BlockRun does
// (docs/singit-gateway-spec.md, 4.2).
//
// The price is worked out from the request and Exa's price list before the payer is asked; the payer's payment is
// verified, Exa answers, and only then is the payment settled: no answer, no charge. A request whose list price is
// above SEARCH_TERMS.maxExaAtomic is not sold. Exa's own cost report is kept here; if it ever exceeds the list price
// we charged against, that is logged.
import crypto from "node:crypto";
import { x402ResourceServer } from "@x402/express";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { ExactSvmScheme } from "@x402/svm/exact/server";
import { declareDiscoveryExtension } from "@x402/extensions/bazaar";
import { addPaymentRoute, isDiscoveryProbe } from "./metered.mjs";
import { ASSET, BASE, SOLANA, SOLANA_ASSET, TERMS, SOLANA_TERMS, SERVICE_NAME, ICON_URL } from "./pricing.mjs";

export const EXA_URL = "https://api.exa.ai";
export const SEARCH_ROUTES = { "/v1/exa/search": "/search", "/v1/exa/contents": "/contents", "/v1/exa/answer": "/answer" };
export const SEARCH_TERMS = Object.freeze({ markupBps: 3000, maxExaAtomic: 50_000 });  // never sell above $0.05 at Exa
// Exa's prices (exa.ai/pricing, 16 October 2026), in micro-USD: a search of up to 10 results by type, each result past
// 10, each page per content kind (text, highlights, summary), an answer.
export const EXA_PRICES = Object.freeze({ search: { instant: 4000, fast: 7000, auto: 7000 }, result: 1000, page: 1000,
  answer: 5000 });
const FEES = { [BASE]: TERMS.settlementFeeAtomic, [SOLANA]: SOLANA_TERMS.settlementFeeAtomic };

export class SearchError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

const FIELDS = {
  "/v1/exa/search": new Set(["query", "numResults", "type", "category", "includeDomains", "excludeDomains",
    "startPublishedDate", "endPublishedDate", "startCrawlDate", "endCrawlDate", "userLocation", "contents"]),
  "/v1/exa/contents": new Set(["urls", "ids", "text", "highlights", "summary", "maxAgeHours"]),
  "/v1/exa/answer": new Set(["query", "text", "model", "systemPrompt", "userLocation"]),
};
const kinds = (options) => (options && typeof options === "object"
  ? ["text", "highlights", "summary"].filter((kind) => options[kind]).length : 0);

// Exa's list price for this request, in micro-USD, after checking it is one sold here. SearchError otherwise.
export function exaPrice(path, body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new SearchError(400, "Send a JSON body.");
  const unknown = Object.keys(body).filter((k) => !FIELDS[path].has(k));
  if (unknown.length) throw new SearchError(400, `Not offered here: ${unknown.slice(0, 3).join(", ")}.`);
  const query = () => {
    if (typeof body.query !== "string" || !body.query.trim() || body.query.length > 2000) {
      throw new SearchError(400, "Send a query of up to 2000 characters.");
    }
  };
  let price;
  if (path === "/v1/exa/search") {
    query();
    const results = body.numResults ?? 10;
    if (!Number.isInteger(results) || results < 1 || results > 25) throw new SearchError(400, "numResults must be 1 to 25.");
    const base = EXA_PRICES.search[body.type ?? "auto"];
    if (base === undefined) throw new SearchError(400, "type must be instant, fast or auto.");
    price = base + Math.max(0, results - 10) * EXA_PRICES.result + results * kinds(body.contents) * EXA_PRICES.page;
  } else if (path === "/v1/exa/contents") {
    const pages = body.urls ?? body.ids;
    if (!Array.isArray(pages) || pages.length < 1 || pages.length > 10 || (body.urls && body.ids)
        || pages.some((p) => typeof p !== "string" || p.length > 2000)) {
      throw new SearchError(400, "Send urls (or ids): 1 to 10 of them.");
    }
    price = pages.length * Math.max(1, kinds(body)) * EXA_PRICES.page;
  } else {
    query();
    if (body.model !== undefined && !["exa", "exa-fast"].includes(body.model)) throw new SearchError(400, "model must be exa or exa-fast.");
    price = EXA_PRICES.answer;
  }
  if (price > SEARCH_TERMS.maxExaAtomic) {
    throw new SearchError(400, `This request costs ${(price / 1e6).toFixed(3)} USDC at Exa, more than is sold here. Ask for fewer results or less content.`);
  }
  return price;
}

// What the payer pays for a request Exa lists at `exaAtomic`, on `network`.
export function searchPrice(exaAtomic, network) {
  const exa = BigInt(exaAtomic);
  const markup = (exa * BigInt(SEARCH_TERMS.markupBps) + 9999n) / 10000n;
  return { exaAtomic: exa.toString(), markupAtomic: markup.toString(), feeAtomic: FEES[network],
           totalAtomic: (exa + markup + BigInt(FEES[network])).toString() };
}

// <=300 characters with the "SingIt Search: " prefix (see app.mjs's DISCOVERY_DESCRIPTION for why): each route does
// something different, so each gets its own text.
const DESCRIPTIONS = {
  "/v1/exa/search": "Web search from Exa, up to 25 results with optional page contents. Call it when an agent needs "
    + "current web results, with no API key or account.",
  "/v1/exa/contents": "Page text, highlights or a summary from up to 10 URLs, fetched by Exa. Call it when an agent "
    + "already has URLs and needs their content, with no API key or account.",
  "/v1/exa/answer": "A cited answer to a question, from Exa. Call it when an agent needs a sourced answer rather "
    + "than raw search results, with no API key or account.",
};
const EXAMPLES = {
  "/v1/exa/search": { query: "x402 payment facilitators", numResults: 5 },
  "/v1/exa/contents": { urls: ["https://x402.org"], text: true },
  "/v1/exa/answer": { query: "What is x402?" },
};

export function addSearchRoutes(app, config, { facilitatorClient, exaKey, fetchImpl = fetch, log = console.error }) {
  const server = new x402ResourceServer(facilitatorClient)
    .register(BASE, new ExactEvmScheme())
    .register(SOLANA, new ExactSvmScheme());
  const prices = new WeakMap();        // parsed body -> Exa's list price, for the middleware's price functions
  const used = new Map();              // payment signature digest -> when: one payment, one request to Exa
  const digest = (value) => crypto.createHash("sha256").update(value).digest("hex");
  const price = (network) => (ctx) => {
    const amount = searchPrice(prices.get(ctx.adapter.getBody()) ?? SEARCH_TERMS.maxExaAtomic, network).totalAtomic;
    return network === BASE ? { asset: ASSET, amount, extra: { name: "USD Coin", version: "2" } } : { asset: SOLANA_ASSET, amount };
  };
  // exact on Base first: the simplest offer for a buyer that only inspects accepts[0].
  const offers = [
    { scheme: "exact", network: BASE, payTo: config.payToBase, maxTimeoutSeconds: 120, price: price(BASE) },
    { scheme: "exact", network: SOLANA, payTo: config.payToSolana, maxTimeoutSeconds: 120, price: price(SOLANA) },
  ];
  for (const route of Object.keys(SEARCH_ROUTES)) {
    app.post(route, (req, res, next) => {
      // x402scan's empty, unpaid probe gets the standing offer, not a "Send a JSON body" 400.
      if (isDiscoveryProbe(req)) return next();
      try {
        prices.set(req.body, exaPrice(route, req.body));
        next();
      } catch (error) {
        res.status(error.status ?? 400).json({ error: { type: "invalid_request_error", message: error.message } });
      }
    });
  }
  addPaymentRoute(app, Object.fromEntries(Object.keys(SEARCH_ROUTES).map((route) => [`POST ${route}`, {
    accepts: offers, mimeType: "application/json", description: `SingIt Search: ${DESCRIPTIONS[route]}`,
    serviceName: SERVICE_NAME, iconUrl: ICON_URL,
    extensions: declareDiscoveryExtension({ bodyType: "json", input: EXAMPLES[route] }),
  }])), server);
  for (const [route, exaPath] of Object.entries(SEARCH_ROUTES)) {
    app.post(route, async (req, res) => {
      const header = req.get("payment-signature") ?? req.get("x-payment");
      const network = JSON.parse(Buffer.from(header, "base64").toString()).accepted?.network;
      const seen = digest(header);
      if (used.has(seen)) {
        return res.status(409).json({ error: { type: "invalid_request_error", message: "This payment was already used." } });
      }
      used.set(seen, Date.now());
      for (const [k, at] of used) if (Date.now() - at > 3_600_000) used.delete(k);
      const listed = prices.get(req.body);
      const started = Date.now();
      let answer;
      try {
        answer = await fetchImpl(`${EXA_URL}${exaPath}`, { method: "POST", redirect: "error",
          headers: { "Content-Type": "application/json", "x-api-key": exaKey,
                     "User-Agent": "SingIt-Search/1.0 (+https://singitai.app)" },
          body: JSON.stringify(req.body), signal: AbortSignal.timeout(60000) });
      } catch (error) {
        log(`singit-ask: Exa ${exaPath} unreachable (${error.name})`);
        return res.status(502).json({ error: { type: "upstream_error", message: "Exa did not answer. You were not charged." } });
      }
      const data = await answer.json().catch(() => null);
      if (answer.status !== 200 || !data || typeof data !== "object") {
        // 401/402 from Exa means SingIt's own key or Exa credit: not the payer's fault, and not charged.
        log(`singit-ask: Exa ${exaPath} answered HTTP ${answer.status}`);
        return res.status(answer.status === 400 ? 400 : 502).json({ error: { type: answer.status === 400
          ? "invalid_request_error" : "upstream_error", message: answer.status === 400
          ? "Exa refused the request. You were not charged." : "Exa did not answer. You were not charged." } });
      }
      const reported = Math.ceil(Number(data.costDollars?.total ?? 0) * 1e6);
      if (reported > listed) log(`singit-ask: Exa ${exaPath} cost ${reported} micro-USD, listed ${listed}`);
      delete data.costDollars;
      const billing = { ...searchPrice(listed, network), currency: "USDC" };
      log(`singit-ask: Exa ${exaPath} answered in ${Date.now() - started} ms for ${billing.totalAtomic} micro-USDC`);
      res.json({ ...data, billing });
    });
  }
}
