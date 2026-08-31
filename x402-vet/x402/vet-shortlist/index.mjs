/**
 * x402-vet / shortlist - which x402 services in a category are worth calling.
 *
 * Deploy on Bankr x402 Cloud. Self-contained: no gateway access, no secrets,
 * no imports. Directory facts come from x402-list under CC BY 4.0 and the
 * attribution ships in every response because the licence requires it.
 *
 * What is sold is not the facts. It is the filter:
 *   - volume net of the single largest buyer, instead of gross
 *   - revenue per buyer, which separates real use from a swept list
 *   - clusters of services reporting identical buyer/volume pairs
 *   - a live 402 handshake at request time, so a service that died an hour ago
 *     is not reported as healthy
 *
 * Verdicts describe observations, never motives. "one buyer accounts for 99% of
 * volume" is defensible; "farmed" is not.
 */
const DIRECTORY = "https://x402-list.com/api/v1/services";
const ATTRIBUTION = "Underlying directory data: x402-list.com (CC BY 4.0)";
const NOTE =
  "A verdict is a measurement, not a guarantee. Absence of traction is not " +
  "evidence of a bad service - new ones look identical to dead ones for a month.";
const PAGE_SIZE = 100;
const SNAPSHOT_TTL_MS = 60 * 60 * 1000;
const SNAPSHOT_MAX_AGE_MS = 6 * 60 * 60 * 1000;
const SNAPSHOT_SCHEMA_VERSION = 2;
const DIRECTORY_TIMEOUT_MS = 8000;
const PROBE_TIMEOUT_MS = 4000;
const MAX_CHALLENGE_BYTES = 256 * 1024;
const USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913";

async function fetchAll() {
  const out = [];
  let page = 1;
  let totalPages = 1;

  while (page <= totalPages && page <= 20) {
    const res = await fetch(`${DIRECTORY}?page=${page}&per_page=${PAGE_SIZE}`, {
      headers: { accept: "application/json" },
      signal: AbortSignal.timeout(DIRECTORY_TIMEOUT_MS),
    });
    if (!res.ok) throw new Error(`directory ${res.status}`);
    const body = await res.json();
    const batch = Array.isArray(body && body.data) ? body.data : [];
    if (batch.length === 0) break;
    out.push(...batch);
    totalPages = Number((body && body.meta && body.meta.total_pages) ?? page) || page;
    page += 1;
  }
  return out;
}

/**
 * Volume excluding the largest single buyer. One buyer is one relationship, not
 * a market: a service booking $160k with a 99% top-buyer share is worth ~$1.6k
 * of distributed demand, and ranking it first would be a lie of omission.
 */
