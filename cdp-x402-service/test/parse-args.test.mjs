import assert from "node:assert/strict";
import { test } from "node:test";

import { parseArgs } from "../src/parse-args.mjs";

// Every purchase this service makes arrives as a command line, so the parser
// is the narrowest place where a caller's intent can be silently changed into
// a different one. These cover both accepted spellings and the values that
// must survive intact — an address or an atomic amount that came through
// wrong would be a payment to the wrong place or for the wrong sum.

const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const GRAPH = "https://gateway.thegraph.com/api/x402/subgraphs/id/5zvR82Qo";

test("--key value, the spelling the gateway already uses", () => {
  const options = parseArgs([
    "--url", GRAPH,
    "--max-atomic", "10000",
    "--expected-asset", USDC,
  ]);

  assert.equal(options.url, GRAPH);
  assert.equal(options["max-atomic"], "10000");
  assert.equal(options["expected-asset"], USDC);
});

test("--key=value, which used to report the option as missing", () => {
  const options = parseArgs([
    `--url=${GRAPH}`,
    "--max-atomic=10000",
    `--expected-asset=${USDC}`,
  ]);

  assert.equal(options.url, GRAPH);
  assert.equal(options["max-atomic"], "10000");
  assert.equal(options["expected-asset"], USDC);
});

test("the two spellings mix in one command line", () => {
  const options = parseArgs(["--url", GRAPH, "--max-atomic=10000"]);

  assert.equal(options.url, GRAPH);
  assert.equal(options["max-atomic"], "10000");
});

test("a value containing = survives whole", () => {
  // JSON bodies and base64 both carry `=`. Splitting on the last one, or on
  // every one, would corrupt the request being paid for.
  const body = '{"query":"{ _meta { block { number } } }","vars":{"a":"b=c"}}';
  const options = parseArgs([`--body-json=${body}`]);

  assert.equal(options["body-json"], body);
});

test("a flag with no value is still true, in both spellings", () => {
  assert.equal(parseArgs(["--live"]).live, "true");
  assert.equal(parseArgs(["--live", "--url", GRAPH]).live, "true");
});

test("an empty value is kept as empty, not turned into true", () => {
  // `--expected-receiver=` is a caller passing nothing, and requiredOption
  // rejects it. Reading it as the string "true" would hand a bogus address to
  // the payment guard instead of failing.
  assert.equal(parseArgs(["--expected-receiver="])["expected-receiver"], "");
});

test("a malformed --= is ignored rather than made into an empty key", () => {
  assert.deepEqual(parseArgs(["--=x"]), { "=x": "true" });
});

test("non-option arguments are skipped", () => {
  const options = parseArgs(["buy", "--url", GRAPH]);

  assert.equal(options.url, GRAPH);
  assert.equal(options.buy, undefined);
});
