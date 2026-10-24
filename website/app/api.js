// The SingIt web API (/web/v1). The session is an HttpOnly cookie; every POST
// also carries the CSRF token the API returned at sign-in.

const base = () => window.SINGIT_APP_CONFIG.apiBase.replace(/\/$/, "");
const CSRF_KEY = "singit.csrf";
const SESSION_KEY = "singit.session";

function readSession() {
  try {
    const saved = JSON.parse(localStorage.getItem(SESSION_KEY) || "null");
    if (saved?.csrfToken && saved?.account) return saved;
    const legacy = localStorage.getItem(CSRF_KEY);
    return legacy ? { csrfToken: legacy, account: null } : null;
  } catch { return null; }
}

// Capture once, not on each request: another tab must never silently replace
// the credentials of the account this tab is displaying.
let tabSession = readSession();
let sessionVersion = 0;
const sameSession = (a, b) => a?.csrfToken === b?.csrfToken && a?.account === b?.account;

function changedSession() {
  setCsrf(null);  // clear an expired local session, but preserve a newer tab's one
  window.dispatchEvent(new Event("singit:session-changed"));
  return new ApiError(409, { error: "session_changed", message: "Your account changed in another tab. Sign in again." });
}

window.addEventListener("storage", (event) => {
  if ((event.key === SESSION_KEY || event.key === CSRF_KEY || event.key === null)
      && !sameSession(tabSession, readSession())) changedSession();
});

export class ApiError extends Error {
  constructor(status, body) {
    super(body?.message || body?.text || `Request failed (${status}).`);
    this.status = status;
    this.code = body?.error;
    this.body = body;
  }
}

export function csrf() {
  return tabSession?.csrfToken || "";
}

export function setCsrf(token, account = tabSession?.account) {
  const previous = tabSession;
  tabSession = token ? { csrfToken: token, account } : null;
  sessionVersion++;
  try {
    if (token) localStorage.setItem(SESSION_KEY, JSON.stringify(tabSession));
    else if (sameSession(previous, readSession())) localStorage.removeItem(SESSION_KEY);
    // A stale tab must not erase another tab's new credentials.
    if (token || localStorage.getItem(CSRF_KEY) === previous?.csrfToken) localStorage.removeItem(CSRF_KEY);
  } catch { /* private mode: the in-memory session still works */ }
}

async function call(method, path, body) {
  const publicAuth = path === "/auth/nonce" || path === "/auth/verify";
  const version = sessionVersion;
  const identity = tabSession;
  if (!publicAuth && !identity) throw new ApiError(401, { message: "Sign in first." });
  const headers = { Accept: "application/json" };
  if (!publicAuth) {
    headers["X-SingIt-CSRF"] = identity.csrfToken;
    if (identity.account) headers["X-SingIt-Account"] = identity.account;
  }
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
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
  // Ignore late replies to an account that this tab has already left. Payments
  // are never retried automatically, even when the response is discarded.
  if (version !== sessionVersion) throw new ApiError(409, { error: "session_changed", message: "Your session changed. This response belongs to the previous session." });
  if (!publicAuth && (response.status === 401 || data.error === "session_changed")) throw changedSession();
  if (!response.ok || data.ok === false) throw new ApiError(response.status, data);
  return data;
}

// The free public spend report (sign402_gateway/wallet_reconciliation.py): no session, no cookies.
async function walletReport(address, days = 30) {
  const response = await fetch(`${new URL(base(), location.href).origin}/v1/wallet/${encodeURIComponent(address)}/reconciliation?days=${days}`);
  if (!response.ok) throw new ApiError(response.status, await response.json().catch(() => ({})));
  return response.json();
}

export const api = {
  walletReport,
  nonce: (address) => call("POST", "/auth/nonce", { address }),
  verify: (message, signature, utm) => call("POST", "/auth/verify", { message, signature, utm }),
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
  chats: () => call("GET", "/chats"),
  chat: (id) => call("GET", `/chats/${encodeURIComponent(id)}`),
  say: (chatId, text, lang) => call("POST", "/chats/message", { chatId, text, ...(lang ? { lang } : {}) }),
  usage: () => call("GET", "/usage"),
  act: (chatId, action) => call("POST", "/chats/action", { chatId, action }),
  progress: (attempt) => call("GET", `/shop/progress?id=${encodeURIComponent(attempt)}`),
  models: () => call("GET", "/chat/models"),
  chooseModel: (model) => call("POST", "/chat/model", { model }),
  updateChat: (chatId, changes) => call("POST", "/chats/update", { chatId, ...changes }),
  deleteChat: (chatId) => call("POST", "/chats/delete", { chatId }),
  pushKey: () => call("GET", "/push/key"),
  pushSubscribe: (subscription) => call("POST", "/push/subscribe", { subscription }),
  pushUnsubscribe: (endpoint) => call("POST", "/push/unsubscribe", { endpoint }),
  pushTest: () => call("POST", "/push/test"),
  apiKeys: () => call("GET", "/api-keys"),
  apiKeyPrepare: (terms) => call("POST", "/api-keys/prepare", terms),
  apiKeyCreate: (message, signature) => call("POST", "/api-keys", { message, signature }),
  apiKeyRevoke: (id) => call("POST", "/api-keys/revoke", { id }),
  balance: () => call("GET", "/balance"),
  balanceSettings: (topupUsd, thresholdUsd, autoTopup) => call("POST", "/balance/settings", { topupUsd, thresholdUsd, autoTopup }),
  balanceTopup: (amountUsd) => call("POST", "/balance/topup", { amountUsd }),
  balanceRefund: () => call("POST", "/balance/refund"),
};