function metricNumber(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function netVolume(t) {
  const gross = metricNumber(t.volume_usd_30d);
  const share = metricNumber(t.top_buyer_share_30d);
  if (gross === null || gross < 0) return 0;
  if (t.top_buyer_share_30d === null || t.top_buyer_share_30d === undefined) return gross;
  if (share === null || share < 0 || share > 1) return 0;
  return gross * (1 - share);
}

/**
 * Flag services reporting a buyer/volume pair identical to at least two others.
 * Says nothing about intent - only that those rows carry no independent
 * information about demand.
 */
function clusterSizes(services) {
  const counts = new Map();
  for (const s of services) {
    const t = (s.assessment && s.assessment.traction) || {};
    if (measurementState(t).state !== "measured") continue;
    const buyers = metricNumber(t.unique_buyers_30d);
    const gross = metricNumber(t.volume_usd_30d);
    const share = metricNumber(t.top_buyer_share_30d);
    if (buyers === null || gross === null || buyers <= 0 || gross < 0) continue;
    if (t.top_buyer_share_30d !== null && t.top_buyer_share_30d !== undefined && (share === null || share < 0 || share > 1)) continue;
    const key = `${buyers}:${gross.toFixed(2)}`;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return counts;
}

/**
 * The traction directory measures a subset of settlement paths and says so:
 * `measured` means it watched the money, `unmeasured-network` that the service
 * settles somewhere it does not observe, and `no-payto` that there is no
 * receiver to watch. Absent field: an older snapshot shape, treated as measured
 * only when it actually carries buyer numbers.
 */
function measurementState(traction) {
  const status = typeof traction.status === "string" ? traction.status : null;

  if (status === "measured") return { state: "measured" };
  if (status === "unmeasured-network") {
    return {
      state: "unmeasured",
      reason:
        "the directory does not measure this service's settlement network, so no demand figure exists either way",
    };
  }
  if (status === "no-payto") {
    return {
      state: "unmeasured",
      reason: "the directory has no payment receiver on file, so settlements cannot be attributed",
    };
  }
  if (status === "unresponsive") {
    return {
      state: "unmeasured",
      reason: "the directory suppresses traction while the service is unresponsive, so settlements cannot be attributed",
    };
  }
  if (status) {
    return { state: "unmeasured", reason: `the directory reports its demand data as "${status}"` };
  }

  return Number(traction.unique_buyers_30d ?? 0) > 0
    ? { state: "measured" }
    : { state: "unmeasured", reason: "the directory publishes no demand data for this service" };
}

function toRecord(s, clusters) {
  const a = s.assessment || {};
  const t = a.traction || {};
  const buyerMetric = metricNumber(t.unique_buyers_30d);
  const grossMetric = metricNumber(t.volume_usd_30d);
  const shareRaw = t.top_buyer_share_30d;
  const shareMetric = metricNumber(shareRaw);
  const buyers = buyerMetric ?? 0;
  const gross = grossMetric ?? 0;
  const net = netVolume(t);
  const perBuyer = buyers > 0 ? gross / buyers : 0;
  const key = `${buyers}:${gross.toFixed(2)}`;
  const cluster = buyers > 0 ? (clusters.get(key) ?? 0) : 0;
  const share = shareRaw === null || shareRaw === undefined ? null : shareMetric;
  const uptime = a.reliability_uptime_30d ?? s.uptime_24h ?? null;
  const checkedAgo = minutesSince(s.last_checked_at);
  const measured = measurementState(t);
  const malformedDemand = measured.state === "measured" && (
    buyerMetric === null || buyerMetric < 0 || !Number.isInteger(buyerMetric) ||
    grossMetric === null || grossMetric < 0 ||
    (shareRaw !== null && shareRaw !== undefined && (shareMetric === null || shareMetric < 0 || shareMetric > 1))
  );

  const why = [];
  let verdict = "ok";

  if (s.status !== "online") {
    verdict = "check";
    why.push(`not answering right now (status: ${s.status ?? "unknown"})`);
  }
  if (cluster >= 3) {
    verdict = "check";
    why.push(
      `reports the same buyer count and volume as ${cluster - 1} unrelated services`,
    );
  }
  // "Not measured" and "measured, found little" are different claims, and only
  // the second is `thin`. The directory now says which it means: it counts only
  // USDC settlements through facilitators it observes, and reports every other
  // service as unmeasured rather than as having no demand. Calling those `thin`
  // would put a demand judgement on services nobody has weighed.
  if (measured.state !== "measured") {
    if (verdict !== "check") verdict = "unrated";
    why.push(measured.reason);
  } else if (!malformedDemand) {
    if (share !== null && Number(share) > 0.9) {
      if (verdict === "ok") verdict = "thin";
      why.push(`one buyer accounts for ${Math.round(Number(share) * 100)}% of volume`);
    }
    if (buyers < 2 || net < 10) {
      if (verdict === "ok") verdict = "thin";
      why.push("little or no distributed demand measured yet");
    }
  }
  if (malformedDemand) {
    verdict = "check";
    why.push("the directory returned malformed demand metrics, so no demand verdict is safe");
  }
  if (a.risk_level && a.risk_level !== "clean" && a.risk_level !== "low") {
    verdict = "check";
    why.push(`directory risk flag: ${a.risk_level}`);
  }
  // The directory re-checks every five minutes. An hour of silence means its
  // monitoring lost the service, which is a fact about the freshness of this
  // answer rather than about the service, so it is reported and not scored.
  if (checkedAgo !== null && checkedAgo > 60) {
    why.push(`directory last checked it ${Math.round(checkedAgo)} minutes ago`);
  }

  if (verdict === "ok") {
    if (uptime !== null) why.push(`${uptime}% uptime over 30 days`);
    if (buyers) why.push(`${buyers} buyers, $${perBuyer.toFixed(2)} each`);
    if (a.compliance_grade) why.push(`compliance ${a.compliance_grade}`);
  }

  return {
    name: String(s.name ?? ""),
    slug: String(s.slug ?? ""),
    resource: typeof s.base_url === "string" ? s.base_url : "",
    category: String(s.category ?? ""),
    priceUsd: s.min_price_usd !== null && s.min_price_usd !== undefined && Number.isFinite(Number(s.min_price_usd)) ? Number(s.min_price_usd) : null,
    networks: Array.isArray(s.networks) ? s.networks.map(networkName) : [],
    uptime30d: uptime,
    lastCheckedAt: s.last_checked_at ?? null,
    netVolume30d: Math.round(net * 100) / 100,
    usdPerBuyer: Math.round(perBuyer * 100) / 100,
    buyers30d: buyers,
    topBuyerShare: share === null ? null : Math.round(Number(share) * 1000) / 1000,
    identicalPairCluster: cluster >= 3 ? cluster : 0,
    verdict,
    why,
  };
}

/**
 * The snapshot cache is a file, not a variable.
 *
 * x402 Cloud runs each request in an isolated serverless invocation with no
 * module-level state carried between them, so a `let snapshot` cache would be
 * cold every single time and re-pull the whole directory on every call. The
 * persistent store reached through `ctx.files` is the only thing that survives.
 *
 * Without it the endpoint still answers correctly, just slower - so a missing
 * or unwritable store degrades the service rather than breaking it.
 */
async function getSnapshot(ctx, cachePath) {
  const raw = await readCache(ctx, cachePath);
  const cached = raw && raw.version === SNAPSHOT_SCHEMA_VERSION && Array.isArray(raw.records) ? raw : null;
  if (cached && Date.now() - cached.at < SNAPSHOT_TTL_MS) return cached;

  try {
    const services = await fetchAll();
    const clusters = clusterSizes(services);
    const fresh = { version: SNAPSHOT_SCHEMA_VERSION, at: Date.now(), records: services.map((s) => toRecord(s, clusters)) };
    await writeCache(ctx, cachePath, fresh);
    return fresh;
  } catch (error) {
    // Serving a stale snapshot beats serving nothing, up to a point. Past that
    // the caller is told rather than quietly given old data.
    if (cached && Date.now() - cached.at <= SNAPSHOT_MAX_AGE_MS) return cached;
    throw error;
  }
}

async function readCache(ctx, cachePath) {
  if (!ctx || !ctx.files || typeof ctx.files.readJson !== "function") return null;
  try {
    const value = await ctx.files.readJson(cachePath);
    return value && Number.isFinite(value.at) ? value : null;
  } catch {
    return null;
  }
}

/**
 * The facilitator index is cached separately from the traction snapshot: it is
 * thirty times the size and changes far more slowly, so refreshing one must not
 * rewrite the other.
 */
async function getIndex(ctx, cachePath, budgetMs) {
  const cached = await readCache(ctx, cachePath);
  const usable = cached && cached.entries && cached.sources;

  if (usable && indexCoverage(cached).complete && Date.now() - cached.at < INDEX_TTL_MS) {
    return cached;
  }
  if (budgetMs <= 0) return usable ? cached : { at: Date.now(), sources: {}, entries: {} };

  const extended = await extendIndex(usable ? cached : null, budgetMs);
  await writeCache(ctx, cachePath, extended);
  return extended;
}

async function writeCache(ctx, cachePath, value) {
  if (!ctx || !ctx.files || typeof ctx.files.writeJson !== "function") return;
  try {
    await ctx.files.writeJson(cachePath, value);
  } catch {
    // A cache we cannot write is a slower endpoint, not a failed request.
  }
}

/**
 * The unpaid 402 handshake: a live x402 seller answers an unauthenticated
 * request with 402 and its terms. Free, carries no obligation, and is exactly
 * what a paying client sees first.
 *
 * The catalog list route publishes only `base_url`, which is a host root and
 * answers 200 or 404 to an unpaid GET. Probing that would mark healthy services
 * as broken, so the real paid path is read from the detail route first. A probe
 * we cannot aim is reported as `unknown`, never as a failure of the service.
 */
async function probeService(options) {
  const { slug = null, url = null, method = null, catalogPayTo: knownPayTo = null } = options || {};

  let detail = null;
  if (slug) {
    try {
      const res = await fetch(`${DIRECTORY}/${encodeURIComponent(slug)}`, {
        headers: { accept: "application/json" },
        signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      });
      if (res.ok) detail = (await res.json()).data ?? null;
    } catch {
      detail = null;
    }
  }

  let target;
  try {
    if (url) {
      const normalized = publicHttpsUrl(url);
      const published = paidTargetForUrl(detail, normalized);
      target = {
        url: normalized,
        method: method === "POST" ? "POST" : published?.method ?? "GET",
        catalogPayTo: knownPayTo ?? published?.catalogPayTo ?? null,
      };
    } else {
      target = firstPaidTarget(detail);
    }
  } catch {
    return { state: "unknown", reason: "probe target is not a public HTTPS URL" };
  }

  if (!target) {
    return { state: "unknown", reason: "no paid path published" };
  }
  // A path like /v2/actors/:actorId/run cannot be aimed without knowing the
  // actor. Guessing produces a 404 that would read as a broken service, so the
  // honest answer is that we did not check.
  if (hasPlaceholder(target.url)) {
    return { state: "unknown", reason: "paid path is a template, not a callable URL" };
  }

  const result = await probeUrl(target.url, target.method);
  return {
    ...result,
    probedUrl: target.url,
    probedMethod: target.method,
    catalogPayTo: target.catalogPayTo ?? knownPayTo,
  };
}

function firstPaidTarget(detail) {
  const endpoints = detail && Array.isArray(detail.endpoints) ? detail.endpoints : [];
  const active =
    endpoints.find((e) => e && e.is_active !== false && typeof e.path === "string" && Array.isArray(e.pricing) && e.pricing.length) ||
    endpoints.find((e) => e && e.is_active !== false && typeof e.path === "string") ||
    endpoints.find((e) => e && typeof e.path === "string");
  return targetFromEndpoint(detail, active);
}

function paidTargetForUrl(detail, url) {
  const endpoints = detail && Array.isArray(detail.endpoints) ? detail.endpoints : [];
  for (const endpoint of endpoints) {
    const target = targetFromEndpoint(detail, endpoint);
    if (target && resourceKey(target.url) === resourceKey(url)) return target;
  }
  return null;
}

function targetFromEndpoint(detail, endpoint) {
  const base = detail && typeof detail.base_url === "string" ? detail.base_url : "";
  if (!base || !endpoint || typeof endpoint.path !== "string") return null;
  try {
    return {
      url: joinUrl(base, endpoint.path),
      // Sellers declare their method, and a POST-only route answers 405 to a
      // GET. Probing with the wrong verb measures our mistake, not their uptime.
      method: String(endpoint.method || "GET").toUpperCase() === "POST" ? "POST" : "GET",
      catalogPayTo: endpointBasePayTo(endpoint),
    };
  } catch {
    return null;
  }
}

/**
 * Concatenate rather than resolve. `new URL("/chat", "https://h/api/v1")` drops
 * the /api/v1, because an absolute path resolves against the origin - which
 * silently aims the probe at a route the seller never published.
 */
function joinUrl(base, path) {
  const root = base.replace(/\/+$/, "");
  const tail = path.startsWith("/") ? path : `/${path}`;
  return publicHttpsUrl(`${root}${tail}`);
}

/**
 * Same host rules as publicHttpsUrl, but allows plain http. Used only to admit
 * an endpoint to the index - never to choose a probe target, which stays
 * HTTPS-only.
 */
function publicHttpUrl(value) {
  const url = new URL(String(value));
  if (url.protocol !== "http:" || url.username || url.password || unsafeHostname(url.hostname)) {
    throw new Error("unsafe URL");
  }
  return url.toString();
}

function publicHttpsUrl(value) {
  const url = new URL(String(value));
  if (url.protocol !== "https:" || url.username || url.password || unsafeHostname(url.hostname)) {
    throw new Error("unsafe URL");
  }
  return url.toString();
}

function ipv6Words(host) {
  let value = host.toLowerCase();
  if (value.includes(".")) {
    const split = value.lastIndexOf(":");
    const ipv4 = value.slice(split + 1).split(".");
    if (split < 0 || ipv4.length !== 4 || ipv4.some((part) => !/^\d+$/.test(part) || Number(part) > 255)) return null;
    value = `${value.slice(0, split)}:${(Number(ipv4[0]) * 256 + Number(ipv4[1])).toString(16)}:${(Number(ipv4[2]) * 256 + Number(ipv4[3])).toString(16)}`;
  }
  const halves = value.split("::");
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(":") : [];
  const right = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  if ([...left, ...right].some((part) => !/^[0-9a-f]{1,4}$/.test(part))) return null;
  const missing = 8 - left.length - right.length;
  if ((halves.length === 1 && missing !== 0) || (halves.length === 2 && missing < 1)) return null;
  return [...left.map((part) => parseInt(part, 16)), ...Array(missing).fill(0), ...right.map((part) => parseInt(part, 16))];
}

function unsafeHostname(value) {
  const host = String(value).toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
  if (!host || host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || host.endsWith(".internal")) {
    return true;
  }
  if (host.includes(":")) {
    const words = ipv6Words(host);
    if (!words) return true;
    if (words.every((word) => word === 0) || (words.slice(0, 7).every((word) => word === 0) && words[7] === 1)) return true;
    if ((words[0] & 0xfe00) === 0xfc00 || (words[0] & 0xffc0) === 0xfe80 || (words[0] & 0xffc0) === 0xfec0 || (words[0] & 0xff00) === 0xff00) return true;
    const mapped = words.slice(0, 5).every((word) => word === 0) && words[5] === 0xffff;
    const compatible = words.slice(0, 6).every((word) => word === 0);
    if (mapped || compatible) {
      return privateIpv4(`${words[6] >> 8}.${words[6] & 255}.${words[7] >> 8}.${words[7] & 255}`);
    }
    return false;
  }
  return privateIpv4(host);
}

function privateIpv4(host) {
  const parts = host.split(".");
  if (parts.length !== 4 || parts.some((part) => !/^\d+$/.test(part) || Number(part) > 255)) return false;
  const [a, b] = parts.map(Number);
  return (
    a === 0 ||
    a === 10 ||
    a === 127 ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) ||
    (a === 198 && (b === 18 || b === 19)) ||
    a >= 224
  );
}

/**
 * Index keys and comparisons accept plain http as well: they name an endpoint,
 * they do not reach one. Only probe targets are held to HTTPS.
 */
function publicIndexUrl(value) {
  try {
    return publicHttpsUrl(value);
  } catch {
    return publicHttpUrl(value);
  }
}

function resourceKey(value) {
  return publicIndexUrl(value);
}

function urlsShareResource(query, resource) {
  try {
    const left = new URL(publicIndexUrl(query));
    const right = new URL(publicIndexUrl(resource));
    if (left.origin !== right.origin) return false;
    const a = left.pathname.replace(/\/+$/, "") || "/";
    const b = right.pathname.replace(/\/+$/, "") || "/";
    return a === "/" || b === "/" || a === b || a.startsWith(`${b}/`) || b.startsWith(`${a}/`);
  } catch {
    return false;
  }
}

function hasPlaceholder(url) {
  return /[/:]:[A-Za-z0-9_-]|\{[^}]+\}|%7B/i.test(url);
}

