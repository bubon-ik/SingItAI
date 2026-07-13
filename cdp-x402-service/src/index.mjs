import dotenv from "dotenv";
import express from "express";
import { CdpClient } from "@coinbase/cdp-sdk";
import { facilitator } from "@coinbase/x402";
import { HTTPFacilitatorClient } from "@x402/core/server";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { ExactEvmScheme as ExactEvmServerScheme } from "@x402/evm/exact/server";
import { paymentMiddleware, x402ResourceServer } from "@x402/express";
import {
  decodePaymentResponseHeader,
  wrapFetchWithPaymentFromConfig,
} from "@x402/fetch";
import { createPublicClient, createWalletClient, erc20Abi, http, parseUnits } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { base } from "viem/chains";
import { assertSwapMeetsMinUsdc } from "./swap-floor.mjs";
import { makePaymentRequirementsSelector } from "./payment-guard.mjs";
import { humanTokenAmountToAtomic } from "./user-token-transfer.mjs";

dotenv.config();

const BASE_MAINNET_CAIP2 = "eip155:8453";
const DEFAULT_ACCOUNT_NAME = "sign402-mainnet-buyer";

async function main() {
  const [command, ...args] = process.argv.slice(2);
  const options = parseArgs(args);

  if (command === "account") {
    const account = await getCdpAccount();
    writeJson({ ok: true, address: account.address, accountName: accountName() });
    return;
  }

  if (command === "buy") {
    const url = requiredOption(options, "url");
    const result = await buyPaidResource(url);
    writeJson(result);
    return;
  }

  if (command === "buy-user") {
    const url = requiredOption(options, "url");
    const caps = {
      maxAtomic: options["max-atomic"] || "",
      expectedReceiver: options["expected-receiver"] || "",
      expectedAsset: options["expected-asset"] || "",
    };
    const result = await buyPaidResourceWithPrivateKey(url, caps);
    writeJson(result);
    return;
  }

  if (command === "swap-price") {
    const result = await getSwapPrice(options);
    writeJson(result);
    return;
  }

  if (command === "swap") {
    const result = await swapTokens(options);
    writeJson(result);
    return;
  }

  if (command === "transfer-usdc") {
    const result = await transferUsdc(options);
    writeJson(result);
    return;
  }

  if (command === "transfer-token-user") {
    const result = await transferTokenFromUserWallet(options);
    writeJson(result);
    return;
  }

  if (command === "transfer-native-user") {
    const result = await transferNativeFromUserWallet(options);
    writeJson(result);
    return;
  }

  if (command === "token-info") {
    const result = await readTokenInfo(options);
    writeJson(result);
    return;
  }

  if (command === "token-balance") {
    const result = await readTokenBalance(options);
    writeJson(result);
    return;
  }

  if (command === "serve-seller") {
    await serveSeller();
    return;
  }

  throw new Error(`Unknown command: ${command || "<empty>"}`);
}

async function getCdpAccount() {
  assertEnv("CDP_API_KEY_ID");
  assertEnv("CDP_API_KEY_SECRET");
  assertEnv("CDP_WALLET_SECRET");

  const cdp = new CdpClient();
  const name = accountName();
  const account = await cdp.evm.getOrCreateAccount({ name });
  const expectedAddress = process.env.CDP_EVM_ACCOUNT_ADDRESS;

  if (expectedAddress && account.address.toLowerCase() !== expectedAddress.toLowerCase()) {
    throw new Error(
      `CDP account ${name} resolved to ${account.address}, expected ${expectedAddress}`,
    );
  }

  return account;
}

async function buyPaidResource(url) {
  const cdpAccount = await getCdpAccount();
  return buyPaidResourceWithSigner(url, cdpAccount);
}

async function buyPaidResourceWithPrivateKey(url, caps = {}) {
  const privateKey = requiredEnv("SIGN402_EVM_PRIVATE_KEY");
  const account = privateKeyToAccount(privateKey);
  return buyPaidResourceWithSigner(url, account, caps);
}

async function buyPaidResourceWithSigner(url, signer, caps = {}) {
  const config = {
    schemes: [
      {
        network: BASE_MAINNET_CAIP2,
        client: new ExactEvmScheme(signer),
      },
    ],
  };
  // Enforce the exact terms the user approved before any payment is signed.
  if (caps.maxAtomic || caps.expectedReceiver || caps.expectedAsset) {
    config.paymentRequirementsSelector = makePaymentRequirementsSelector(caps);
  }
  const fetchWithPayment = wrapFetchWithPaymentFromConfig(fetch, config);

  const response = await fetchWithPayment(url, {
    method: "GET",
    headers: { Accept: "application/json" },
  });
  const bodyText = await response.text();
  const paymentResponse = paymentSettleResponse(response);

  return {
    ok: response.ok,
    status: response.status,
    resourceUrl: url,
    payer: signer.address,
    body: parseMaybeJson(bodyText),
    paymentResponse,
    transactionHash:
      paymentResponse?.transaction ||
      paymentResponse?.transactionHash ||
      paymentResponse?.txHash ||
      null,
  };
}

