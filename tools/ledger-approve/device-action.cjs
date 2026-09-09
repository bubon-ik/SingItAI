"use strict";

const { firstValueFrom, filter, tap } = require("rxjs");

const TERMINAL = new Set(["completed", "error", "stopped"]);
const LEGACY_TYPED_DATA = "signer.eth.steps.signTypedDataLegacy";

function validateApproval(approval) {
  if (
    approval?.signingMethod !== "personal_sign" ||
    approval?.domain?.name !== "SingIt Spending Approval" || approval?.domain?.version !== "2" ||
    approval?.domain?.chainId !== 8453 ||
    typeof approval.displayText !== "string" ||
    !approval.displayText.startsWith("SingIt purchase v2\nBuy: ") ||
    approval.displayText.length > 2000 || /[^\x20-\x7e\n]/.test(approval.displayText)
  ) throw new Error("Use a readable v2 personal_sign approval from the gateway; typed-data signing is disabled.");
}

function purchaseSigningAction(signer, path, approval) {
  validateApproval(approval);
  // Pass the text itself. Hashing it here would discard readable device review.
  return signer.signMessage(path, approval.displayText);
}

// A legacy EIP-712 action sends only hashes to the device. Never forward its
// signature to the payer. The non-legacy path can still lack display metadata:
// absence of this fallback is NOT proof of Clear Signing or readable fields.
async function waitForDeviceAction(action, { signing = true, log = () => {} } = {}) {
  try {
    return await firstValueFrom(
      action.observable.pipe(
        tap((state) => {
          const step = state.intermediateValue?.step;
          const interaction = state.intermediateValue?.requiredUserInteraction;
          log(`  … ${state.status}${step ? ` [${step}]` : ""}${interaction ? ` (${interaction})` : ""}`);
          if (signing && step === LEGACY_TYPED_DATA) {
            throw new Error(
              "Ledger SDK selected hash-only EIP-712 signing. Approval stopped; " +
              "no signature will be submitted. Reject any remaining prompt on the device."
            );
          }
        }),
        filter((state) => TERMINAL.has(state.status))
      )
    );
  } catch (error) {
    try { await action.cancel?.(); } catch { /* Preserve the original refusal. */ }
    throw error;
  }
}

module.exports = { waitForDeviceAction, purchaseSigningAction, validateApproval };