/** The Base receiver the catalog last recorded, for comparison with the live one. */
function endpointBasePayTo(endpoint) {
  for (const p of Array.isArray(endpoint && endpoint.pricing) ? endpoint.pricing : []) {
    if (p && networkName(p.network_caip2 ?? p.network) === "base" && (p.pay_to ?? p.payTo)) {
      return String(p.pay_to ?? p.payTo).toLowerCase();
    }
  }
  return null;
}

function acceptsFrom(payload) {
  if (!payload || typeof payload !== "object") return [];
  if (Array.isArray(payload.accepts)) return payload.accepts;
  if (Array.isArray(payload.paymentRequirements)) return payload.paymentRequirements;
  if (payload.paymentRequirements && typeof payload.paymentRequirements === "object") {
    return [payload.paymentRequirements];
  }
  return [];
}

function parsePaymentHeader(raw) {
  if (!raw || raw.length > MAX_CHALLENGE_BYTES * 2) return null;
  try {
    return JSON.parse(raw);
  } catch {
    try {
      const normalized = raw.replace(/-/g, "+").replace(/_/g, "/");
      const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
      const binary = atob(padded);
      if (binary.length > MAX_CHALLENGE_BYTES) return null;
      const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
      return JSON.parse(new TextDecoder().decode(bytes));
    } catch {
      return null;
    }
  }
}

