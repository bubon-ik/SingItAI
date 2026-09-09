#!/usr/bin/env node
"use strict";
// Sign one SpendingApproval on a Ledger and print the signature to stdout.
//
// `wallet-cli` cannot sign messages — checked across its whole command tree,
// see docs/checks.md L2 — so approval goes through the Device Management Kit.
// It takes the approval the gateway built, asks the device to sign readable
// text (EIP-191), and refuses old EIP-712 input. Payment
// validation lives in the gateway. Successful signing alone does not establish
// which fields the user reviewed on the device.
//
//   echo '<approval-json>' | node approve.cjs --path "44'/60'/0'/0/0"
//
// --address reads the public address without signing (no stdin required).
// stdout is the signature or public address. Everything a human reads goes to
// stderr, so a caller can take stdout verbatim.
//
// CommonJS on purpose: the ESM entry points of these packages resolve to a
// directory and Node refuses them with ERR_UNSUPPORTED_DIR_IMPORT. See
// docs/ledger-dx-notes.md.

const {
  DeviceManagementKitBuilder,
  DeviceStatus,
} = require("@ledgerhq/device-management-kit");
const { nodeHidTransportFactory } = require("@ledgerhq/device-transport-kit-node-hid");
const { SignerEthBuilder } = require("@ledgerhq/device-signer-kit-ethereum");
const { firstValueFrom, filter } = require("rxjs");
const { waitForDeviceAction, purchaseSigningAction, validateApproval } = require("./device-action.cjs");

const argv = process.argv.slice(2);
const flag = (name, fallback) => {
  const hit = argv.find((a) => a === `--${name}` || a.startsWith(`--${name}=`));
  if (!hit) return fallback;
  if (hit.includes("=")) return hit.split("=").slice(1).join("=");
  return argv[argv.indexOf(hit) + 1] ?? fallback;
};

const die = (message) => {
  process.stderr.write(`${message}\n`);
  process.exit(1);
};

const readStdin = async () => {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) die("no approval on stdin");
  try {
    return JSON.parse(raw);
  } catch (e) {
    die(`approval is not JSON: ${e.message}`);
  }
};

(async () => {
  const derivationPath = flag("path", "44'/60'/0'/0/0");
  const timeoutSeconds = Number(flag("timeout-seconds", "180"));
  if (!Number.isInteger(timeoutSeconds) || timeoutSeconds < 1 || timeoutSeconds > 600) {
    die("timeout-seconds must be an integer between 1 and 600");
  }
  setTimeout(() => die("Ledger approval timed out; no signature was returned"), timeoutSeconds * 1000);
  const addressOnly = argv.includes("--address");
  const approval = addressOnly ? null : await readStdin();
  const message = approval?.message || {};
  // Validate the envelope before touching USB.
  if (!addressOnly) {
    try { validateApproval(approval); } catch (e) { die(e.message); }
  }

  const dmk = new DeviceManagementKitBuilder().addTransport(nodeHidTransportFactory).build();
  let sessionId;
  try {
    process.stderr.write("Looking for a Ledger. Unlock it and open the Ethereum app.\n");
    const devices = await firstValueFrom(
      dmk.listenToAvailableDevices({}).pipe(filter((d) => d.length > 0))
    );
    sessionId = await dmk.connect({ device: devices[0] });
    const state = await firstValueFrom(dmk.getDeviceSessionState({ sessionId }));
    if (state.deviceStatus === DeviceStatus.LOCKED) die("the device is locked");

    if (!addressOnly) process.stderr.write(
      `Requested purchase: ${message.purchase}; ${message.amountUsd} USDC on Base ` +
        `to ${message.payTo}\n` +
        "Review these values on the Ledger. If they are missing or only hashes appear, reject.\n"
    );

    const signer = new SignerEthBuilder({ dmk, sessionId, originToken: "singit" }).build();
    // Wait for a *terminal* state, not merely a non-pending one. The first
    // emission is `not-started`, so filtering on "not pending" resolves before
    // the device has been asked anything — the signature then arrives with
    // nobody listening, which looks from the outside like the device hanging.
    const action = addressOnly
      ? signer.getAddress(derivationPath, { checkOnDevice: false })
      : purchaseSigningAction(signer, derivationPath, approval);
    const done = await waitForDeviceAction(action, {
      signing: !addressOnly,
      log: (line) => process.stderr.write(`${line}\n`),
    });
    if (done.status !== "completed") {
      const detail = done.error
        ? `${done.error._tag || done.error.name || ""} ${done.error.message || ""} ${
            done.error.originalError ? JSON.stringify(done.error.originalError) : ""
          }`.trim()
        : JSON.stringify(done);
      die(`the device did not sign (${done.status}): ${detail}`);
    }

    if (addressOnly) {
      process.stdout.write(`${done.output.address}\n`);
    } else {
      const hex = (x) => String(x).replace(/^0x/, "");
      const { r, s, v } = done.output;
      process.stdout.write(`0x${hex(r)}${hex(s)}${Number(v).toString(16).padStart(2, "0")}\n`);
    }
    // Leave deliberately. The HID transport keeps a listener open, so the event
    // loop never empties and the process hangs after a perfectly good
    // signature — which reads, from the caller's side, exactly like a device
    // that never answered.
    await dmk.disconnect({ sessionId }).catch(() => {});
    process.exit(0);
  } catch (e) {
    die(`Ledger action failed: ${e?.message || e?._tag || e?.name || "device unavailable"}`);
  } finally {
    if (sessionId) await dmk.disconnect({ sessionId }).catch(() => {});
  }
})();
