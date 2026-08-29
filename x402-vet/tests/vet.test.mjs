import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const DIRECTORY = "https://x402-list.com/api/v1/services";

function service(over) {
  return {
    slug: "svc",
    name: "Svc",
    base_url: "https://svc.example/paid",
    category: "AI",
    status: "online",
    min_price_usd: 0.01,
    networks: ["base"],
    assessment: {
      reliability_uptime_30d: 100,
      compliance_grade: "A",
      risk_level: "clean",
      traction: { volume_usd_30d: 100, unique_buyers_30d: 40, top_buyer_share_30d: 0.16 },
    },
    ...over,
  };
}

const FIXTURE = [
  service({ slug: "healthy", name: "Healthy" }),
  service({
    slug: "one-buyer",
    name: "One Buyer",
    assessment: {
      reliability_uptime_30d: 100,
      risk_level: "clean",
      traction: { volume_usd_30d: 160000, unique_buyers_30d: 3, top_buyer_share_30d: 0.99 },
    },
  }),
  service({ slug: "offline", name: "Offline", status: "offline" }),
  // Three services reporting the same buyer/volume pair to the cent.
  ...["twin-a", "twin-b", "twin-c"].map((slug) =>
    service({
      slug,
      name: slug,
      assessment: {
        reliability_uptime_30d: 99,
        risk_level: "clean",
        traction: { volume_usd_30d: 500, unique_buyers_30d: 7, top_buyer_share_30d: 0.2 },
      },
    }),
  ),
];

/**
 * Each test gets its own module instance, because the snapshot cache lives at
 * module scope and would otherwise leak one test's directory into the next.
 */
let instance = 0;
async function load(name, fetchImpl) {
  globalThis.fetch = fetchImpl;
  return (await import(`../x402/${name}/index.mjs?i=${instance++}`)).evaluate;
}

async function loadDefault(name, fetchImpl) {
  globalThis.fetch = fetchImpl;
  return (await import(`../x402/${name}/index.mjs?i=${instance++}`)).default;
}

const PAID_PATH = "/chat";
const CATALOG_PAY_TO = "0xaaaa";

/**
 * The list route publishes only `base_url`; the paid path and the recorded
 * receiver live on the detail route. The stub mirrors that split, because
 * getting it wrong is what made the first version probe host roots.
 */
function detailFor(svc) {
  return {
    ...svc,
    endpoints: [
      {
        path: PAID_PATH,
        is_active: true,
        pricing: [
          { network_caip2: "eip155:8453", pay_to: CATALOG_PAY_TO, asset_address: "0x8335" },
        ],
      },
    ],
  };
}

function directoryReturning(services, probeImpl) {
  return async (url, init) => {
    const href = String(url);
    if (href.startsWith(`${DIRECTORY}?`) || href === DIRECTORY) {
      const page = Number(new URL(href).searchParams.get("page") ?? 1);
      return Response.json({ data: page === 1 ? services : [], meta: { total_pages: 1 } });
    }
    if (href.startsWith(`${DIRECTORY}/`)) {
      const slug = href.slice(DIRECTORY.length + 1);
      const hit = services.find((s) => s.slug === slug);
      return hit
        ? Response.json({ data: detailFor(hit) })
        : new Response("no", { status: 404 });
    }
    if (probeImpl) return probeImpl(href, init);
    return new Response(JSON.stringify({ accepts: [{ payTo: "0xAAAA", network: "eip155:8453", amount: "1000" }] }), {
      status: 402,
      headers: { "content-type": "application/json" },
    });
  };
}