async function readJsonBounded(response) {
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > MAX_CHALLENGE_BYTES) return null;
  if (!response.body || typeof response.body.getReader !== "function") {
    const text = await response.text();
    return new TextEncoder().encode(text).length <= MAX_CHALLENGE_BYTES ? JSON.parse(text) : null;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_CHALLENGE_BYTES) {
      await reader.cancel();
      return null;
    }
    text += decoder.decode(value, { stream: true });
  }
  text += decoder.decode();
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function paymentAccepts(response) {
  const header = response.headers.get("payment-required") || response.headers.get("x-payment-required");
  const fromHeader = acceptsFrom(parsePaymentHeader(header)).filter(validAcceptance);
  if (fromHeader.length) return fromHeader;
  try {
    return acceptsFrom(await readJsonBounded(response));
  } catch {
    return [];
  }
}

function validAcceptance(value) {
  if (!value || typeof value !== "object" || typeof value.payTo !== "string" || !value.payTo.trim()) return false;
  if (typeof value.network !== "string" || !value.network.trim()) return false;
  const amount = value.amount ?? value.maxAmountRequired;
  return amount !== undefined && typeof amount !== "object" && /^\d+$/.test(String(amount));
}

async function probeUrl(resource, method) {
  const started = Date.now();
  const verb = method === "POST" ? "POST" : "GET";
  try {
    const target = publicHttpsUrl(resource);
    const res = await fetch(target, {
      method: verb,
      headers: {
        accept: "application/json",
        "user-agent": "x402-vet/1.0 (unpaid 402 handshake, no payment attempted)",
        ...(verb === "POST" ? { "content-type": "application/json" } : {}),
      },
      // An empty object, so the seller reaches its payment gate rather than its
      // body validator. The request is unpaid either way and buys nothing.
      body: verb === "POST" ? "{}" : undefined,
      redirect: "manual",
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });

    const latencyMs = Date.now() - started;
    // 405 says we used the wrong verb, 404 that we aimed at the wrong path, and
    // 400 that the seller validates its body before reaching the payment gate.
    // None of the three tells us anything about whether it would demand
    // payment, and we cannot distinguish "validates first" from "broken", so
    // the honest report is that the probe did not get an answer.
    if ((res.status >= 300 && res.status < 400) || (res.status >= 400 && res.status < 500 && res.status !== 402)) {
      return { state: "unknown", httpStatus: res.status, latencyMs, reason: `probe could not reach a paid route (HTTP ${res.status})` };
    }
    if (res.status !== 402) {
      return { state: "no-402", httpStatus: res.status, latencyMs };
    }

    const accepts = (await paymentAccepts(res)).filter(validAcceptance);
    if (!accepts.length) {
      return { state: "unknown", httpStatus: 402, latencyMs, reason: "402 response contained no valid payment requirements" };
    }

    const selected = accepts.find((a) => networkName(a.network) === "base") || accepts[0];
    return {
      state: "alive",
      httpStatus: 402,
      latencyMs,
      acceptsCount: accepts.length,
      livePayTo: selected.payTo.toLowerCase(),
      liveNetwork: selected.network,
      liveAsset: typeof selected.asset === "string" ? selected.asset.toLowerCase() : null,
      liveAmount: selected.amount === undefined ? selected.maxAmountRequired ?? null : selected.amount,
      liveScheme: selected.scheme ?? null,
    };
  } catch (error) {
    const timedOut = error && (error.name === "TimeoutError" || error.name === "AbortError");
    return { state: timedOut ? "timeout" : "unreachable", latencyMs: Date.now() - started };
  }
}

