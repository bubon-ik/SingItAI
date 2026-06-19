const BASE_CAIP2 = "eip155:8453";
const SINGIT_ADDRESS = "0xc2c1e0b7c401e6217193732272444d928646eba3";
const USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913";
const EURC_ADDRESS = "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42";

const KNOWN_ASSETS = {
  [SINGIT_ADDRESS]: {
    symbol: "SINGIT",
    decimals: 18,
    defaultTransferMethod: "permit2",
  },
  [USDC_ADDRESS]: {
    symbol: "USDC",
    decimals: 6,
    defaultTransferMethod: "eip3009",
  },
  [EURC_ADDRESS]: {
    symbol: "EURC",
    decimals: 6,
    defaultTransferMethod: "eip3009",
  },
};

export function analyzePaymentRisk(input) {
  const rawRequirement = selectRequirement(input?.paymentRequirements);
  const summary = summarizeRequirement(rawRequirement, input?.url);
  const checks = buildChecks(summary, rawRequirement);
  const riskLevel = riskLevelFromChecks(checks);

  return {
    ok: true,
    product: "sign402-risk-check",
    riskLevel,
    summary,
    checks,
    recommendation: recommendationFor(riskLevel),
  };
}

export function summarizeRequirement(requirement, fallbackResource) {
  const assetAddress = normalizeAddress(
    findFirst(requirement, ["asset", "tokenAddress", "token", "currencyAddress"]),
  );
  const knownAsset = assetAddress ? KNOWN_ASSETS[assetAddress] : null;
  const symbol =
    knownAsset?.symbol ||
    stringOrNull(findFirst(requirement, ["currency", "assetSymbol", "symbol"]));
  const decimals =
    knownAsset?.decimals ??
    numberOrNull(findFirst(requirement, ["decimals", "assetDecimals"]));
  const amountAtomic = stringOrNull(
    findFirst(requirement, ["maxAmountRequired", "amount", "value", "price"]),
  );
  const transferMethod =
    stringOrNull(findFirst(requirement, ["assetTransferMethod", "transferMethod"])) ||
    knownAsset?.defaultTransferMethod ||
    null;

  return {
    network: stringOrNull(findFirst(requirement, ["network", "chain", "chainId"])),
    scheme: stringOrNull(findFirst(requirement, ["scheme", "paymentScheme"])),
    asset: {
      address: assetAddress,
      symbol,
      decimals,
      transferMethod,
    },
    amount: {
      atomic: amountAtomic,
      display: formatAmount(amountAtomic, decimals, symbol),
    },
    receiver: normalizeAddress(
      findFirst(requirement, ["payTo", "receiver", "recipient", "to"]),
    ),
    resource:
      stringOrNull(findFirst(requirement, ["resource", "resourceUrl", "url"])) ||
      fallbackResource ||
      null,
  };
}

function buildChecks(summary, requirement) {
  const checks = [
    checkNetwork(summary.network),
    checkReceiver(summary.receiver),
    checkAmount(summary.amount.atomic),
    checkReplayProtection(requirement),
    checkCustomToken(summary.asset),
    checkPaymentScheme(summary.scheme, summary.asset.transferMethod),
  ];

  if (summary.resource) {
    checks.push(checkResourceUrl(summary.resource));
  }

  return checks;
}

function checkNetwork(network) {
  if (!network) {
    return warning("network_base", "Payment network is missing; require manual review.");
  }
  const normalized = network.toLowerCase();
  if (normalized === BASE_CAIP2 || normalized === "base" || normalized === "8453") {
    return pass("network_base", "Payment is on Base.");
  }
  return warning("network_base", `Payment is on ${network}, not Base.`);
}

function checkReceiver(receiver) {
  if (receiver) {
    return pass("receiver_present", "Payment receiver is explicit.");
  }
  return fail("receiver_present", "Payment receiver is missing.");
}

function checkAmount(amount) {
  if (amount) {
    return pass("amount_present", "Payment amount is explicit.");
  }
  return warning("amount_present", "Payment amount is missing.");
}

function checkReplayProtection(requirement) {
  const replayField = findFirst(requirement, [
    "nonce",
    "paymentIntent",
    "paymentId",
    "validAfter",
    "validBefore",
    "authorization",
  ]);
  if (replayField) {
    return pass("replay_protection", "Payment includes a nonce, intent, or validity window.");
  }
  return fail("replay_protection", "Payment has no visible nonce, intent, or validity window.");
}