function post(body) {
  return new Request("https://x402.bankr.bot/w/vet-shortlist", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function get(query) {
  const url = new URL("https://x402.bankr.bot/w/vet-shortlist");
  for (const [k, v] of Object.entries(query)) url.searchParams.set(k, String(v));
  return new Request(url, { method: "GET" });
}

/** Stand-in for the persistent store x402 Cloud hands the handler via ctx. */
function fileStore() {
  const disk = new Map();
  return {
    disk,
    ctx: {
      files: {
        async readJson(path) {
          if (!disk.has(path)) throw new Error("ENOENT");
          return JSON.parse(disk.get(path));
        },
        async writeJson(path, value) {
          disk.set(path, JSON.stringify(value));
        },
      },
    },
  };
}

test("shortlist applies the documented filters and ranks by net volume", async () => {
  const handler = await load("vet-shortlist", directoryReturning(FIXTURE));
  const body = await (await handler(post({ category: "AI" }))).json();

  assert.equal(body.ok, true);
  assert.equal(body.checked, FIXTURE.length);
  const slugs = body.results.map((r) => r.slug);
  assert.deepEqual(slugs, ["healthy"]);
  assert.equal(body.results[0].netVolume30d, 84);
});

test("a service whose buyer/volume pair matches two others is flagged, without a motive", async () => {
  const handler = await load("vet-shortlist", directoryReturning(FIXTURE));
  const body = await (await handler(post({ includeThin: true }))).json();

  // Flagged services are `check` and never reach the shortlist.
  assert.ok(!body.results.some((r) => r.slug.startsWith("twin-")));

  const every = JSON.stringify(body).toLowerCase();
  for (const accusation of ["farm", "fraud", "fake", "sybil", "wash", "scam"]) {
    assert.ok(!every.includes(accusation), `verdict wording must not say "${accusation}"`);
  }
});

test("one dominant buyer reads as thin, and says so as an observation", async () => {
  const handler = await load("vet-shortlist", directoryReturning(FIXTURE));
  const body = await (await handler(post({ includeThin: true, minBuyers: 1, minNetVolume30d: 0 }))).json();

  const hit = body.results.find((r) => r.slug === "one-buyer");
  assert.equal(hit.verdict, "thin");
  assert.ok(hit.why.includes("one buyer accounts for 99% of volume"));
});

test("an empty match is a real answer: 200, populated `checked`", async () => {
  const handler = await load("vet-shortlist", directoryReturning(FIXTURE));
  const res = await handler(post({ category: "nothing-matches-this" }));
  const body = await res.json();

  assert.equal(res.status, 200);
  assert.equal(body.ok, true);
  assert.equal(body.returned, 0);
  assert.equal(body.checked, FIXTURE.length);
});

test("an unavailable directory is our failure: 503, nothing charged", async () => {
  const handler = await load("vet-shortlist", async (url) => {
    if (String(url).startsWith(DIRECTORY)) throw new Error("directory down");
    return Response.json({});
  });
  const res = await handler(post({}));

  assert.equal(res.status, 503);
  assert.equal((await res.json()).error, "directory_unavailable");
});

test("a probe that times out does not fail the request", async () => {
  const handler = await load(
    "vet-shortlist",
    directoryReturning(FIXTURE, async () => {
      const error = new Error("timed out");
      error.name = "TimeoutError";
      throw error;
    }),
  );
  const res = await handler(post({ category: "AI", probe: true }));
  const body = await res.json();

  assert.equal(res.status, 200);
  assert.equal(body.probed, 1);
  assert.equal(body.results[0].liveProbe, "timeout");
});

test("a service not answering the unpaid handshake is downgraded to check", async () => {
  const handler = await load(
    "vet-shortlist",
    directoryReturning(FIXTURE, async () => new Response("hello", { status: 200 })),
  );
  const body = await (await handler(post({ category: "AI", probe: true }))).json();

  assert.equal(body.results[0].verdict, "check");
  assert.ok(body.results[0].why.some((w) => w.includes("instead of 402")));
});

test("attribution ships in every response, because the licence requires it", async () => {
  const ok = await load("vet-shortlist", directoryReturning(FIXTURE));
  const down = await load("vet-shortlist", async (url) => {
    if (String(url).startsWith(DIRECTORY)) throw new Error("down");
    return Response.json({});
  });

  for (const res of [await ok(post({})), await down(post({}))]) {
    const body = await res.json();
    assert.match(body.attribution, /x402-list\.com \(CC BY 4\.0\)/);
  }
});

test("vet-service returns one record and always probes it", async () => {
  const handler = await load("vet-service", directoryReturning(FIXTURE));
  const body = await (await handler(post({ slug: "healthy" }))).json();

  assert.equal(body.ok, true);
  assert.equal(body.result.slug, "healthy");
  assert.equal(body.result.liveProbe, "alive");
  assert.equal(body.result.probedUrl, `https://svc.example/paid${PAID_PATH}`);
  assert.equal(body.result.livePayTo, CATALOG_PAY_TO);
  assert.equal(body.result.payToChanged, false);
});

test("shortlist does not probe unless asked", async () => {
  const handler = await load(
    "vet-shortlist",
    directoryReturning(FIXTURE, () => {
      throw new Error("must not probe by default");
    }),
  );
  const body = await (await handler(post({ category: "AI" }))).json();

  assert.equal(body.probed, 0);
  assert.equal(body.results[0].liveProbe, undefined);
});

test("a live receiver differing from the recorded one is reported as a mismatch", async () => {
  const handler = await load(
    "vet-service",
    directoryReturning(FIXTURE, async () =>
      Response.json({ accepts: [{ payTo: "0xBBBB", network: "eip155:8453", amount: "1000" }] }, { status: 402 }),
    ),
  );
  const body = await (await handler(post({ slug: "healthy" }))).json();

  assert.equal(body.result.payToChanged, true);
  assert.equal(body.result.verdict, "check");
  assert.ok(body.result.why.some((w) => w.includes("differs from the one the directory lists")));
});

test("a service with no published paid path is `unknown`, never a failure", async () => {
  const handler = await load("vet-service", async (url) => {
    const href = String(url);
    if (href.startsWith(`${DIRECTORY}?`)) {
      return Response.json({ data: FIXTURE, meta: { total_pages: 1 } });
    }
    if (href.startsWith(`${DIRECTORY}/`)) {
      return Response.json({ data: { base_url: "https://svc.example", endpoints: [] } });
    }
    throw new Error("must not probe without a path");
  });
  const body = await (await handler(post({ slug: "healthy" }))).json();

  assert.equal(body.result.liveProbe, "unknown");
  assert.equal(body.result.verdict, "ok");
});

test("vet-service refuses an empty query and 404s an unknown one", async () => {
  const handler = await load("vet-service", directoryReturning(FIXTURE));

  assert.equal((await handler(post({}))).status, 400);
  assert.equal((await handler(post({ slug: "never-heard-of-it" }))).status, 404);
});

test("a templated paid path is not probed and not held against the service", async () => {
  const handler = await load("vet-service", async (url) => {
    const href = String(url);
    if (href.startsWith(`${DIRECTORY}?`)) {
      return Response.json({ data: FIXTURE, meta: { total_pages: 1 } });
    }
    if (href.startsWith(`${DIRECTORY}/`)) {
      return Response.json({
        data: {
          base_url: "https://svc.example",
          endpoints: [{ path: "/v2/actors/:actorId/run", is_active: true }],
        },
      });
    }
    throw new Error("must not probe a template URL");
  });
  const body = await (await handler(post({ slug: "healthy" }))).json();

  assert.equal(body.result.liveProbe, "unknown");
  assert.equal(body.result.verdict, "ok");
  assert.match(body.result.liveProbeReason, /template/);
});

test("a POST-only route is probed with POST, and 405 is our fault not theirs", async () => {
  const seen = [];
  const handler = await load("vet-service", async (url, init) => {
    const href = String(url);
    if (href.startsWith(`${DIRECTORY}?`)) {
      return Response.json({ data: FIXTURE, meta: { total_pages: 1 } });
    }
    if (href.startsWith(`${DIRECTORY}/`)) {
      return Response.json({
        data: {
          base_url: "https://svc.example",
          endpoints: [{ path: "/chat", method: "POST", is_active: true }],
        },
      });
    }
    seen.push(init.method);
    return new Response("method not allowed", { status: 405 });
  });
  const body = await (await handler(post({ slug: "healthy" }))).json();

  assert.deepEqual(seen, ["POST"]);
  assert.equal(body.result.liveProbe, "unknown");
  assert.equal(body.result.verdict, "ok");
});

test("a base_url with its own path prefix is preserved when aiming the probe", async () => {
  let probed = null;
  const handler = await load("vet-service", async (url) => {
    const href = String(url);
    if (href.startsWith(`${DIRECTORY}?`)) {
      return Response.json({ data: FIXTURE, meta: { total_pages: 1 } });
    }
    if (href.startsWith(`${DIRECTORY}/`)) {
      return Response.json({
        data: {
          base_url: "https://api.venice.ai/api/v1",
          endpoints: [{ path: "/chat/completions", method: "POST", is_active: true }],
        },
      });
    }
    probed = href;
    return Response.json({ accepts: [] }, { status: 402 });
  });
  await handler(post({ slug: "healthy" }));

  assert.equal(probed, "https://api.venice.ai/api/v1/chat/completions");
});


test("GET carries filters as query params, so the endpoint can be probed without code", async () => {
  const handler = await load("vet-shortlist", directoryReturning(FIXTURE));
  const body = await (await handler(get({ category: "AI", includeThin: true, minBuyers: 1 }))).json();

  assert.equal(body.ok, true);
  assert.equal(body.filter.category, "ai");
  assert.equal(body.filter.includeThin, true);
});

test("the snapshot is cached in ctx.files, because module state does not survive", async () => {
  let directoryPulls = 0;
  const store = fileStore();
  const fetchImpl = async (url, init) => {
    if (String(url).startsWith(`${DIRECTORY}?`)) directoryPulls += 1;
    return directoryReturning(FIXTURE)(url, init);
  };

  const first = await load("vet-shortlist", fetchImpl);
  await first(post({}), store.ctx);
  assert.equal(directoryPulls, 1);
  assert.ok(store.disk.has("/x402/vet-shortlist/snapshot.json"));

  // A second request is a cold module: only the file carries the snapshot over.
  const second = await load("vet-shortlist", fetchImpl);
  const body = await (await second(post({}), store.ctx)).json();

  assert.equal(directoryPulls, 1);
  assert.equal(body.checked, FIXTURE.length);
});

test("a cached snapshot keeps the endpoint answering when the directory is down", async () => {
  const store = fileStore();
  const warm = await load("vet-shortlist", directoryReturning(FIXTURE));
  await warm(post({}), store.ctx);

  const cold = await load("vet-shortlist", async (url) => {
    if (String(url).startsWith(DIRECTORY)) throw new Error("directory down");
    return Response.json({});
  });
  const res = await cold(post({}), store.ctx);

  assert.equal(res.status, 200);
  assert.equal((await res.json()).checked, FIXTURE.length);
});

test("an unwritable store costs speed, not correctness", async () => {
  const handler = await load("vet-shortlist", directoryReturning(FIXTURE));
  const hostile = {
    files: {
      async readJson() {
        throw new Error("permission denied");
      },
      async writeJson() {
        throw new Error("read-only");
      },
    },
  };
  const res = await handler(post({}), hostile);

  assert.equal(res.status, 200);
  assert.equal((await res.json()).checked, FIXTURE.length);
});

// --- the facilitator index: the other ~42,000 endpoints -----------------------

const PAYAI = "https://facilitator.payai.network";

function bazaarItem(resource, over = {}) {
  return {
    resource,
    method: "GET",
    serviceName: "Sponge",
    accepts: [
      {
        network: "eip155:8453",
        asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        amount: "3000",
        payTo: "0xCCCC",
        scheme: "exact",
      },
    ],
    ...over,
  };
}

/**
 * Serves the directory as usual, plus one facilitator whose page size is
 * clamped below the requested limit - the thirdweb behaviour that stopped the
 * first version at a quarter of that catalog.
 */
function withBazaar({ total, pageSize, failing = [], probeImpl } = {}) {
  const items = Array.from({ length: total }, (_, i) =>
    bazaarItem(`https://seller${i}.example/paid`),
  );
  return async (url, init) => {
    const href = String(url);
    if (href.startsWith(DIRECTORY)) return directoryReturning(FIXTURE)(href, init);

    const source = Object.keys({ payai: 1, coinbase: 1, thirdweb: 1, dexter: 1 }).find((n) =>
      href.includes(n),
    );
    if (source && href.includes("/discovery/resources")) {
      if (failing.includes(source)) throw new Error("facilitator down");
      if (source !== "payai") return Response.json({ items: [], pagination: { total: 0 } });
      const offset = Number(new URL(href).searchParams.get("offset") ?? 0);
      return Response.json({
        items: items.slice(offset, offset + pageSize),
        pagination: { total },
      });
    }
    if (probeImpl) return probeImpl(href, init);
    return Response.json({ accepts: [{ payTo: "0xCCCC", network: "eip155:8453", amount: "1000" }] }, { status: 402 });
  };
}

test("a facilitator that clamps page size is paged to the end, not stopped early", async () => {
  const store = fileStore();
  // 1,000 asked for, 200 returned: exhaustion must be judged by the declared
  // total, never by a short page.
  const handler = await load("vet-service", withBazaar({ total: 700, pageSize: 200 }));
  const body = await (await handler(post({ url: "https://seller699.example/paid" }), store.ctx)).json();

  assert.equal(body.ok, true);
  assert.equal(body.coverage.sources.payai.indexed, 700);
  assert.equal(body.coverage.complete, true);
  assert.equal(body.coverage.percent, 100);
});

test("an endpoint outside the traction directory is `unrated`, not suspect", async () => {
  const store = fileStore();
  const handler = await load("vet-service", withBazaar({ total: 10, pageSize: 10 }));
  const body = await (await handler(post({ url: "https://seller3.example/paid" }), store.ctx)).json();

  assert.equal(body.result.verdict, "unrated");
  assert.equal(body.result.priceUsd, 0.003);
  assert.equal(body.result.listedPayTo, "0xcccc");
  assert.match(body.result.why[0], /no traction data published/);
  assert.equal(body.source, "payai");
});

test("a rated service still answers from the traction directory, without touching the index", async () => {
  const store = fileStore();
  const handler = await load(
    "vet-service",
    withBazaar({ total: 10, pageSize: 10, failing: ["payai", "coinbase", "thirdweb", "dexter"] }),
  );
  const body = await (await handler(post({ slug: "healthy" }), store.ctx)).json();

  assert.equal(body.ok, true);
  assert.equal(body.source, "x402-list");
  assert.equal(body.coverage, undefined);
});

test("a permanently failing facilitator is retired instead of blocking coverage", async () => {
  const store = fileStore();
  const fetchImpl = withBazaar({ total: 10, pageSize: 10, failing: ["payai"] });

  let body;
  for (let i = 0; i < 3; i += 1) {
    const handler = await load("vet-service", fetchImpl);
    body = await (await handler(post({ url: "https://absent.example/x" }), store.ctx)).json();
  }

  assert.equal(body.coverage.sources.payai.unavailable, true);
  assert.equal(body.coverage.complete, true);
});

test("a miss against a partial index says so instead of claiming absence", async () => {
  const store = fileStore();
  const handler = await load("vet-service", withBazaar({ total: 10, pageSize: 10 }));
  const res = await handler(post({ url: "https://never-listed.example/x" }), store.ctx);
  const body = await res.json();

  assert.equal(res.status, 404);
  assert.equal(body.coverage.complete, true);
  assert.match(body.note, /register nowhere by design/);
});

test("unrated endpoints reach the shortlist only when asked, cheapest first", async () => {
  const store = fileStore();
  const handler = await load("vet-shortlist", withBazaar({ total: 5, pageSize: 5 }));

  const without = await (await handler(post({}), store.ctx)).json();
  assert.ok(!without.results.some((r) => r.verdict === "unrated"));
  assert.equal(without.coverage, undefined);

  const withUnrated = await (await handler(post({ includeUnrated: true, limit: 6 }), store.ctx)).json();
  const unrated = withUnrated.results.filter((r) => r.verdict === "unrated");
  assert.ok(unrated.length > 0);
  assert.equal(withUnrated.coverage.endpointsIndexed, 5);
});

test("a 400 to an unpaid probe is `unknown`: validating a body first is not being broken", async () => {
  const handler = await load(
    "vet-service",
    directoryReturning(FIXTURE, async () => new Response("bad request", { status: 400 })),
  );
  const body = await (await handler(post({ slug: "healthy" }))).json();

  assert.equal(body.result.liveProbe, "unknown");
  assert.equal(body.result.verdict, "ok");
  assert.match(body.result.liveProbeReason, /HTTP 400/);
});

test("payment requirements are parsed from the v2 header", async () => {
  const challenge = Buffer.from(JSON.stringify({
    accepts: [{
      scheme: "exact",
      network: "eip155:8453",
      asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      amount: "7000",
      payTo: "0xBBBB",
    }],
  })).toString("base64");
  const handler = await load(
    "vet-service",
    directoryReturning(FIXTURE, async () => new Response("payment required", {
      status: 402,
      headers: { "payment-required": challenge },
    })),
  );
  const body = await (await handler(post({ slug: "healthy" }))).json();

  assert.equal(body.result.liveProbe, "alive");
  assert.equal(body.result.livePayTo, "0xbbbb");
  assert.equal(body.result.liveAmount, "7000");
  assert.equal(body.result.payToChanged, true);
});

test("a 402 without valid payment requirements is unknown, not alive", async () => {
  const handler = await load(
    "vet-service",
    directoryReturning(FIXTURE, async () =>
      Response.json({ accepts: [{ payTo: "0xAAAA", network: "eip155:8453" }] }, { status: 402 }),
    ),
  );
  const body = await (await handler(post({ slug: "healthy" }))).json();

  assert.equal(body.result.liveProbe, "unknown");
  assert.match(body.result.liveProbeReason, /payment requirements/);
});

test("URL lookup still compares the live receiver with directory detail", async () => {
  const handler = await load(
    "vet-service",
    directoryReturning(FIXTURE, async () =>
      Response.json({ accepts: [{ payTo: "0xBBBB", network: "eip155:8453", amount: "1000" }] }, { status: 402 }),
    ),
  );
  const body = await (await handler(post({ url: `https://svc.example/paid${PAID_PATH}` }))).json();

  assert.equal(body.result.payToChanged, true);
  assert.equal(body.result.verdict, "check");
});

test("probe targets reject credential confusion and private IPs", async () => {
  const rootService = service({ slug: "root", base_url: "https://svc.example" });
  let externalProbe = false;
  const handler = await load("vet-service", directoryReturning([rootService], async () => {
    externalProbe = true;
    return Response.json({ accepts: [] }, { status: 402 });
  }));
  const res = await handler(post({ url: "https://svc.example@127.0.0.1/admin" }));

  assert.equal(res.status, 400);
  assert.equal(externalProbe, false);
  assert.match((await res.json()).attribution, /CC BY 4\.0/);
});

test("live probes never follow redirects automatically", async () => {
  let redirectMode = null;
  const handler = await load("vet-service", directoryReturning(FIXTURE, async (_url, init) => {
    redirectMode = init.redirect;
    return new Response(null, { status: 302, headers: { location: "http://127.0.0.1/admin" } });
  }));
  const body = await (await handler(post({ slug: "healthy" }))).json();

  assert.equal(redirectMode, "manual");
  assert.equal(body.result.liveProbe, "unknown");
});

test("unrated Base results are globally cheapest and preserve URL case", async () => {
  const asset = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
  const items = [
    bazaarItem("https://seller.example/Expensive", { accepts: [{ network: "eip155:8453", asset, amount: "9000", payTo: "0xCCCC" }] }),
    bazaarItem("https://seller.example/CheapPath", { accepts: [{ network: "eip155:8453", asset, amount: "1000", payTo: "0xCCCC" }] }),
  ];
  const handler = await load("vet-shortlist", async (url, init) => {
    const href = String(url);
    if (href.startsWith(DIRECTORY)) return directoryReturning([])(href, init);
    if (href.includes("/discovery/resources")) {
      const isPayai = href.includes("facilitator.payai.network");
      const offset = Number(new URL(href).searchParams.get("offset") ?? 0);
      return Response.json({
        items: isPayai && offset === 0 ? items : [],
        pagination: { total: isPayai ? items.length : 0 },
      });
    }
    throw new Error(`unexpected fetch ${href}`);
  });
  const body = await (await handler(post({ includeUnrated: true, network: "base", limit: 1 }))).json();

  assert.equal(body.results[0].resource, "https://seller.example/CheapPath");
  assert.equal(body.results[0].priceUsd, 0.001);
  assert.deepEqual(body.results[0].networks, ["base"]);
  assert.equal(body.checked, 2);
});

test("malformed shortlist input returns an unpaid 400", async () => {
  const handler = await load("vet-shortlist", directoryReturning(FIXTURE));
  const malformed = new Request("https://x402.bankr.bot/w/vet-shortlist", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{",
  });

  assert.equal((await handler(malformed)).status, 400);
  assert.equal((await handler(post({ maxPriceUsd: null }))).status, 400);
});

test("deployment sources stay identical and the manifest describes text output", async () => {
  for (const name of ["vet-service", "vet-shortlist"]) {
    const ts = await readFile(new URL(`../x402/${name}/index.ts`, import.meta.url), "utf8");
    const mjs = await readFile(new URL(`../x402/${name}/index.mjs`, import.meta.url), "utf8");
    assert.equal(ts, mjs);
  }
  const manifest = JSON.parse(await readFile(new URL("../bankr.x402.json", import.meta.url), "utf8"));
  assert.equal(manifest.services["vet-shortlist"].schema.output.type, "string");
  assert.equal(manifest.services["vet-service"].schema.output.type, "string");
  assert.match(manifest.services["vet-shortlist"].schema.input.properties.probe.description, /50/);
});

test("public handlers return readable Markdown instead of JSON", async () => {
  const serviceHandler = await loadDefault("vet-service", directoryReturning(FIXTURE));
  const serviceResponse = await serviceHandler(post({ slug: "healthy" }));
  const serviceText = await serviceResponse.text();

  assert.match(serviceResponse.headers.get("content-type"), /^text\/markdown/);
  assert.match(serviceText, /Verdict: Recommended/);
  assert.match(serviceText, /Payment recipient: unchanged/);
  assert.doesNotMatch(serviceText, /^\s*\{/);

  const errorResponse = await serviceHandler(post({}));
  const errorText = await errorResponse.text();
  assert.equal(errorResponse.status, 400);
  assert.match(errorText, /Invalid request/);
  assert.match(errorText, /Data attribution/);
  assert.doesNotMatch(errorText, /^\s*\{/);

  const shortlistHandler = await loadDefault("vet-shortlist", directoryReturning(FIXTURE));
  const shortlistResponse = await shortlistHandler(post({ category: "AI" }));
  const shortlistText = await shortlistResponse.text();

  assert.match(shortlistResponse.headers.get("content-type"), /^text\/markdown/);
  assert.match(shortlistText, /services worth calling/);
  assert.match(shortlistText, /Healthy — Recommended/);
  assert.doesNotMatch(shortlistText, /^\s*\{/);
});