/**
 * A live probe can only ever downgrade a verdict, never upgrade one. A service
 * that answers is not thereby proven to deliver; a service we could not reach
 * or could not aim at is not thereby proven broken, and only a clear wrong
 * answer to a correctly aimed request is held against it.
 */
function applyProbe(record, result) {
  if (!result || result.state === "skipped") return record;

  const out = {
    ...record,
    liveProbe: result.state,
    liveProbeLatencyMs: result.latencyMs ?? null,
    probedUrl: result.probedUrl ?? null,
  };

  if (result.state === "unknown") {
    out.liveProbeReason = result.reason ?? null;
    return out;
  }

  if (result.state === "alive") {
    out.livePayTo = result.livePayTo ?? null;
    out.liveNetwork = result.liveNetwork ?? null;
    out.liveAsset = result.liveAsset ?? null;
    out.liveAmount = result.liveAmount ?? null;
    out.liveScheme = result.liveScheme ?? null;
    out.liveAssetIsUsdc = result.liveAsset ? result.liveAsset === USDC_BASE : null;

    // Worth stating plainly: the address taking the money is not the address
    // the catalog recorded. That is an observation about a mismatch, not a
    // claim about why it happened.
    if (
      networkName(result.liveNetwork) === "base" &&
      result.catalogPayTo &&
      result.livePayTo &&
      result.catalogPayTo !== result.livePayTo
    ) {
      out.payToChanged = true;
      out.why = [...out.why, "live payment receiver differs from the one the directory lists"];
      out.verdict = "check";
    } else if (networkName(result.liveNetwork) === "base" && result.catalogPayTo && result.livePayTo) {
      out.payToChanged = false;
    }
    return out;
  }

  if (result.state === "timeout") {
    out.why = [...out.why, "did not answer the unpaid 402 handshake within 4s"];
    return out;
  }

  out.verdict = "check";
  out.why = [
    ...out.why,
    result.state === "no-402"
      ? `answered HTTP ${result.httpStatus} instead of 402 to an unpaid request`
      : "did not respond to an unpaid request",
  ];
  return out;
}

/**
 * The second source: facilitator Bazaar catalogs.
 *
 * x402-list knows roughly 600 services and, uniquely, what they earn. The
 * facilitator catalogs know two orders of magnitude more endpoints - ~40,600
 * across ~2,600 domains - and, uniquely, the resource URLs themselves with
 * their prices and receivers. Neither is a census: a seller registers nowhere,
 * answering 402 on its own domain is the whole requirement. This is a union,
 * and says so.
 *
 * These two halves are complementary rather than redundant, and an agent that
 * only sees the traction directory sees roughly 1.5% of the visible ecosystem.
 */
const BAZAAR = {
  payai: "https://facilitator.payai.network",
  coinbase: "https://api.cdp.coinbase.com/platform/v2/x402",
  thirdweb: "https://api.thirdweb.com/v1/payments/x402",
  dexter: "https://facilitator.dexter.cash",
  ultravioletadao: "https://facilitator.ultravioletadao.xyz",
};
const BAZAAR_PAGE = 1000;
const INDEX_TTL_MS = 24 * 60 * 60 * 1000;
const INDEX_MAX_ENTRIES = 60000;
const EURC_BASE = "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42";
const KNOWN_DECIMALS = { [USDC_BASE]: 6, [EURC_BASE]: 6 };

/**
 * Building the whole index inside one request is not possible: PayAI alone is
 * 27 pages of 1.5 MB, and the runtime allows 30 seconds. So the index is built
 * across requests - each cold request spends a bounded budget extending it from
 * where the last one stopped, and every response reports how complete it is.
 *
 * A partial index is stated, never hidden. An agent told "not found" by a 12%
 * index has been misled; one told "not found, 12% indexed" has not.
 */
async function extendIndex(prev, budgetMs) {
  const index =
    prev && prev.entries && Date.now() - (prev.at ?? 0) < INDEX_TTL_MS
      ? prev
      : { at: Date.now(), sources: {}, entries: {} };

  const deadline = Date.now() + budgetMs;
  const size = () => Object.keys(index.entries).length;

  await Promise.all(
    Object.entries(BAZAAR).map(async ([name, base]) => {
      const state = index.sources[name] ?? {
        offset: 0,
        total: null,
        done: false,
        failures: 0,
        error: null,
      };
      index.sources[name] = state;
      if (state.done) return;

      while (Date.now() < deadline && size() < INDEX_MAX_ENTRIES) {
        let payload;
        try {
          const res = await fetch(
            `${base.replace(/\/+$/, "")}/discovery/resources?limit=${BAZAAR_PAGE}&offset=${state.offset}`,
            {
              headers: { accept: "application/json", "user-agent": "x402-vet/1.0 (catalog read)" },
              // Clamped to what is left of the budget: the deadline is checked
              // before a page starts, so an unclamped page can overrun it by
              // its whole timeout and push the request past the 30-second
              // ceiling - where the answer is lost and this progress with it.
              signal: AbortSignal.timeout(
                Math.max(1000, Math.min(PROBE_TIMEOUT_MS * 2, deadline - Date.now())),
              ),
            },
          );
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          payload = await res.json();
        } catch (error) {
          // One unreachable facilitator must not cost the caller the other
          // three. The gap is recorded and retried on the next cold request -
          // but a source that keeps failing is retired, or coverage could never
          // report itself complete and every request would re-pay for the same
          // failure.
          state.error = String((error && error.message) || error).slice(0, 80);
          state.failures += 1;
          if (state.failures >= 3) {
            state.done = true;
            state.unavailable = true;
          }
          return;
        }

        const items = Array.isArray(payload && payload.items) ? payload.items : [];
        for (const item of items) absorb(index.entries, item, name);

        const total = Number((payload && payload.pagination && payload.pagination.total) ?? NaN);
        if (Number.isFinite(total)) state.total = total;
        state.offset += items.length;
        state.error = null;
        state.failures = 0;

        // A short page does not mean the end: facilitators clamp `limit`
        // silently - thirdweb caps at 200 - and treating that as exhaustion
        // stops at a quarter of its catalog. Only an empty page, or reaching a
        // declared total, ends a source.
        if (items.length === 0 || (state.total !== null && state.offset >= state.total)) {
          state.done = true;
          return;
        }
      }
    }),
  );

  index.entries = index.entries;
  return index;
}

