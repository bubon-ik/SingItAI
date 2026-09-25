// The SingIt web API (/web/v1). The session is an HttpOnly cookie; every POST
// also carries the CSRF token the API returned at sign-in.

const base = () => window.SINGIT_APP_CONFIG.apiBase.replace(/\/$/, "");
const CSRF_KEY = "singit.csrf";

export class ApiError extends Error {
  constructor(status, body) {
    super(body?.message || body?.text || `Request failed (${status}).`);
    this.status = status;
    this.code = body?.error;
    this.body = body;
  }
}

export function csrf() {
  try { return localStorage.getItem(CSRF_KEY) || ""; } catch { return ""; }
}

export function setCsrf(token) {
  try { token ? localStorage.setItem(CSRF_KEY, token) : localStorage.removeItem(CSRF_KEY); } catch { /* private mode */ }
}

async function call(method, path, body) {
  const headers = { Accept: "application/json" };
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
    headers["X-SingIt-CSRF"] = csrf();
  }
  let response;
  try {
    response = await fetch(base() + path, {
      method, headers, credentials: "include",
      body: method === "POST" ? JSON.stringify(body || {}) : undefined,
    });
  } catch {
    throw new ApiError(0, { message: "Could not reach SingIt. Check your connection and try again." });
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) throw new ApiError(response.status, data);
  return data;
}

export const api = {
  nonce: (address) => call("POST", "/auth/nonce", { address }),
  verify: (message, signature) => call("POST", "/auth/verify", { message, signature }),
  logout: () => call("POST", "/auth/logout"),
  session: () => call("GET", "/session"),
  allowance: () => call("GET", "/allowance"),
  setup: (dailyCap, perPurchaseCap, days) => call("POST", "/allowance/setup", { dailyCap, perPurchaseCap, days }),
  prepare: (kind, body) => call("POST", `/allowance/${kind}/prepare`, body),
  submit: (kind, body) => call("POST", `/allowance/${kind}/submit`, body),
  operation: (id) => call("GET", `/allowance/operations/${encodeURIComponent(id)}`),
  pause: () => call("POST", "/allowance/pause"),
  linkTelegram: () => call("POST", "/link/telegram"),
  unlinkTelegram: () => call("POST", "/link/telegram/remove"),
  tools: () => call("GET", "/shop/tools"),
  toolQuote: (body) => call("POST", "/shop/tools/quote", body),
  toolBuy: (quoteId) => call("POST", "/shop/tools/buy", { quoteId }),
  bitrefillSearch: (query, country) => call("POST", "/shop/bitrefill/search", { query, country }),
  bitrefillQuote: (productId, pkg) => call("POST", "/shop/bitrefill/quote", { productId, package: pkg }),
  bitrefillBuy: (quoteId) => call("POST", "/shop/bitrefill/buy", { quoteId }),
  purchases: (offset = 0) => call("GET", `/purchases?offset=${offset}`),
  reveal: (purchaseId) => call("POST", "/purchases/reveal", { purchaseId }),
};
