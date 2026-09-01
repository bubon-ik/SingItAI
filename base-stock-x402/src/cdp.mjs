/**
 * Running on a CDP wallet instead of a private key.
 *
 * This service holds a buyer's USDC for the seconds between settlement and the
 * swap, so the wallet that does the holding is the one worth protecting. A CDP
 * account never exports a key: signing happens inside Coinbase's TEE and this
 * process only ever sees a transaction hash. That is strictly better than a
 * hex string in a `.env` next to the code that spends it.
 *
 * The price is an external dependency in the settlement path. If CDP is
 * unreachable mid-order the swap fails and the refund path runs, which is the
 * same ending as any other delivery failure — but the refund needs CDP too, so
 * an outage during that window strands the order rather than resolving it. The
 * journal is what makes that recoverable.
 *
 * `@coinbase/cdp-sdk` is NOT a dependency of this package. The adapter takes an
 * already-constructed account, so the CDP path costs nothing to anyone running
 * on a plain key, and the tests exercise the adaptation without credentials.
 */

import { createPublicClient, http } from "viem";
import { base } from "viem/chains";
import { BASE_RPC_URL } from "./chain.mjs";

export const CDP_NETWORK = "base";

/**
 * Wrap a CDP account in the interface `fulfilOrder` already expects.
 *
 * Reads stay on our own RPC: a receipt is public data and there is no reason
 * to spend an API call, or a dependency, on fetching it.
 */
export function cdpClients(account, { rpcUrl = BASE_RPC_URL, publicClient: injected = null } = {}) {
  if (!account?.address) {
    throw new Error("a CDP account with an address is required");
  }
  const publicClient = injected ?? createPublicClient({ chain: base, transport: http(rpcUrl) });

  return () => ({
    account,
    publicClient,
    walletClient: {
      async sendTransaction({ to, data, value = 0n }) {
        const transaction = await complete({ publicClient, from: account.address, to, data, value });
        const result = await account.sendTransaction({
          network: CDP_NETWORK,
          transaction,
        });
        // Both spellings, because the SDK has used both and the existing
        // cdp-x402-service reads them the same defensive way.
        const hash = result?.transactionHash || result?.hash;
        if (!hash) {
          throw new Error("CDP returned no transaction hash");
        }
        return hash;
      },
    },
  });
}

// estimateGas is exact for the state it saw. A swap moves the pool it is
// swapping against, and an approve landing first changes what the swap costs,
// so the estimate is a floor rather than an answer.
const GAS_HEADROOM_PERCENT = 125n;

/**
 * Fill in what CDP does not.
 *
 * The SDK hands the transaction to viem's `serializeTransaction` as given, so
 * a `{to, data}` object becomes a transaction with zero gas and zero fees —
 * which the node rejects as invalid parameters, from far enough away that the
 * error says nothing about gas. Nothing fills these in on the way; this does.
 *
 * The nonce is read pending, which is correct only because this service sends
 * one transaction at a time and waits for each receipt before the next.
 */
export async function complete({ publicClient, from, to, data, value = 0n }) {
  const [nonce, fees, gas] = await Promise.all([
    publicClient.getTransactionCount({ address: from, blockTag: "pending" }),
    publicClient.estimateFeesPerGas(),
    publicClient.estimateGas({ account: from, to, data, value }),
  ]);

  return {
    to,
    data,
    value,
    nonce,
    gas: (gas * GAS_HEADROOM_PERCENT) / 100n,
    maxFeePerGas: fees.maxFeePerGas,
    maxPriorityFeePerGas: fees.maxPriorityFeePerGas,
  };
}

/**
 * Resolve the named CDP account, refusing to spend from any other one.
 *
 * `getOrCreateAccount` is idempotent by name, so a fresh name is a fresh
 * wallet and re-running is safe. Passing `expectedAddress` turns a typo in the
 * name from "quietly used a different wallet" into a startup failure.
 */
export async function resolveCdpAccount({ client, name, expectedAddress = null }) {
  if (!name) {
    throw new Error("a CDP account name is required");
  }
  const account = await client.evm.getOrCreateAccount({ name });
  if (expectedAddress && account.address.toLowerCase() !== expectedAddress.toLowerCase()) {
    throw new Error(
      `CDP account "${name}" is ${account.address}, not the expected ${expectedAddress}`,
    );
  }
  return account;
}

/**
 * A buyer that signs with CDP rather than with a key on this machine.
 *
 * The buyer only ever signs a message — it never sends a transaction and needs
 * no ETH — so this side is a straight swap of one signer for another.
 */
export function cdpSigner(account) {
  return {
    address: account.address,
    signTypedData: (parameters) => account.signTypedData(parameters),
  };
}
