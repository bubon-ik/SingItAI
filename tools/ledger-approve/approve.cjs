#!/usr/bin/env node
"use strict";
// Sign one SpendingApproval on a Ledger and print the signature to stdout.
//
// `wallet-cli` cannot sign messages — checked across its whole command tree,
// see docs/checks.md L2 — so approval goes through the Device Management Kit.
// This file is deliberately small: it takes the typed data the gateway built,
// asks the device, prints what came back. It decides nothing and validates
// nothing; the gateway does both, because it is the side that knows what the
// payment is.
//
//   echo '<typed-data-json>' | node approve.cjs --path "44'/60'/0'/0/0"
//
// stdout is the signature and nothing else. Everything a human reads goes to
// stderr, so a caller can take stdout verbatim.
//
// CommonJS on purpose: the ESM entry points of these packages resolve to a
// directory and Node refuses them with ERR_UNSUPPORTED_DIR_IMPORT. See
// docs/ledger-dx-notes.md.

const {
  DeviceManagementKitBuilder,
  DeviceStatus,
  DeviceActionStatus,
} = require("@ledgerhq/device-management-kit");
const { nodeHidTransportFactory } = require("@ledgerhq/device-transport-kit-node-hid");
const { SignerEthBuilder } = require("@ledgerhq/device-signer-kit-ethereum");
const { firstValueFrom, filter, tap } = require("rxjs");

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
  if (!raw) die("no typed data on stdin");
  try {
    return JSON.parse(raw);
  } catch (e) {
    die(`typed data is not JSON: ${e.message}`);
  }
};

(async () => {
  const derivationPath = flag("path", "44'/60'/0'/0/0");
  const typedData = await readStdin();
  const message = typedData.message || {};

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

    process.stderr.write(
      `Confirm on the device: ${message.amountUsd} USD to ${message.merchant} ` +
        `at ${message.payTo}\n`
    );

    const signer = new SignerEthBuilder({ dmk, sessionId, originToken: "sign402" }).build();
    // Wait for a *terminal* state, not merely a non-pending one. The first
    // emission is `not-started`, so filtering on "not pending" resolves before
    // the device has been asked anything — the signature then arrives with
    // nobody listening, which looks from the outside like the device hanging.
    const TERMINAL = new Set([
      DeviceActionStatus.Completed,
      DeviceActionStatus.Error,
      DeviceActionStatus.Stopped,
    ]);
    const { observable } = signer.signTypedData(derivationPath, typedData);
    const done = await firstValueFrom(
      observable.pipe(
        tap((s) => process.stderr.write(`  … ${s.status}\n`)),
        filter((s) => TERMINAL.has(s.status))
      )
    );
    if (done.status !== "completed") {
      const detail = done.error
        ? `${done.error._tag || done.error.name || ""} ${done.error.message || ""} ${
            done.error.originalError ? JSON.stringify(done.error.originalError) : ""
          }`.trim()
        : JSON.stringify(done);
      die(`the device did not sign (${done.status}): ${detail}`);
    }

    const hex = (x) => String(x).replace(/^0x/, "");
    const { r, s, v } = done.output;
    process.stdout.write(`0x${hex(r)}${hex(s)}${Number(v).toString(16).padStart(2, "0")}\n`);
    // Leave deliberately. The HID transport keeps a listener open, so the event
    // loop never empties and the process hangs after a perfectly good
    // signature — which reads, from the caller's side, exactly like a device
    // that never answered.
    await dmk.disconnect({ sessionId }).catch(() => {});
    process.exit(0);
  } catch (e) {
    die(`signing failed: ${e && e.message ? e.message : e}`);
  } finally {
    if (sessionId) await dmk.disconnect({ sessionId }).catch(() => {});
  }
})();