function absorb(entries, item, source) {
  // Bazaar implementations disagree on the field name: PayAI and Coinbase send
  // `resource`, ultravioletadao sends `url`. Reading only the first silently
  // discarded that catalog whole, which is the worst kind of gap - no error,
  // just 2,683 endpoints that never existed as far as the answer was concerned.
  const raw = [item.resource, item.url].find((value) => typeof value === "string" && value.trim());

  let resource;
  let insecure = false;
  try {
    resource = publicHttpsUrl(raw ? raw.trim() : "");
  } catch {
    // A plaintext endpoint is still a real endpoint, and an agent about to pay
    // one deserves to be told so rather than to be told it does not exist.
    // Dropping it here would repeat, quietly, the same mistake the `url` field
    // caused: roughly 1.2% of PayAI's catalog is http://, and it was vanishing
    // without a trace. It is indexed and flagged; the probe still refuses it.
    try {
      resource = publicHttpUrl(raw ? raw.trim() : "");
      insecure = true;
    } catch {
      return;
    }
  }

  const accepts = Array.isArray(item.accepts) ? item.accepts : [];
  const pick = accepts.find((a) => a && networkName(a.network) === "base") || accepts[0] || {};
  const key = resourceKey(resource);
  const existing = entries[key];

  const entry = {
    d: domainOf(resource),
    u: resource,
    m: String(item.method || "GET").toUpperCase() === "POST" ? "POST" : "GET",
    p: priceOf(pick),
    t: typeof pick.payTo === "string" ? pick.payTo.toLowerCase() : null,
    a: typeof pick.asset === "string" ? pick.asset.toLowerCase() : null,
    n: networkName(pick.network),
    s: String(item.serviceName || item.description || "").slice(0, 80) || null,
    f: existing ? [...new Set([...existing.f, source])] : [source],
    ...(insecure ? { x: true } : {}),
  };
  entries[key] = existing ? { ...existing, ...entry } : entry;
}

/**
 * Only assets whose decimals we actually know are priced. Guessing 18 or 6 for
 * an unrecognised token would put a wrong dollar figure next to a real
 * endpoint, which is worse than admitting the price is unknown.
 */
function priceOf(accept) {
  const decimals = KNOWN_DECIMALS[String(accept.asset ?? "").toLowerCase()];
  if (decimals === undefined) return null;
  const amount = Number(accept.amount ?? accept.maxAmountRequired);
  if (!Number.isFinite(amount)) return null;
  return amount / 10 ** decimals;
}

function networkName(value) {
  const network = String(value ?? "").trim().toLowerCase();
  if (network === "base" || network === "bse" || network === "eip155:8453") return "base";
  return network;
}

function domainOf(url) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function indexCoverage(index) {
  const sources = {};
  let known = 0;
  let fetched = 0;
  const states = Object.entries((index && index.sources) || {});
  let complete = states.length > 0;

  for (const [name, state] of states) {
    sources[name] = {
      indexed: state.offset,
      total: state.total,
      complete: state.done === true,
      ...(state.unavailable ? { unavailable: true } : {}),
      ...(state.error ? { error: state.error } : {}),
    };
    // A facilitator that clamps `limit` can report more rows fetched than the
    // total it declared. Coverage above 100% is a bug in the arithmetic, not
    // extra knowledge, so each source counts at most its own total.
    if (Number.isFinite(state.total)) known += state.total;
    fetched += Number.isFinite(state.total) ? Math.min(state.offset, state.total) : state.offset;
    if (!state.done) complete = false;
  }

  return {
    endpointsIndexed: Object.keys((index && index.entries) || {}).length,
    endpointsKnown: known || null,
    percent: known ? Math.min(100, Math.round((fetched / known) * 1000) / 10) : null,
    complete,
    sources,
  };
}

/**
 * An endpoint the traction directory has never heard of is not thereby
 * suspect - it is unmeasured. `unrated` says exactly that and can never be
 * confused with `ok`, which claims measured, distributed demand.
 */
function unratedRecord(resource, entry) {
  const original = entry.u || resource;
  return {
    name: entry.s || entry.d || original,
    slug: null,
    resource: original,
    method: entry.m,
    category: "",
    priceUsd: entry.p,
    networks: entry.n ? [networkName(entry.n)] : [],
    uptime30d: null,
    lastCheckedAt: null,
    netVolume30d: 0,
    usdPerBuyer: 0,
    buyers30d: 0,
    topBuyerShare: null,
    identicalPairCluster: 0,
    verdict: entry.x ? "check" : "unrated",
    why: [
      `listed by ${entry.f.join(", ")}; no traction data published anywhere`,
      // Plain HTTP is not a missing-data problem, it is a live one: payment
      // terms, including the receiving address, can be rewritten in transit.
      // That is a specific defect, so it earns `check` rather than `unrated`.
      ...(entry.x
        ? ["listed over plain HTTP, so its payment terms can be altered in transit"]
        : []),
    ],
    listedPayTo: entry.t,
    ...(entry.x ? { insecureTransport: true } : {}),
  };
}