async function getSwapPrice(options) {
  const cdp = new CdpClient();
  const account = await getCdpAccount();
  const fromToken = requiredOption(options, "from-token");
  const toToken = requiredOption(options, "to-token");
  const fromAmount = humanTokenAmountToAtomic(
    requiredOption(options, "amount"),
    Number(options.decimals || "18"),
  );
  const network = networkName(options.chain || "base");
  const price = await cdp.evm.getSwapPrice({
    network,
    fromToken,
    toToken,
    fromAmount,
    taker: account.address,
  });
  return normalizeSwapResult({ ok: true, ...price });
}

async function swapTokens(options) {
  const cdp = new CdpClient();
  const account = await getCdpAccount();
  const fromToken = requiredOption(options, "from-token");
  const toToken = requiredOption(options, "to-token");
  const fromAmount = humanTokenAmountToAtomic(
    requiredOption(options, "amount"),
    Number(options.decimals || "18"),
  );
  const network = networkName(options.chain || "base");
  const slippageBps = Number(options["slippage-bps"] || "100");
  const minUsdc = options["min-usdc"] || "";

  // Enforce the caller's absolute USDC floor before moving funds. account.swap
  // only honors slippageBps relative to its own execution price, so without
  // this check a swap that underfills versus the original quote would still
  // settle and silently short the treasury.
  if (minUsdc) {
    const price = await cdp.evm.getSwapPrice({
      network,
      fromToken,
      toToken,
      fromAmount,
      taker: account.address,
    });
    assertSwapMeetsMinUsdc(price, minUsdc);
  }

  const result = await account.swap({
    network,
    fromToken,
    toToken,
    fromAmount,
    slippageBps,
  });
  const transactionHash = result.transactionHash || result.hash;
  return {
    ok: true,
    transactionHash,
    fromToken,
    toToken,
    fromAmount: fromAmount.toString(),
    minUsdc,
    network,
  };
}

async function transferUsdc(options) {
  const account = await getCdpAccount();
  const to = requiredOption(options, "to");
  const amount = parseUnits(requiredOption(options, "amount"), 6);
  const network = networkName(options.chain || "base");
  const result = await account.transfer({
    to,
    amount,
    token: "usdc",
    network,
  });
  const transactionHash = result.transactionHash || result.hash;
  return {
    ok: true,
    transactionHash,
    to,
    amount: amount.toString(),
    token: "usdc",
    network,
  };
}

function basePublicClient(chainName) {
  const chain = viemChain(chainName || "base");
  const rpcUrl =
    process.env.SIGN402_BASE_RPC_URL ||
    process.env.BASE_RPC_URL ||
    chain.rpcUrls.default.http[0];
  return createPublicClient({ chain, transport: http(rpcUrl) });
}

async function readTokenInfo(options) {
  const token = requiredOption(options, "token");
  const publicClient = basePublicClient(options.chain);
  const [symbol, decimals] = await Promise.all([
    publicClient.readContract({ address: token, abi: erc20Abi, functionName: "symbol" }),
    publicClient.readContract({ address: token, abi: erc20Abi, functionName: "decimals" }),
  ]);
  return { ok: true, token, symbol: String(symbol), decimals: Number(decimals) };
}

async function readTokenBalance(options) {
  const token = requiredOption(options, "token");
  const owner = requiredOption(options, "owner");
  const publicClient = basePublicClient(options.chain);
  const balance = await publicClient.readContract({
    address: token,
    abi: erc20Abi,
    functionName: "balanceOf",
    args: [owner],
  });
  return { ok: true, token, owner, balanceAtomic: balance.toString() };
}

async function transferTokenFromUserWallet(options) {
  const privateKey = requiredEnv("SIGN402_EVM_PRIVATE_KEY");
  const account = privateKeyToAccount(privateKey);
  const to = requiredOption(options, "to");
  const token = requiredOption(options, "token");
  const amount = humanTokenAmountToAtomic(
    requiredOption(options, "amount"),
    Number(options.decimals || "18"),
  );
  const chain = viemChain(options.chain || "base");
  const rpcUrl = process.env.SIGN402_BASE_RPC_URL || process.env.BASE_RPC_URL || chain.rpcUrls.default.http[0];
  const transport = http(rpcUrl);
  const walletClient = createWalletClient({
    account,
    chain,
    transport,
  });
  const publicClient = basePublicClient(options.chain);

  const transactionHash = await walletClient.writeContract({
    address: token,
    abi: erc20Abi,
    functionName: "transfer",
    args: [to, amount],
  });
  const receipt = await publicClient.waitForTransactionReceipt({
    hash: transactionHash,
  });
  if (receipt.status !== "success") {
    throw new Error(`user wallet token transfer failed: ${transactionHash}`);
  }
  return {
    ok: true,
    transactionHash,
    from: account.address,
    to,
    token,
    amount: amount.toString(),
    network: networkName(options.chain || "base"),
  };
}

