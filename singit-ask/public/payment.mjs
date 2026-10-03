// A single, explicitly signed Base USDC test. No keys, signatures or payment
// authorizations are persisted. Compatible with @x402/evm 2.28 exact/EIP-3009.
export const ENDPOINT = "https://ask.singitai.app/v1/chat/completions";
export const PAY_TO = "0xC23d1Dc0f5fCe1abfFB051e06cB93f0329968B4e";
export const ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
export const QUESTION = { messages: [{ role: "user", content: "Reply with exactly: OK" }], max_tokens: 32 };
export const sameAddress = (a, b) => typeof a === "string" && typeof b === "string" && a.toLowerCase() === b.toLowerCase();
export const decode = (s) => JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(s), c => c.charCodeAt(0))));
export const encode = (v) => btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(v))));

export function validateOffer(offer) {
  if (offer?.x402Version !== 2 || offer.resource?.url !== ENDPOINT || !Array.isArray(offer.accepts)) {
    throw new Error("Unexpected payment endpoint or protocol. Nothing was signed.");
  }
  const options = offer.accepts.filter(a => a.network === "eip155:8453");
  if (options.length !== 1) throw new Error("Expected one Base payment option.");
  const a = options[0];
  if (a.scheme !== "exact" || a.amount !== "3000" || !sameAddress(a.asset, ASSET) || !sameAddress(a.payTo, PAY_TO)
      || a.extra?.name !== "USD Coin" || a.extra?.version !== "2"
      || (a.extra?.assetTransferMethod && a.extra.assetTransferMethod !== "eip3009")
      || !Number.isInteger(a.maxTimeoutSeconds) || a.maxTimeoutSeconds < 1 || a.maxTimeoutSeconds > 300) {
    throw new Error("Quote differs from the reviewed 0.003 USDC payment on Base. Nothing was signed.");
  }
  return a;
}

export function typedPayment(payer, accepted, nowSeconds, nonce) {
  return {
    domain: { name: "USD Coin", version: "2", chainId: 8453, verifyingContract: ASSET },
    types: {
      EIP712Domain: [{ name: "name", type: "string" }, { name: "version", type: "string" },
        { name: "chainId", type: "uint256" }, { name: "verifyingContract", type: "address" }],
      TransferWithAuthorization: [{ name: "from", type: "address" }, { name: "to", type: "address" },
        { name: "value", type: "uint256" }, { name: "validAfter", type: "uint256" },
        { name: "validBefore", type: "uint256" }, { name: "nonce", type: "bytes32" }],
    },
    primaryType: "TransferWithAuthorization",
    message: { from: payer, to: PAY_TO, value: "3000", validAfter: "0",
      validBefore: String(nowSeconds + accepted.maxTimeoutSeconds), nonce },
  };
}

export async function runTest({ provider, payer, fetchImpl = fetch, onStatus = () => {}, onSubmitting = () => {} }) {
  if (!/^0x[0-9a-fA-F]{40}$/.test(payer)) throw new Error("Set a valid payer address first.");
  const checkWallet = async () => {
    const accounts = await provider.request({ method: "eth_accounts" });
    if (!sameAddress(accounts[0], payer)) throw new Error("Select the expected payer account in MetaMask.");
    if (BigInt(await provider.request({ method: "eth_chainId" })) !== 8453n) throw new Error("Select the Base network in MetaMask.");
  };
  await checkWallet();
  const post = (headers = {}) => fetchImpl(ENDPOINT, { method: "POST", redirect: "error",
    headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(QUESTION),
    signal: AbortSignal.timeout(90000) });
  onStatus("Checking a fresh payment quote…");
  const unpaid = await post();
  if (unpaid.status !== 402 || !unpaid.headers.get("payment-required")) throw new Error("Server did not return an x402 quote.");
  const offer = decode(unpaid.headers.get("payment-required"));
  const accepted = validateOffer(offer);
  const nonce = "0x" + Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2, "0")).join("");
  const typed = typedPayment(payer, accepted, Math.floor(Date.now() / 1000), nonce);
  await checkWallet();
  onStatus("Confirm the 0.003 USDC payment authorization in MetaMask.");
  const signature = await provider.request({ method: "eth_signTypedData_v4", params: [payer, JSON.stringify(typed)] });
  if (!/^0x[0-9a-fA-F]{130}$/.test(signature)) throw new Error("Unexpected signature format; this test supports a standard EOA wallet.");
  await checkWallet();
  if (Math.floor(Date.now() / 1000) >= Number(typed.message.validBefore)) throw new Error("Authorization expired before submission. Nothing was sent.");
  const payload = { x402Version: 2, accepted, resource: offer.resource, payload: { authorization: typed.message, signature } };
  onSubmitting(); // The UI locks this attempt before it can leave the browser.
  onStatus("Payment submitted. Waiting for settlement and the answer; do not pay again.");
  const response = await post({ "PAYMENT-SIGNATURE": encode(payload) }); // Never retry automatically.
  const result = await response.json();
  const receipt = response.headers.get("payment-response");
  const settlement = receipt ? decode(receipt) : null;
  if (!response.ok || settlement?.success !== true || settlement.network !== "eip155:8453"
      || !/^0x[0-9a-fA-F]{64}$/.test(settlement.transaction)
      || !sameAddress(settlement.payer, payer) || typeof result.choices?.[0]?.message?.content !== "string") {
    throw new Error(`HTTP ${response.status}: a complete payment receipt and answer were not received. Do not pay again until this attempt has been checked.`);
  }
  return { answer: result.choices[0].message.content, transaction: settlement.transaction };
}