/** Exact resource URL first, then any endpoint on the same host. */
function findInIndex(index, query) {
  const entries = (index && index.entries) || {};
  let q = String(query).trim();
  try {
    q = resourceKey(q);
  } catch {
    q = q.toLowerCase();
  }

  if (entries[q]) return { resource: entries[q].u || q, entry: entries[q], matchedBy: "resource" };

  const host = domainOf(q) || q;
  for (const [resource, entry] of Object.entries(entries)) {
    if (entry.d === host) return { resource: entry.u || resource, entry, matchedBy: "domain" };
  }
  return null;
}

function minutesSince(iso) {
  if (!iso) return null;
  const at = Date.parse(iso);
  return Number.isFinite(at) ? (Date.now() - at) / 60000 : null;
}

function num(value, fallback) {
  if (value === undefined) return fallback;
  if (value === null || value === "" || typeof value === "boolean") return NaN;
  const n = Number(value);
  return Number.isFinite(n) ? n : NaN;
}

/**
 * Both verbs are accepted, so a caller can probe the endpoint with a plain
 * `bankr x402 call <url>` before writing any code. GET carries filters as query
 * params, POST as a JSON body; a body wins where both are present.
 */
async function readInput(req) {
  const query = {};
  try {
    for (const [key, value] of new URL(req.url).searchParams) {
      query[key] = value === "true" ? true : value === "false" ? false : value;
    }
  } catch {
    // A request URL we cannot parse simply carries no query input.
  }

  if (req.method !== "POST") return query;
  try {
    const body = await req.json();
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("invalid body");
    return { ...query, ...body };
  } catch {
    throw new Error("invalid JSON body");
  }
}

// Probing touches third-party hosts and costs two outbound requests per row, so
// it stays opt-in and capped. The cap is set from measurement, not caution:
// probing the entire default result set - 27 services, 54 outbound requests -
// takes 1.7s in parallel, well inside the 30-second budget.
const PROBE_LIMIT = 50;

// Writable file scope for this service, as declared in bankr.x402.json.
const SNAPSHOT_PATH = "/x402/vet-shortlist/snapshot.json";
const INDEX_PATH = "/x402/vet-shortlist/index.json";
const INDEX_BUDGET_MS = 9000;
// The runtime kills a handler at 30 seconds and settles nothing, so the whole
// request works to a wall-clock deadline well inside it. Whatever the index
// reached by then is persisted and the next call carries on from there.
const REQUEST_BUDGET_MS = 20000;
const PROBE_RESERVE_MS = 9000;

function indexBudgetFrom(startedAt) {
  const remaining = REQUEST_BUDGET_MS - (Date.now() - startedAt);
  return Math.max(0, Math.min(INDEX_BUDGET_MS, remaining - PROBE_RESERVE_MS));
}

export async function evaluate(request, ctx) {
  const startedAt = Date.now();
  let input;
  try {
    input = await readInput(request);
  } catch {
    return Response.json(
      { ok: false, error: "invalid_request", attribution: ATTRIBUTION },
      { status: 400 },
    );
  }

  const category = typeof input.category === "string" ? input.category.trim().toLowerCase() : "";
  const network = typeof input.network === "string" ? networkName(input.network) : "";
  const maxPrice = input.maxPriceUsd === undefined ? null : num(input.maxPriceUsd, NaN);
  const minBuyers = num(input.minBuyers, 2);
  const minUptime = num(input.minUptime30d, 90);
  const minNet = num(input.minNetVolume30d, 10);
  const minPerBuyer = num(input.minUsdPerBuyer, 0.1);
  const limit = num(input.limit, 20);
  const includeThin = input.includeThin ?? false;
  const wantProbe = input.probe ?? false;
  const includeUnrated = input.includeUnrated ?? false;
  const invalid =
    (input.category !== undefined && typeof input.category !== "string") ||
    (input.network !== undefined && typeof input.network !== "string") ||
    [includeThin, wantProbe, includeUnrated].some((value) => typeof value !== "boolean") ||
    maxPrice !== null && (!Number.isFinite(maxPrice) || maxPrice < 0) ||
    !Number.isInteger(minBuyers) || minBuyers < 0 ||
    !Number.isFinite(minUptime) || minUptime < 0 || minUptime > 100 ||
    !Number.isFinite(minNet) || minNet < 0 ||
    !Number.isFinite(minPerBuyer) || minPerBuyer < 0 ||
    !Number.isInteger(limit) || limit < 1 || limit > 100;
  if (invalid) {
    return Response.json(
      { ok: false, error: "invalid_filter", attribution: ATTRIBUTION },
      { status: 400 },
    );
  }

  let snap;
  try {
    snap = await getSnapshot(ctx, SNAPSHOT_PATH);
  } catch {
    return Response.json(
      { ok: false, error: "directory_unavailable", attribution: ATTRIBUTION },
      { status: 503 },
    );
  }

  let results = snap.records
    .filter((r) => {
      if (r.verdict === "check") return false;
      // `thin` and `unrated` are different claims and are asked for
      // separately: one is "measured, and there is little", the other "nobody
      // measured". Folding them under one flag would return hundreds of
      // unweighed services to a caller who asked to see weak ones.
      if (r.verdict === "thin" && !includeThin) return false;
      if (r.verdict === "unrated" && !includeUnrated) return false;
      if (category && r.category.toLowerCase() !== category) return false;
      if (network && !r.networks.some((n) => networkName(n) === network)) return false;
      if (maxPrice !== null && !Number.isNaN(maxPrice)) {
        if (r.priceUsd === null || r.priceUsd > maxPrice) return false;
      }
      if (r.uptime30d === null || r.uptime30d < minUptime) return false;
      // Demand thresholds judge measured demand, so they are applied only to
      // services that were measured. Running them against an unrated record -
      // which has no demand figures by definition, not a figure of zero -
      // would silently reject everything `includeUnrated` was asked for.
      if (r.verdict !== "unrated") {
        if (r.buyers30d < minBuyers) return false;
        if (r.netVolume30d < minNet) return false;
        if (r.usdPerBuyer < minPerBuyer) return false;
      }
      return true;
    })
    .sort((a, b) => b.netVolume30d - a.netVolume30d)
    .slice(0, limit);

  // The traction directory covers roughly 600 services. Asking for unrated endpoints
  // opens the other ~40,600, which have prices and receivers but no measured
  // demand at all - useful for "what is cheap on this network", never for
  // "what is proven".
  let coverage = null;
  if (includeUnrated) {
    const index = await getIndex(ctx, INDEX_PATH, indexBudgetFrom(startedAt));
    coverage = indexCoverage(index);
    const directoryByDomain = new Map();
    for (const record of snap.records) {
      const domain = domainOf(record.resource);
      if (!domain) continue;
      const resources = directoryByDomain.get(domain) ?? [];
      resources.push(record.resource);
      directoryByDomain.set(domain, resources);
    }

    const extra = Object.entries(index.entries || {})
      .filter(([resource, entry]) => {
        const directoryResources = directoryByDomain.get(domainOf(resource)) ?? [];
        if (directoryResources.some((known) => urlsShareResource(resource, known))) return false;
        if (network && networkName(entry.n) !== network) return false;
        if (maxPrice !== null && (entry.p === null || entry.p > maxPrice)) return false;
        return true;
      })
      .map(([resource, entry]) => unratedRecord(resource, entry))
      .sort((a, b) => (a.priceUsd ?? Infinity) - (b.priceUsd ?? Infinity))
      .slice(0, Math.max(0, limit - results.length));
    results = [...results, ...extra];
  }

  let probed = 0;
  if (wantProbe && results.length) {
    const head = results.slice(0, PROBE_LIMIT);
    const probes = await Promise.all(
      head.map((r) =>
        r.slug
          ? probeService({ slug: r.slug })
          : probeService({ url: r.resource, method: r.method, catalogPayTo: r.listedPayTo ?? null }),
      ),
    );
    results = [...head.map((r, i) => applyProbe(r, probes[i])), ...results.slice(PROBE_LIMIT)];
    probed = head.length;
  }

  return Response.json({
    ok: true,
    product: "x402-vet-shortlist",
    asOf: new Date(snap.at).toISOString(),
    checked: snap.records.length + (coverage?.endpointsIndexed ?? 0),
    returned: results.length,
    probed,
    filter: { category, network, maxPrice, minBuyers, minUptime, minNet, minPerBuyer, includeThin, includeUnrated, limit },
    ...(coverage ? { coverage } : {}),
    results,
    note: NOTE,
    attribution: ATTRIBUTION,
  });
}