function checkCustomToken(asset) {
  if (!asset.address) {
    return warning("custom_token", "Payment asset address is missing.");
  }
  if (asset.address === USDC_ADDRESS || asset.address === EURC_ADDRESS) {
    return pass("custom_token", `Payment uses ${asset.symbol}.`);
  }
  return warning(
    "custom_token",
    `Payment uses ${asset.symbol || "a custom ERC-20"}; first-time payers may need Permit2 approval.`,
  );
}

function checkPaymentScheme(scheme, transferMethod) {
  if (scheme?.toLowerCase() === "upto") {
    return pass("payment_scheme", "Payment authorizes a capped x402 amount.");
  }
  if (transferMethod?.toLowerCase() === "permit2") {
    return pass("payment_scheme", "Payment uses the Permit2 rail.");
  }
  return pass("payment_scheme", "Payment scheme is fixed or auto-selected.");
}

function checkResourceUrl(resource) {
  if (resource.startsWith("https://")) {
    return pass("resource_url", "Resource URL uses HTTPS.");
  }
  if (resource.startsWith("http://")) {
    return warning("resource_url", "Resource URL uses plain HTTP.");
  }
  return warning("resource_url", "Resource URL is not a standard HTTP(S) URL.");
}

function riskLevelFromChecks(checks) {
  if (checks.some((check) => check.status === "fail")) return "high";
  const warningCount = checks.filter((check) => check.status === "warning").length;
  if (warningCount >= 2) return "medium";
  if (warningCount === 1) return "low";
  return "low";
}

function recommendationFor(riskLevel) {
  if (riskLevel === "high") {
    return "Do not approve this payment until the failed checks are fixed.";
  }
  if (riskLevel === "medium") {
    return "Require explicit user approval before payment.";
  }
  return "Payment looks acceptable for a bounded Sign402 policy, but still require normal approval.";
}

function selectRequirement(value) {
  if (!value || typeof value !== "object") return value;
  if (Array.isArray(value)) return value[0] || {};
  if (Array.isArray(value.accepts)) return value.accepts[0] || {};
  if (Array.isArray(value.paymentRequirements)) return value.paymentRequirements[0] || {};
  if (value.paymentRequirements && typeof value.paymentRequirements === "object") {
    return selectRequirement(value.paymentRequirements);
  }
  return value;
}

function findFirst(value, keys) {
  if (!value || typeof value !== "object") return null;
  const queue = [value];
  const seen = new Set();

  while (queue.length > 0) {
    const current = queue.shift();
    if (!current || typeof current !== "object" || seen.has(current)) continue;
    seen.add(current);

    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(current, key) && current[key] != null) {
        return current[key];
      }
    }

    for (const next of Object.values(current)) {
      if (next && typeof next === "object") {
        if (Array.isArray(next)) queue.push(...next);
        else queue.push(next);
      }
    }
  }

  return null;
}

function formatAmount(amountAtomic, decimals, symbol) {
  if (!amountAtomic) return null;
  if (!Number.isInteger(decimals)) return String(amountAtomic);

  try {
    const amount = BigInt(amountAtomic);
    const base = 10n ** BigInt(decimals);
    const whole = amount / base;
    const fraction = amount % base;
    const fractionText = fraction
      .toString()
      .padStart(decimals, "0")
      .replace(/0+$/, "");
    const display = fractionText ? `${whole}.${fractionText}` : whole.toString();
    return symbol ? `${display} ${symbol}` : display;
  } catch (_error) {
    return String(amountAtomic);
  }
}

function normalizeAddress(value) {
  const text = stringOrNull(value);
  if (!text || !/^0x[a-fA-F0-9]{40}$/.test(text)) return text;
  return text.toLowerCase();
}

function numberOrNull(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return null;
}

function stringOrNull(value) {
  if (value == null) return null;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "bigint" || typeof value === "boolean") {
    return String(value);
  }
  return null;
}

function pass(name, message) {
  return { name, status: "pass", message };
}

function warning(name, message) {
  return { name, status: "warning", message };
}

function fail(name, message) {
  return { name, status: "fail", message };
}
