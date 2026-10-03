// Internal gateway helper. Request and key arrive via stdin; stdout never contains
// a key, signature or authorization payload. No automatic paid retry is permitted.
import fs from "node:fs";
import path from "node:path";
import { createPublicClient, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { base } from "viem/chains";
import { payMetered } from "./metered-buyer.mjs";

let submitted = false, stage = "initialization", httpStatus, checkpointPath;
try {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  checkpointPath = input.checkpoint;
  const account = privateKeyToAccount(input.privateKey);
  const chain = createPublicClient({ chain: base, transport: http(process.env.SIGN402_BASE_RPC_URL || "https://mainnet.base.org",
    { retryCount: 0, timeout: 15000 }) });
  const signer = { address: account.address, signTypedData: account.signTypedData,
    readContract: chain.readContract.bind(chain) };
  const result = await payMetered({ signer, chain, body: input.body,
    onProgress: (value, status) => { stage = value; if (status) httpStatus = status; }, beforeSubmit: async checkpoint => {
    // The caller creates a private directory. Exclusive creation prevents replay of a call ID.
    const fd = fs.openSync(input.checkpoint, "wx", 0o600);
    try { fs.writeFileSync(fd, JSON.stringify(checkpoint)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    const directory = fs.openSync(path.dirname(input.checkpoint), "r");
    try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
    submitted = true;
  } });
  console.log(JSON.stringify({ ...result, submitted }));
} catch {
  // Never serialize SDK errors: they can include signed payloads or provider responses.
  // Preserve only our own stage labels and numeric HTTP status, never provider
  // response bodies, signatures or arbitrary exception strings.
  if (submitted && checkpointPath) {
    try {
      const saved = JSON.parse(fs.readFileSync(checkpointPath, "utf8"));
      const temporary = checkpointPath + ".diagnostic.tmp";
      const fd = fs.openSync(temporary, "w", 0o600);
      try { fs.writeFileSync(fd, JSON.stringify({ ...saved, failureStage: stage, httpStatus })); fs.fsyncSync(fd); }
      finally { fs.closeSync(fd); }
      fs.renameSync(temporary, checkpointPath);
      const directory = fs.openSync(path.dirname(checkpointPath), "r");
      try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
    } catch { /* The existing durable account journal still blocks retries. */ }
  }
  console.log(JSON.stringify({ ok: false, submitted, stage, httpStatus,
    error: submitted ? "payment_unresolved" : "payment_not_submitted" }));
  process.exitCode = 1;
}
