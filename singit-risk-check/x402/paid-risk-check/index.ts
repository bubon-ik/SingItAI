const SINGIT = "0xc2c1e0b7c401e6217193732272444d928646eba3";
const USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913";
const EURC = "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42";

export default async function handler(req) {
  if (req.method !== "POST") {
    return Response.json({ ok: false, error: "POST required" }, { status: 405 });
  }

  const body = await readBody(req);
  const requirement = normalizeRequirement(
    body.paymentRequirements || body.requirements || body.payment,
  );

  if (!requirement) {
    return Response.json(
      { ok: false, error: "paymentRequirements is required" },
      { status: 400 },
    );
  }

  return Response.json(analyze(requirement));
}

async function readBody(req) {
  try {
    return await req.json();
  } catch {
    return {};
  }
}

function normalizeRequirement(value) {
  if (!value || typeof value !== "object") return null;
  if (Array.isArray(value)) return value[0] || null;
  if (Array.isArray(value.accepts)) return value.accepts[0] || null;
  if (Array.isArray(value.paymentRequirements)) return value.paymentRequirements[0] || null;
  return value;
}

function analyze(requirement) {
  const extra = objectOrEmpty(requirement.extra);
  const assetAddress = lower(requirement.asset || requirement.tokenAddress);
  const network = String(requirement.network || requirement.chain || "");
  const amountAtomic = String(
    requirement.maxAmountRequired || requirement.amount || requirement.price || "",
  );
  const receiver = lower(requirement.payTo || requirement.receiver || requirement.recipient);
  const replayValue =
    requirement.nonce ||
    requirement.paymentIntent ||
    requirement.paymentId ||
    requirement.validAfter ||
    requirement.validBefore ||
    extra.nonce;
  const symbol = assetAddress === SINGIT ? "SINGIT" : assetAddress === USDC ? "USDC" : assetAddress === EURC ? "EURC" : null;
  const decimals = symbol === "SINGIT" ? 18 : symbol ? 6 : null;
  const transferMethod =
    extra.assetTransferMethod ||
    requirement.assetTransferMethod ||
    (assetAddress === USDC || assetAddress === EURC ? "eip3009" : assetAddress ? "permit2" : null);

  const checks = [
    network === "eip155:8453" || network.toLowerCase() === "base" || network === "8453"
      ? pass("network_base", "Payment is on Base.")
      : warn("network_base", "Payment network is missing or not Base."),
    receiver
      ? pass("receiver_present", "Payment receiver is explicit.")
      : fail("receiver_present", "Payment receiver is missing."),
    amountAtomic
      ? pass("amount_present", "Payment amount is explicit.")
      : warn("amount_present", "Payment amount is missing."),
    replayValue
      ? pass("replay_protection", "Payment includes a nonce, intent, or validity window.")
      : fail("replay_protection", "Payment has no visible replay-protection field."),
    assetAddress && assetAddress !== USDC && assetAddress !== EURC
      ? warn("custom_token", "Payment uses a custom ERC-20; first-time payers may need Permit2 approval.")
      : pass("custom_token", "Payment uses a standard x402 asset."),
    pass("payment_scheme", transferMethod === "permit2" ? "Payment uses Permit2." : "Payment scheme is fixed or auto-selected."),
  ];

  const riskLevel = checks.some((check) => check.status === "fail")
    ? "high"
    : checks.filter((check) => check.status === "warning").length >= 2
      ? "medium"
      : "low";

  return {
    ok: true,
    product: "sign402-risk-check",
    riskLevel,
    summary: {
      network: network || null,
      scheme: requirement.scheme || requirement.paymentScheme || null,
      asset: {
        address: assetAddress || null,
        symbol,
        decimals,
        transferMethod,
      },
      amount: {
        atomic: amountAtomic || null,
        display: formatAmount(amountAtomic, decimals, symbol),
      },
      receiver: receiver || null,
      resource: requirement.resource || requirement.resourceUrl || requirement.url || null,
    },
    checks,
    recommendation:
      riskLevel === "high"
        ? "Reject until failed checks are fixed."
        : riskLevel === "medium"
          ? "Require explicit user approval before payment."
          : "Payment looks acceptable for a bounded Sign402 policy.",
  };
}

function formatAmount(amountAtomic, decimals, symbol) {
  if (!amountAtomic || !Number.isInteger(decimals)) return amountAtomic || null;
  try {
    const amount = BigInt(amountAtomic);
    const base = 10n ** BigInt(decimals);
    const whole = amount / base;
    const fraction = (amount % base).toString().padStart(decimals, "0").replace(/0+$/, "");
    return `${fraction ? `${whole}.${fraction}` : whole}${symbol ? ` ${symbol}` : ""}`;
  } catch {
    return amountAtomic;
  }
}

function lower(value) {
  return typeof value === "string" ? value.toLowerCase() : "";
}

function objectOrEmpty(value) {
  return value && typeof value === "object" ? value : {};
}

function pass(name, message) {
  return { name, status: "pass", message };
}

function warn(name, message) {
  return { name, status: "warning", message };
}

function fail(name, message) {
  return { name, status: "fail", message };
}