async function transferNativeFromUserWallet(options) {
  const privateKey = requiredEnv("SIGN402_EVM_PRIVATE_KEY");
  const account = privateKeyToAccount(privateKey);
  const to = requiredOption(options, "to");
  const value = humanTokenAmountToAtomic(requiredOption(options, "amount"), 18);
  const chain = viemChain(options.chain || "base");
  const rpcUrl = process.env.SIGN402_BASE_RPC_URL || process.env.BASE_RPC_URL || chain.rpcUrls.default.http[0];
  const walletClient = createWalletClient({
    account,
    chain,
    transport: http(rpcUrl),
  });
  const publicClient = basePublicClient(options.chain);

  const transactionHash = await walletClient.sendTransaction({ to, value });
  const receipt = await publicClient.waitForTransactionReceipt({
    hash: transactionHash,
  });
  if (receipt.status !== "success") {
    throw new Error(`user wallet native transfer failed: ${transactionHash}`);
  }
  return {
    ok: true,
    transactionHash,
    from: account.address,
    to,
    asset: "native",
    symbol: "ETH",
    amount: value.toString(),
    network: networkName(options.chain || "base"),
  };
}

async function serveSeller() {
  const payTo = requiredEnv("CDP_SELLER_PAY_TO");
  const port = Number(process.env.CDP_X402_SELLER_PORT || "4021");
  const price = process.env.CDP_X402_SELLER_PRICE || "$0.01";
  const route = process.env.CDP_X402_SELLER_ROUTE || "/paid/sign402-report";
  const app = express();

  const facilitatorClient = new HTTPFacilitatorClient(facilitator);
  const server = new x402ResourceServer(facilitatorClient).register(
    BASE_MAINNET_CAIP2,
    new ExactEvmServerScheme(),
  );

  app.use(
    paymentMiddleware(
      {
        [`GET ${route}`]: {
          accepts: [
            {
              scheme: "exact",
              price,
              network: BASE_MAINNET_CAIP2,
              payTo,
            },
          ],
          description: "Sign402 paid report",
          mimeType: "application/json",
        },
      },
      server,
    ),
  );

  app.get(route, (_req, res) => {
    res.json({
      ok: true,
      product: "sign402-report",
      network: BASE_MAINNET_CAIP2,
      paidAt: new Date().toISOString(),
      report: {
        summary: "This JSON was unlocked by an x402 payment on Base Mainnet.",
      },
    });
  });

  app.get("/health", (_req, res) => {
    res.json({
      ok: true,
      service: "sign402-cdp-x402-seller",
      network: BASE_MAINNET_CAIP2,
      route,
      price,
      payTo,
    });
  });

  app.listen(port, "127.0.0.1", () => {
    console.error(`CDP x402 seller listening on http://127.0.0.1:${port}${route}`);
  });
}

function paymentSettleResponse(response) {
  const header = response.headers.get("PAYMENT-RESPONSE");
  if (!header) return null;
  return decodePaymentResponseHeader(header);
}

function parseMaybeJson(text) {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch (_error) {
    return text;
  }
}

function accountName() {
  return process.env.CDP_EVM_ACCOUNT_NAME || DEFAULT_ACCOUNT_NAME;
}

function networkName(chain) {
  if (chain === "base") return "base";
  if (chain === "base-mainnet") return "base";
  throw new Error(`Unsupported CDP chain: ${chain}`);
}

function viemChain(chain) {
  if (chain === "base" || chain === "base-mainnet") return base;
  throw new Error(`Unsupported EVM chain: ${chain}`);
}

function singitAmountToAtomic(amount) {
  // Callers always pass a human SINGIT amount (e.g. "25000" or "25000.5");
  // convert to 18-decimal atomic units. Do not treat all-digit strings as
  // already-atomic — that would silently scale the amount by 1e18.
  return parseUnits(String(amount), 18);
}

function normalizeSwapResult(payload) {
  if (typeof payload === "bigint") return payload.toString();
  if (Array.isArray(payload)) return payload.map((item) => normalizeSwapResult(item));
  if (payload && typeof payload === "object") {
    const result = {};
    for (const [key, value] of Object.entries(payload)) {
      result[key] = normalizeSwapResult(value);
    }
    return result;
  }
  return payload;
}

function parseArgs(args) {
  const options = {};
  for (let index = 0; index < args.length; index += 1) {
    const current = args[index];
    if (!current.startsWith("--")) continue;
    const key = current.slice(2);
    const value = args[index + 1];
    if (!value || value.startsWith("--")) {
      options[key] = "true";
      continue;
    }
    options[key] = value;
    index += 1;
  }
  return options;
}

function requiredOption(options, key) {
  const value = options[key];
  if (!value) throw new Error(`--${key} is required`);
  return value;
}

function assertEnv(key) {
  if (!process.env[key]) throw new Error(`${key} is required`);
}

function requiredEnv(key) {
  const value = process.env[key];
  if (!value) throw new Error(`${key} is required`);
  return value;
}

function writeJson(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
}

main().catch((error) => {
  console.error(error?.stack || error?.message || String(error));
  process.exitCode = 1;
});
