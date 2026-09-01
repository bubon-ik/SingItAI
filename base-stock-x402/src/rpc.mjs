import { BASE_RPC_URL } from "./chain.mjs";

let nextId = 1;

// Some public Base endpoints refuse a request with no user agent. Sending one
// costs nothing and removes a failure that looks like a chain problem.
const HEADERS = { "content-type": "application/json", "user-agent": "sign402-base-stock-x402" };

/** Send one JSON-RPC call. Throws on transport errors, returns {result} or {error}. */
export async function rpc(method, params = [], { rpcUrl = BASE_RPC_URL } = {}) {
  const response = await fetch(rpcUrl, {
    method: "POST",
    headers: HEADERS,
    body: JSON.stringify({ jsonrpc: "2.0", id: nextId++, method, params }),
  });
  if (!response.ok) {
    throw new Error(`rpc ${method} failed: HTTP ${response.status}`);
  }
  return response.json();
}

/** Batch several calls into one round-trip. Order of results matches order of calls. */
export async function rpcBatch(calls, { rpcUrl = BASE_RPC_URL } = {}) {
  if (calls.length === 0) return [];
  const body = calls.map(([method, params = []]) => ({
    jsonrpc: "2.0",
    id: nextId++,
    method,
    params,
  }));
  const response = await fetch(rpcUrl, {
    method: "POST",
    headers: HEADERS,
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`rpc batch failed: HTTP ${response.status}`);
  }
  const payload = await response.json();
  const byId = new Map(payload.map((entry) => [entry.id, entry]));
  return body.map((entry) => byId.get(entry.id));
}

/** eth_call that never throws — returns {data} on success or {revert} on revert. */
export async function ethCall(to, data, options = {}) {
  const response = await rpc("eth_call", [{ to, data }, "latest"], options);
  if (response.error) {
    return { revert: response.error.data ?? null, message: response.error.message };
  }
  return { data: response.result };
}

/** Left-pad a value into one 32-byte ABI word. */
export function word(value) {
  const hex = typeof value === "string" && value.startsWith("0x")
    ? value.slice(2)
    : BigInt(value).toString(16);
  return hex.padStart(64, "0");
}

/** An address as one ABI word. */
export function addressWord(address) {
  return String(address).toLowerCase().replace(/^0x/, "").padStart(64, "0");
}

/** Read the nth 32-byte word out of returndata. */
export function wordAt(data, index) {
  const start = 2 + index * 64;
  return "0x" + String(data).slice(start, start + 64);
}
