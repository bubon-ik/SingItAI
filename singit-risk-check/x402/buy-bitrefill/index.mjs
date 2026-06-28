export default async function handler(req) {
  if (req.method !== "POST") {
    return Response.json({ ok: false, error: "POST required" }, { status: 405 });
  }

  const body = await readBody(req);
  const quoteId = string(body.quoteId);
  const fulfillmentToken = string(body.fulfillmentToken);

  if (!quoteId || !fulfillmentToken) {
    return Response.json(
      { ok: false, error: "quoteId and fulfillmentToken are required" },
      { status: 400 },
    );
  }

  const gatewayUrl = env("SIGN402_GATEWAY_INTERNAL_URL");
  const serviceSecret = env("SIGN402_BANKR_FULFILLMENT_SECRET");

  const response = await fetch(`${gatewayUrl.replace(/\/$/, "")}/internal/prepare-bitrefill-settlement`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${serviceSecret}`,
    },
    body: JSON.stringify({ quoteId, fulfillmentToken }),
  });

  const payload = await safeJson(response);
  if (!response.ok || !payload.ok) {
    return Response.json(
      { ok: false, quoteId, error: payload.error || "fulfillment failed" },
      { status: response.ok ? 400 : response.status },
    );
  }

  const settleAmount = payload.settleAmountAtomic || payload.maxSingitAtomic;
  const headers = settleAmount ? { "X-402-Settle-Amount": String(settleAmount) } : {};
  return Response.json(
    {
      ok: true,
      quoteId,
      orderId: payload.orderId,
      status: payload.status || "ready_for_singit_settlement",
      pricingMode: payload.pricingMode || "fixed",
      settleAmountAtomic: String(settleAmount),
    },
    { headers },
  );
}

async function readBody(req) {
  try {
    return await req.json();
  } catch {
    return {};
  }
}

async function safeJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

function string(value) {
  return typeof value === "string" ? value.trim() : "";
}

function env(name) {
  const value = globalThis.process?.env?.[name] || globalThis[name];
  if (!value) throw new Error(`${name} is required`);
  return String(value);
}
