import {
  encodeFunctionData,
  erc20Abi,
  isAddress,
} from "viem";


const IDEMPOTENCY_KEY_PATTERN = /^[A-Za-z0-9:_-]{1,128}$/;
const HEX_SUFFIX_PATTERN = /^0x(?:[0-9a-fA-F]{2})+$/;


// ERC-8021 attribution: the suffix is appended after the ABI-encoded calldata.
// The ERC-20 transfer decodes identically because the extra bytes fall outside
// the encoded arguments; offchain indexers read them to attribute the transfer.
// An invalid suffix is dropped rather than sent, because attribution must never
// change the bytes that move money.
function appendDataSuffix(data, dataSuffix) {
  if (!dataSuffix) {
    return data;
  }
  if (typeof dataSuffix !== "string" || !HEX_SUFFIX_PATTERN.test(dataSuffix)) {
    return data;
  }
  return `${data}${dataSuffix.slice(2)}`;
}


export async function returnErc20({
  account,
  publicClient,
  token,
  to,
  amountAtomic,
  network,
  idempotencyKey,
  dataSuffix,
}) {
  if (!isAddress(token)) {
    throw new Error("token return token must be an EVM address");
  }
  if (!isAddress(to)) {
    throw new Error("token return destination must be an EVM address");
  }
  const atomicText = String(amountAtomic || "").trim();
  if (!/^[0-9]+$/.test(atomicText) || BigInt(atomicText) <= 0n) {
    throw new Error("token return amount must be a positive atomic integer");
  }
  const networkName = String(network || "").trim();
  if (networkName !== "base") {
    throw new Error("token return network must be base");
  }
  const key = String(idempotencyKey || "").trim();
  if (!IDEMPOTENCY_KEY_PATTERN.test(key)) {
    throw new Error("token return idempotency key is invalid");
  }

  const amount = BigInt(atomicText);
  const result = await account.sendTransaction({
    network: networkName,
    transaction: {
      to: token,
      data: appendDataSuffix(
        encodeFunctionData({
          abi: erc20Abi,
          functionName: "transfer",
          args: [to, amount],
        }),
        dataSuffix,
      ),
    },
    idempotencyKey: key,
  });
  const transactionHash = result?.transactionHash || result?.hash;
  if (!transactionHash) {
    throw new Error("CDP token return did not provide a transaction hash");
  }
  const receipt = await publicClient.waitForTransactionReceipt({
    hash: transactionHash,
  });
  if (receipt?.status !== "success") {
    throw new Error("CDP token return transaction reverted");
  }

  return {
    ok: true,
    transactionHash,
    network: networkName,
    token,
    amountAtomic: atomicText,
    from: String(account.address || ""),
    to,
  };
}
