import assert from "node:assert/strict";
import test from "node:test";
import { privateKeyToAccount } from "viem/accounts";

import { config, createServer, priceOrder } from "../src/server.mjs";

const KEY = "0x" + "11".repeat(32);
const ACCOUNT = privateKeyToAccount(KEY);

async function withServer(settings, run) {
  const server = createServer(settings).listen(0);
  await new Promise((resolve) => server.once("listening", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    return await run(base);
  } finally {
    server.close();
  }
}

test("a payout address that is not the facilitator refuses to start", () => {
  // Settlement pays payTo; the swap spends from the facilitator. Two different
  // addresses means every order takes money it cannot then spend.
  assert.throws(
    () => config({ BASE_FACILITATOR_KEY: KEY, BASE_PAY_TO: "0x" + "99".repeat(20) }),
    /must be the facilitator address/,
  );
});

test("a matching payout address is accepted", () => {
  const settings = config({ BASE_FACILITATOR_KEY: KEY, BASE_PAY_TO: ACCOUNT.address });
  assert.equal(settings.payTo.toLowerCase(), ACCOUNT.address.toLowerCase());
  assert.equal(settings.hasKey, true);
});

test("the fee is added to the order rather than taken out of it", () => {
  const { amountInAtomic, feeAtomic, totalAtomic } = priceOrder(100, { feeBps: 100 });

  // The buyer gets $100 of shares and pays $101 — not $99 of shares for $100.
  assert.equal(amountInAtomic, 100_000_000n);
  assert.equal(feeAtomic, 1_000_000n);
  assert.equal(totalAtomic, 101_000_000n);
});

test("an unpaid request answers 402 with a payable challenge", async () => {
  const settings = config({ BASE_FACILITATOR_KEY: KEY, BASE_PAY_TO: ACCOUNT.address });

  await withServer(settings, async (base) => {
    const response = await fetch(`${base}/paid/buy/NVDA?usd=100`, { method: "POST" });
    assert.equal(response.status, 402);

    const body = await response.json();
    const [accept] = body.accepts;
    assert.equal(accept.network, "eip155:8453");
    assert.equal(accept.maxAmountRequired, "101000000");
    assert.equal(accept.payTo.toLowerCase(), ACCOUNT.address.toLowerCase());
    // The domain travels with the challenge so a payer never has to guess it.
    assert.equal(accept.extra.name, "USD Coin");
    assert.equal(accept.extra.version, "2");
  });
});

test("a ticker this node does not sell says which it does", async () => {
  const settings = config({ BASE_FACILITATOR_KEY: KEY, BASE_PAY_TO: ACCOUNT.address });

  await withServer(settings, async (base) => {
    const response = await fetch(`${base}/paid/buy/TSLA?usd=100`, { method: "POST" });
    assert.equal(response.status, 404);
    assert.match((await response.json()).error, /NVDA, AAPL, GOOGL, META/);
  });
});

test("an order outside the size limits is refused before any challenge", async () => {
  const settings = config({ BASE_FACILITATOR_KEY: KEY, BASE_PAY_TO: ACCOUNT.address, BASE_MAX_USD: "250" });

  await withServer(settings, async (base) => {
    for (const usd of ["0", "-5", "5000", "abc", ""]) {
      const response = await fetch(`${base}/paid/buy/NVDA?usd=${usd}`, { method: "POST" });
      assert.equal(response.status, 400, `usd=${usd}`);
    }
  });
});

test("health says plainly whether the node can actually be paid", async () => {
  await withServer(config({}), async (base) => {
    const body = await (await fetch(`${base}/health`)).json();
    assert.equal(body.payable, false, "no key, no payments — and it says so");
    assert.deepEqual(body.markets, ["NVDA", "AAPL", "GOOGL", "META"]);
  });
});

test("the catalog advertises one resource per market", async () => {
  const settings = config({ BASE_FACILITATOR_KEY: KEY, BASE_PAY_TO: ACCOUNT.address });

  await withServer(settings, async (base) => {
    const body = await (await fetch(`${base}/.well-known/x402`)).json();
    assert.equal(body.resources.length, 4);
    assert.match(body.resources[0].resource, /\/paid\/buy\/NVDA$/);
  });
});


// -- the challenge, in both forms clients read ---------------------------

test("the 402 carries the terms in the body and the header, and they agree", async () => {
  const settings = config({ BASE_FACILITATOR_KEY: KEY, BASE_PAY_TO: ACCOUNT.address });

  await withServer(settings, async (base) => {
    const response = await fetch(`${base}/paid/buy/NVDA?usd=100`, { method: "POST" });
    const body = await response.json();

    const header = response.headers.get("payment-required");
    assert.ok(header, "a client reading only the header must still be able to pay");
    const decoded = JSON.parse(Buffer.from(header, "base64").toString("utf8"));

    // Massive publishes only the header; our Robinhood node only the body. A
    // buyer that reads the other one sees no payable leg, so we send both.
    assert.deepEqual(decoded.accepts, body.accepts);
    assert.equal(decoded.x402Version, 2);
  });
});

test("the challenge tells an agent how to call it without documentation", async () => {
  const settings = config({ BASE_FACILITATOR_KEY: KEY, BASE_PAY_TO: ACCOUNT.address, BASE_MAX_USD: "250" });

  await withServer(settings, async (base) => {
    const body = await (await fetch(`${base}/paid/buy/NVDA?usd=100`, { method: "POST" })).json();
    const bazaar = body.extensions.bazaar;

    assert.equal(bazaar.routeTemplate, "/paid/buy/:ticker");
    const usd = bazaar.schema.properties.input.properties.queryParams.properties.usd;
    assert.equal(usd.maximum, 250, "the size limit is discoverable, not a surprise 400");
    assert.deepEqual(
      bazaar.schema.properties.input.properties.pathParams.properties.ticker.enum,
      ["NVDA", "AAPL", "GOOGL", "META"],
    );
    assert.equal(bazaar.info.output.example.ok, true);
  });
});

test("health reports how much money is owed, without naming who is owed it", async () => {
  const settings = config({ BASE_FACILITATOR_KEY: KEY, BASE_PAY_TO: ACCOUNT.address });
  const { Journal } = await import("../src/journal.mjs");
  const { mkdtempSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");

  const log = new Journal({ path: join(mkdtempSync(join(tmpdir(), "health-")), "orders.jsonl") });
  log.append({ orderId: "a", step: "settled", payer: "0xdeadbeef" });

  const server = createServer(settings, log).listen(0);
  await new Promise((resolve) => server.once("listening", resolve));
  try {
    const body = await (await fetch(`http://127.0.0.1:${server.address().port}/health`)).json();
    assert.equal(body.unresolvedOrders, 1);
    // The route is public; the payer address must not be on it.
    assert.equal(JSON.stringify(body).includes("0xdeadbeef"), false);
  } finally {
    server.close();
  }
});


// -- a bad detail string must not take the server down -------------------

test("a multi-line provider error is flattened, not put in a header raw", async () => {
  // What killed the process: viem errors span several lines, Node rejects a
  // header containing one, and the throw happened inside the response.
  const { headerSafe } = await import("../src/server.mjs");

  assert.equal(headerSafe("Execution reverted\n\nDetails: 0xabc\tmore"), "Execution reverted Details: 0xabc more");
  assert.equal(headerSafe("цена — мала"), "");
  assert.equal(headerSafe(null), "");
  assert.ok(headerSafe("x".repeat(500)).length <= 200);
  assert.match(headerSafe("ok\r\nInjected: yes"), /^ok Injected: yes$/, "no CRLF survives");
});

test("the refusal detail is readable in the body, not only in a header", async () => {
  const settings = config({ BASE_FACILITATOR_KEY: KEY, BASE_PAY_TO: ACCOUNT.address });
  const { nullJournal } = await import("../src/journal.mjs");

  const server = createServer(settings, nullJournal, {
    async verify() {
      return { isValid: false, invalidReason: "the signature does not match\nline two", payer: null };
    },
  }).listen(0);
  await new Promise((resolve) => server.once("listening", resolve));

  try {
    const response = await fetch(
      `http://127.0.0.1:${server.address().port}/paid/buy/NVDA?usd=1`,
      { method: "POST", headers: { "X-PAYMENT": Buffer.from("{}").toString("base64") } },
    );
    assert.equal(response.status, 402);
    const body = await response.json();
    assert.match(body.detail, /signature does not match/);
    assert.equal(response.headers.get("x402-detail").includes("\n"), false);
  } finally {
    server.close();
  }
});