function verdictLabel(verdict) {
  if (verdict === "ok") return "Recommended";
  if (verdict === "thin") return "Limited evidence";
  if (verdict === "check") return "Review before paying";
  return "Unrated";
}

function priceLabel(value) {
  if (!Number.isFinite(value)) return "unknown";
  return `$${Number(value).toLocaleString("en-US", { maximumFractionDigits: 6 })}`;
}

function formatFilters(filter) {
  const values = [];
  if (filter.category) values.push(`directory category ${filter.category}`);
  if (filter.network) values.push(`network ${filter.network}`);
  if (filter.maxPrice !== null) values.push(`maximum price ${priceLabel(filter.maxPrice)}`);
  values.push(
    `directory services: at least ${filter.minUptime}% uptime`,
    `measured services: at least ${filter.minBuyers} buyers`,
    `at least ${priceLabel(filter.minNet)} net volume`,
    `at least ${priceLabel(filter.minPerBuyer)} per buyer`,
  );
  if (filter.includeThin) values.push("limited-evidence services included");
  if (filter.includeUnrated) {
    // Naming the demand thresholds without this would claim they were applied
    // to every row, when unrated rows are by definition exempt from them.
    values.push("unrated services included without demand thresholds");
  }
  return values.join("; ");
}

function formatShortlistResponse(body, status) {
  if (!body.ok || status >= 400) {
    const title = status === 503 ? "Shortlist temporarily unavailable" : "Invalid shortlist request";
    const detail = status === 503
      ? "The service directory could not be refreshed. No payment should be collected."
      : "Check the filters and try again. No payment should be collected.";
    return [`# ${title}`, "", detail, "", `Data attribution: ${body.attribution || ATTRIBUTION}`].join("\n");
  }

  const lines = [
    "# x402 services worth calling",
    "",
    `Reviewed ${body.checked} services and found ${body.returned} matching results.`,
    `Filters: ${formatFilters(body.filter)}.`,
    `Live checks completed: ${body.probed}.`,
  ];

  if (!body.results.length) {
    lines.push("", "No services matched these filters.");
  }

  body.results.forEach((result, index) => {
    lines.push(
      "",
      `## ${index + 1}. ${result.name || result.resource} — ${verdictLabel(result.verdict)}`,
      `Endpoint: ${result.resource}`,
      `Price per call: ${priceLabel(result.priceUsd)}`,
      `Networks: ${result.networks?.length ? result.networks.join(", ") : "unknown"}`,
    );
    if (result.liveProbe) lines.push(`Live payment check: ${result.liveProbe}`);
    if (result.liveProbeReason) lines.push(`Live check detail: ${result.liveProbeReason}`);
    if (result.payToChanged === false) lines.push("Payment recipient: unchanged from the catalog");
    if (result.payToChanged === true) lines.push("Payment recipient: differs from the catalog; verify it before paying");
    if (result.why?.length) lines.push(`Why: ${result.why.join("; ")}`);
  });

  if (body.coverage) {
    lines.push(
      "",
      `Catalog coverage: ${body.coverage.percent ?? "unknown"}% (${body.coverage.endpointsIndexed} endpoints indexed).`,
    );
  }

  lines.push(
    "",
    `Data snapshot: ${body.asOf}`,
    "",
    body.note,
    "",
    `Data attribution: ${body.attribution}`,
  );
  return lines.join("\n");
}

export default async function handler(request, ctx) {
  const response = await evaluate(request, ctx);
  const body = await response.json();
  return new Response(formatShortlistResponse(body, response.status), {
    status: response.status,
    headers: {
      "content-type": "text/markdown; charset=utf-8",
      "cache-control": "private, no-store",
    },
  });
}
