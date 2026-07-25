# Fictional Phone Example Privacy Fix

## Goal

Remove the reported personal phone-number literal from every file in the
current repository tree and replace user-facing examples with a fictional
United States number.

## Scope

- Use `+12025550123` as the E.164 example. It uses the fictional `555-01xx`
  range and must not identify a real Sign402 user.
- Update the iMessage connection prompt.
- Update the operator CLI E.164 validation example.
- Replace matching test-fixture values so the reported personal number no
  longer exists anywhere in the current tree.
- Keep all runtime phone-number validation and pairing behavior unchanged.

## Testing

- Add or strengthen the prompt regression test so it requires the exact
  fictional example and rejects a Czech-country-code example.
- Keep the operator normalization tests passing with the revised validation
  message.
- Run the complete Sign402 wallet-plugin and operator-script test suites.
- Scan the current tree to confirm the reported personal number is absent.

## Deployment

Merge the privacy patch into `x402Bnkr`, push it, fast-forward the production
checkout, and restart `hermes-gateway` so the updated prompt is loaded. The
Sign402 gateway does not need a restart because its runtime code is unchanged.

## Non-goals

- Rewriting existing Git history or force-pushing shared branches.
- Changing phone validation, Photon registration, pairing, or approval flows.
- Changing configured production phone numbers or secrets.
