// SingIt agent allowance: connect a wallet, set limits, allow once, let the agent buy.
// Design: docs/allowance-web-v1.md. Every rule is enforced by the API and the
// chain; this page shows state and hands prepared requests to the wallet.

import { api, ApiError, csrf, setCsrf } from "./api.js";
import {
  appKitConfigured, disconnectAppKit, discover, openAppKit, Wallet, wallets, walletError, watchAppKit,
} from "./wallet.js";

const $ = (selector, root = document) => root.querySelector(selector);
const view = $("#view");
const accountEl = $("#account");
const tabsEl = $("#tabs");
const modalEl = $("#modal");

const state = {
  wallet: null,        // the connected wallet (needed to sign)
  session: null,       // {account, address, telegramLinked}
  allowance: null,     // GET /allowance
  tab: "allowance",
  modal: null,         // {type: "busy" | "wallets" | "quote" | "pause", …}
  preset: 0,           // index into PRESETS
  method: null,        // "approve" | "permit"; null = pick from the wallet's ETH
  linkCode: null,
  tools: null,
  quote: null,
  result: null,
  products: null,
  search: null,
  purchases: null,
  revealed: {},
  view: "chat",        // signed in: "chat" | "allowance" | "purchases" | "telegram"
  chats: [],           // the sidebar's history
  chatId: null,
  messages: [],
  sending: false,
  done: {},            // wallet cards already carried out, by "messageId:index"
  menuOpen: false,
};

const PRESETS = [
  { daily: "5", per: "1", days: "30", name: "Starter" },
  { daily: "20", per: "5", days: "30", name: "Everyday" },
  { daily: "100", per: "10", days: "90", name: "Power" },
];

// -- helpers --

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const short = (address) => (address ? `${address.slice(0, 6)}…${address.slice(-4)}` : "");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const mark = (cls = "mark") => `<svg class="${cls}" aria-hidden="true"><use href="#singit-mark"/></svg>`;
const orb = `<span class="btn-orb" aria-hidden="true">↗</span>`;

function amount(atomic) {
  const n = BigInt(String(atomic ?? 0));
  const whole = n / 1000000n;
  const frac = (n % 1000000n).toString().padStart(6, "0").replace(/0+$/, "");
  return `${whole.toLocaleString("en-US")}${frac ? "." + frac.slice(0, 4) : ""}`;
}
const usdc = (atomic) => `${amount(atomic)} USDC`;

function atomicFromUsdc(text) {
  const clean = String(text).trim();
  if (!/^\d+(\.\d{1,6})?$/.test(clean)) throw new Error("Enter an amount like 5 or 2.50.");
  const [whole, frac = ""] = clean.split(".");
  return BigInt(whole) * 1000000n + BigInt(frac.padEnd(6, "0"));
}

function toast(message, error = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (error ? " error" : "");
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, error ? 9000 : 5000);
}

function explain(error) {
  if (error instanceof ApiError) {
    if (error.status === 401 && state.session) {
      state.session = null;
      setCsrf(null);
      return "Your session ended. Sign in again.";
    }
    return error.message;
  }
  return walletError(error);
}

async function busy(text, task) {
  state.modal = { type: "busy", text };
  render();
  try {
    return await task();
  } catch (error) {
    toast(explain(error), true);
    return undefined;
  } finally {
    if (state.modal?.type === "busy") state.modal = null;
    render();
  }
}

function say(text) {
  state.modal = { type: "busy", text };
  renderModal();
}

// -- wallet and session --

function setWallet(wallet) {
  const changed = (wallet?.address || "").toLowerCase() !== (state.wallet?.address || "").toLowerCase();
  state.wallet = wallet;
  if (changed) render();
}

async function needWallet() {
  if (!state.wallet) {
    connectWallet();
    throw new Error("Connect your wallet, then try again.");
  }
  if (state.session && state.wallet.address.toLowerCase() !== state.session.address.toLowerCase()) {
    throw new Error(`Switch your wallet to ${short(state.session.address)}, the address you signed in with.`);
  }
  return state.wallet;
}

function connectWallet() {
  if (appKitConfigured()) {
    openAppKit().catch((error) => toast(explain(error), true));
  } else {
    state.modal = { type: "wallets" };
    render();
  }
}

async function connectInjected(uuid) {
  const choice = wallets().find((w) => w.info.uuid === uuid);
  await busy("Connecting your wallet…", async () => {
    const wallet = await Wallet.fromInjected(choice);
    wallet.on("accountsChanged", (accounts) => {
      wallet.address = accounts?.[0] || null;
      setWallet(wallet.address ? wallet : null);
    });
    state.wallet = wallet;
  });
}

async function signIn() {
  await busy("Sign the message in your wallet. It does not move any funds.", async () => {
    const wallet = await needWallet();
    const { message } = await api.nonce(wallet.address);
    const signature = await wallet.signMessage(message);
    const signed = await api.verify(message, signature);
    setCsrf(signed.csrfToken);
    state.session = signed;
    await Promise.all([loadAllowance(), loadChats()]);
  });
}

async function signOut() {
  try { await api.logout(); } catch { /* the cookie may already be gone */ }
  setCsrf(null);
  await disconnectAppKit().catch(() => {});
  Object.assign(state, { session: null, wallet: null, allowance: null, quote: null, result: null, purchases: null,
                         linkCode: null, revealed: {}, tab: "allowance", modal: null, view: "chat", chats: [],
                         chatId: null, messages: [], done: {} });
  render();
}

async function loadAllowance() {
  state.allowance = await api.allowance();
}

// -- the limiter --

async function createLimiter() {
  let daily, per, days;
  try {
    daily = $("#daily").value.trim(); per = $("#per").value.trim(); days = $("#days").value.trim();
    if (atomicFromUsdc(per) > atomicFromUsdc(daily)) throw new Error("The per-purchase limit cannot be above the daily limit.");
  } catch (error) { toast(error.message, true); return; }
  await busy("Creating your limiter on Base. This takes about a minute.", async () => {
    await api.setup(daily, per, days);
    await loadAllowance();
    toast("Your limiter is ready. Now allow it to spend.");
  });
}

function hasGas() {
  return BigInt(state.allowance?.ownerEthWei || "0") >= 50000000000000n;
}

function method() {
  return state.method || (hasGas() ? "approve" : "permit");
}

async function walletOperation(kind, body) {
  return busy("Preparing…", async () => {
    const wallet = await needWallet();
    const prepared = await api.prepare(kind, body);
    say(`${prepared.walletShows} Confirm it in your wallet.`);
    let answer;
    try {
      answer = prepared.method === "permit"
        ? { operation: prepared.operation, signature: await wallet.signTypedData(prepared.typedData) }
        : { operation: prepared.operation, txHash: await wallet.sendTransaction(prepared.tx) };
    } catch (error) {
      // Some wallets (smart accounts, no ETH) cannot send the transaction. The same
      // approval also works as a signature we send; the next press uses that.
      if (prepared.method === "approve" && error?.code !== 4001 && !/reject|denied|cancel/i.test(error?.message || "")) {
        state.method = "permit";
        throw new Error("Your wallet couldn't send the transaction. Press the button again — this time you only sign, and we pay the gas.");
      }
      throw error;
    }
    say("Waiting for Base to confirm it…");
    let op = await api.submit(kind, answer);
    const deadline = Date.now() + 180000;
    while (!["DONE", "FAILED", "EXPIRED"].includes(op.state) && Date.now() < deadline) {
      await sleep(2000);
      op = await api.operation(prepared.operation);
    }
    await loadAllowance();
    if (op.state === "DONE") toast(op.detail || "Done.");
    else toast(op.detail || `The request ended as ${op.state.toLowerCase()}.`, true);
    return op.state === "DONE";
  });
}

function grant() {
  let value;
  try { value = $("#grant-amount").value.trim(); atomicFromUsdc(value); }  // exact, as typed; checked only
  catch (error) { toast(error.message, true); return; }
  walletOperation("grant", { amount: value, method: method() });
}

async function pause() {
  state.modal = null;
  await busy("Pausing your limiter…", async () => {
    await api.pause();
    await loadAllowance();
    toast("Paused for good. To be thorough, also revoke the allowance.");
  });
}

// -- the shop --

async function loadTools() {
  await busy("Loading the catalog…", async () => { state.tools = (await api.tools()).tools; });
  state.tools ??= [];
  render();
}

function requiredFields(tool) {
  const schema = tool?.inputSchema || {};
  return (schema.required || []).filter((name) => schema.properties?.[name]);
}

async function quoteTool(id) {
  const tool = state.tools.find((t) => t.id === id);
  const body = { tool: id };
  for (const name of requiredFields(tool)) {
    const value = document.querySelector(`[data-field="${CSS.escape(id + ":" + name)}"]`)?.value.trim();
    if (!value) { toast(`Fill in ${name}.`, true); return; }
    body[name] = value;
  }
  await busy("Asking the seller for the price…", async () => {
    state.quote = { kind: "tool", ...(await api.toolQuote(body)) };
    state.result = null;
  });
  if (state.quote) { state.modal = { type: "quote" }; render(); }
}

async function searchBitrefill() {
  const query = $("#bf-query").value.trim();
  const country = $("#bf-country").value.trim().toUpperCase();
  state.search = { query, country };
  if (!query) { toast("Type what you are looking for.", true); return; }
  await busy("Searching Bitrefill…", async () => {
    state.products = (await api.bitrefillSearch(query, country)).products || [];
  });
}

async function quoteBitrefill(slug) {
  const pkg = document.querySelector(`[data-package="${CSS.escape(slug)}"]`)?.value.trim();
  if (!pkg) { toast("Enter the amount you want on the card.", true); return; }
  await busy("Asking Bitrefill for the price…", async () => {
    state.quote = { kind: "bitrefill", ...(await api.bitrefillQuote(slug, pkg)) };
    state.result = null;
  });
  if (state.quote) { state.modal = { type: "quote" }; render(); }
}

async function buy() {
  const quote = state.quote;
  state.modal = null;
  await busy("Buying from your allowance…", async () => {
    const result = quote.kind === "tool" ? await api.toolBuy(quote.quoteId) : await api.bitrefillBuy(quote.quoteId);
    state.quote = null;
    state.result = { title: quote.kind === "tool" ? quote.tool.name : quote.name, text: result.text || "Done." };
    state.purchases = null;
    await loadAllowance();
  });
}

async function loadPurchases() {
  await busy("Loading your purchases…", async () => { state.purchases = (await api.purchases()).purchases || []; });
  state.purchases ??= [];
  render();
}

// -- views --

function renderNav() {
  const signedIn = Boolean(state.session);
  tabsEl.hidden = !signedIn;
  for (const tab of tabsEl.querySelectorAll(".tab")) tab.classList.toggle("active", tab.dataset.tab === state.tab);
  if (signedIn) {
    accountEl.innerHTML = `<button class="account-chip" data-action="account" title="Sign out">
      <span class="dot"></span><span class="addr">${esc(short(state.session.address))}</span></button>`;
  } else if (state.wallet) {
    accountEl.innerHTML = `<button class="account-chip" data-action="account"><span class="dot"></span>
      <span class="addr">${esc(short(state.wallet.address))}</span></button>`;
  } else {
    accountEl.innerHTML = `<button class="btn btn-primary btn-sm" data-action="connect">Connect wallet</button>`;
  }
}

function renderHero() {
  const cta = state.wallet
    ? `<button class="btn btn-primary btn-lg has-orb" data-action="sign-in">Sign in as ${esc(short(state.wallet.address))}${orb}</button>
       <button class="btn btn-ghost btn-lg" data-action="connect">Use another wallet</button>`
    : `<button class="btn btn-primary btn-lg has-orb" data-action="connect">Connect wallet${orb}</button>
       <a class="btn btn-ghost btn-lg" href="https://singitai.app/#how">How it works</a>`;
  return `
    <section class="hero">
      <span class="eyebrow">Agent allowance · Base</span>
      <h1>Your agent spends.<br><em>Your wallet</em> keeps the money.</h1>
      <p class="sub">Set a daily limit and a per-purchase limit once. Your agent buys inside them without asking again,
        and one signature takes it all back.</p>
      <div class="row" style="justify-content:center">${cta}</div>
      <div class="steps">
        <div class="step"><span class="n">01</span><h3>Connect and sign in</h3>
          <p>Rabby, MetaMask, Phantom or any WalletConnect wallet. Signing in moves nothing.</p></div>
        <div class="step"><span class="n">02</span><h3>Set your limits</h3>
          <p>We deploy a small contract that enforces them on Base. You pay no gas for it.</p></div>
        <div class="step"><span class="n">03</span><h3>Allow once</h3>
          <p>One approval from your wallet. Revoke it any time, here or on revoke.cash.</p></div>
      </div>
    </section>`;
}

// What the agent can really spend today: the day's room within the allowance, plus what it already holds.
function spendableToday(a) {
  const left = BigInt(a.remainingTodayAtomic);
  const allowed = BigInt(a.allowanceAtomic);
  return (left < allowed ? left : allowed) + BigInt(a.floatAtomic || 0);
}

function statusPill(code) {
  const map = { granted: ["ok", "Active"], waiting_for_grant: ["warn", "Waiting for your approval"],
                paused: ["bad", "Paused"], expired: ["bad", "Expired"] };
  const [cls, text] = map[code] || ["", code];
  return `<span class="status ${cls}">${esc(text)}</span>`;
}

function renderSetup(heading) {
  const p = PRESETS[state.preset] || PRESETS[0];
  return `
    <div class="bezel glow"><div class="core">
      <h2>${heading}</h2>
      <p>A small contract on Base that lets your agent spend only within these limits. Creating it moves no money,
        and we pay its gas.</p>
      <div class="presets">${PRESETS.map((x, i) => `
        <button class="preset ${i === state.preset ? "selected" : ""}" data-action="preset" data-index="${i}">
          <b>$${x.daily}<span> / day</span></b><span>${x.name} · up to $${x.per} a purchase · ${x.days} days</span>
        </button>`).join("")}</div>
      <div class="fields">
        <div class="field"><label for="daily">Daily limit, USDC</label><input class="input" id="daily" inputmode="decimal" value="${esc(p.daily)}"></div>
        <div class="field"><label for="per">Per purchase, USDC</label><input class="input" id="per" inputmode="decimal" value="${esc(p.per)}"></div>
        <div class="field"><label for="days">Lasts, days</label><input class="input" id="days" type="number" min="1" max="90" value="${esc(p.days)}"></div>
      </div>
      <div class="row" style="margin-top:22px"><button class="btn btn-primary has-orb" data-action="create-limiter">Create my limiter${orb}</button></div>
    </div></div>`;
}

// No choice for the user: a transaction when the wallet has ETH (wallets show it
// most clearly), otherwise — or after a transaction the wallet could not send —
// a gas-free signature we send. One sentence says which.
function methodSwitch() {
  return `<p class="hint">${method() === "permit"
    ? "Your wallet will ask you to sign; we send it to Base and pay the gas."
    : "Your wallet will ask you to confirm a transaction; it costs a few cents of ETH."}</p>`;
}

function renderAllowance() {
  const a = state.allowance;
  if (!a) return `<p class="faint">Loading…</p>`;
  if (!a.configured) {
    return `<div class="page-head"><span class="eyebrow">Step 2 of 3</span>
      <h1>Set your agent's <em>limits</em>.</h1>
      <p>Your money stays in your wallet. These limits hold even if our server or the agent is compromised.</p></div>
      ${renderSetup("Choose your limits")}`;
  }
  const blocked = a.state === "paused" || a.state === "expired";
  const daily = BigInt(a.dailyCapAtomic);
  const left = BigInt(a.remainingTodayAtomic);
  const spent = daily - left;
  const pct = daily > 0n ? Number((spent * 100n) / daily) : 0;
  const granted = BigInt(a.allowanceAtomic) > 0n;
  const canSpend = spendableToday(a);
  const title = blocked ? `This limiter is <em>stopped</em>.`
    : a.state === "granted" ? `Your agent is <em>ready</em>.` : `Now <em>allow</em> it, once.`;
  const intro = blocked ? "Create a new limiter to continue."
    : a.state === "granted" ? "It buys on its own, inside your limits. Everything below reads from Base."
    : "One approval from your wallet lets the limiter pay for your agent, never more than your limits.";

  return `
    <div class="page-head"><span class="eyebrow">${a.state === "waiting_for_grant" ? "Step 3 of 3" : "Agent allowance"}</span>
      <h1>${title}</h1><p>${intro}</p></div>

    <div class="stack">
      <div class="bezel"><div class="core">
        <div class="balance">
          <div>
            <div class="spread"><span class="label">Your agent can spend today</span>${statusPill(a.state)}</div>
            <div class="big">${amount(blocked ? 0 : canSpend)}<small>USDC</small></div>
            <div class="meter"><span style="width:${Math.min(100, pct)}%"></span></div>
            <p class="faint">${amount(spent)} of the ${amount(daily)} USDC daily limit used today · up to ${usdc(a.perPurchaseCapAtomic)} a purchase</p>
          </div>
          <div class="stats" style="grid-template-columns:1fr;margin-top:0;gap:8px">
            <div class="stat"><div class="label">Allowed from your wallet</div><div class="v">${usdc(a.allowanceAtomic)}</div></div>
            <div class="stat"><div class="label">Held by your agent</div><div class="v">${usdc(a.floatAtomic)}</div></div>
            <div class="stat"><div class="label">In your wallet</div><div class="v">${usdc(a.ownerUsdcAtomic)}</div></div>
          </div>
        </div>
        <div class="links">
          <a class="chip" href="https://base.blockscout.com/address/${esc(a.limiter)}?tab=contract" target="_blank" rel="noopener">Limiter ${esc(short(a.limiter))} · verified code ↗</a>
          <a class="chip" href="https://revoke.cash/address/${esc(a.owner)}?chainId=8453" target="_blank" rel="noopener">revoke.cash ↗</a>
          <span class="chip">Until ${esc(new Date(a.expiry * 1000).toLocaleDateString())}</span>
        </div>
      </div></div>

      ${renderStale(a)}

      ${blocked ? renderSetup("Create a new limiter") : `
      <div class="grid-2">
        <div class="bezel ${a.state === "granted" ? "" : "glow"}"><div class="core stack">
          <div><h2>${a.state === "granted" ? "Change the allowance" : "Allow it to spend"}</h2>
            <p>The total your agent may take through the limiter, still at most ${usdc(a.dailyCapAtomic)} a day.
              Check the address in your wallet ends in <span class="mono">${esc(a.limiter.slice(-4))}</span>.</p></div>
          <div class="field"><label for="grant-amount">Amount, USDC</label>
            <input class="input" id="grant-amount" inputmode="decimal" value="${esc(amount(a.dailyCapAtomic).replace(/,/g, ""))}"></div>
          ${methodSwitch()}
          <button class="btn btn-primary has-orb" data-action="grant">Approve from my wallet${orb}</button>
        </div></div>
        ${renderTelegram()}
      </div>`}

      ${renderActivity(a)}

      <div class="grid-2">
        ${granted ? `
        <div class="bezel"><div class="core stack">
          <div><h2>Revoke</h2><p>Sets the allowance to 0. Whatever your agent still holds comes back to your wallet.</p></div>
          ${methodSwitch()}
          <button class="btn btn-ghost" data-action="revoke">Revoke the allowance</button>
        </div></div>` : ""}
        ${blocked ? "" : `
        <div class="bezel danger"><div class="core stack">
          <div><h2>Emergency stop</h2><p>Pauses the limiter forever. Nothing can move through it again; you would create a new one.</p></div>
          <button class="btn btn-danger" data-action="ask-pause">Pause the limiter</button>
        </div></div>`}
      </div>
    </div>`;
}

function renderStale(a) {
  const stale = a.staleLimiters || [];
  if (!stale.length) return "";
  return `<div class="bezel danger"><div class="core stack">
    <div><h2>Revoke your old limiter${stale.length > 1 ? "s" : ""}</h2>
      <p>Your agent no longer uses ${stale.length > 1 ? "these" : "this one"}, but your wallet still allows ${stale.length > 1 ? "them" : "it"} to take USDC. Revoke to close it.</p></div>
    ${stale.map((x) => `<div class="spread"><span class="mono">${esc(short(x.limiter))} · ${usdc(x.allowanceAtomic)} allowed</span>
      <button class="btn btn-danger btn-sm" data-action="revoke-old" data-limiter="${esc(x.limiter)}">Revoke</button></div>`).join("")}
  </div></div>`;
}

function renderTelegram() {
  const linked = state.session?.telegramLinked;
  const body = linked
    ? `<p>Your SingIt bot uses this allowance and sends the watcher's notices.</p>
       <button class="btn btn-ghost" data-action="unlink">Unlink Telegram</button>`
    : state.linkCode
      ? `<p class="note">Send <span class="mono">/link ${esc(state.linkCode.code)}</span> to the SingIt bot within 10 minutes, then reload.</p>`
      : `<p>Shop from the SingIt bot with the same allowance, and get a message whenever money moves.</p>
         <button class="btn btn-ghost" data-action="link">Link Telegram</button>`;
  return `<div class="bezel"><div class="core stack"><div><h2>Telegram</h2></div>${body}</div></div>`;
}

function renderActivity(a) {
  const alerts = a.alerts || [];
  const ops = a.operations || [];
  if (!alerts.length && !ops.length) return "";
  return `
    <div class="bezel"><div class="core">
      <h2>Activity</h2>
      <ul class="list feed">
        ${alerts.map((x) => `<li><span>${x.severity === "ALARM" ? "⚠️ " : ""}${esc(x.text)}</span>
          <span class="when">${esc(new Date(x.createdAt * 1000).toLocaleString())}</span></li>`).join("")}
        ${ops.map((line) => `<li><span class="mono">${esc(line)}</span></li>`).join("")}
      </ul>
    </div></div>`;
}

function renderShop() {
  const a = state.allowance;
  if (!a?.configured || a.state !== "granted") {
    return `<div class="page-head"><span class="eyebrow">Shop</span><h1>Allow your agent <em>first</em>.</h1>
      <p>Set up your limits and approve them on the Allowance tab; then everything here is paid from your allowance.</p>
      <div class="row" style="margin-top:22px"><button class="btn btn-primary has-orb" data-tab="allowance">Go to Allowance${orb}</button></div></div>`;
  }
  if (!state.tools && !state.modal) queueMicrotask(loadTools);
  return `
    <div class="page-head"><span class="eyebrow">Shop</span><h1>Spend from your <em>allowance</em>.</h1>
      <p>${usdc(spendableToday(a))} available today, up to ${usdc(a.perPurchaseCapAtomic)} a purchase. Every price is confirmed before you pay.</p></div>
    <div class="stack">
      ${state.result ? `<div class="bezel glow"><div class="core"><div class="spread"><h2>${esc(state.result.title)}</h2>
        <span class="status ok">Delivered</span></div><pre class="result">${esc(state.result.text)}</pre></div></div>` : ""}
      <div class="bezel"><div class="core">
        <h2>Paid tools</h2><p>Your agent pays per call over x402.</p>
        <div class="tool-grid">${(state.tools || []).map((tool) => `
          <div class="tool">
            <h3>${esc(tool.name)}</h3>
            <p>${esc(tool.description || "")}</p>
            ${requiredFields(tool).map((name) => `<input class="input" placeholder="${esc(name)}" data-field="${esc(tool.id)}:${esc(name)}">`).join("")}
            <button class="btn btn-ghost btn-sm" data-action="quote-tool" data-id="${esc(tool.id)}">Get price</button>
          </div>`).join("")}</div>
      </div></div>
      <div class="bezel"><div class="core">
        <h2>Gift cards</h2><p>Thousands of brands through Bitrefill. The code is shown to you once.</p>
        <div class="searchbar">
          <input class="input" id="bf-query" placeholder="Amazon, Steam, Netflix…" value="${esc(state.search?.query)}">
          <input class="input country" id="bf-country" maxlength="2" placeholder="DE" value="${esc(state.search?.country)}">
          <button class="btn btn-primary" data-action="search-bitrefill">Search</button>
        </div>
        ${state.products ? `<ul class="list" style="margin-top:14px">${state.products.length ? state.products.slice(0, 12).map((p) => `
          <li><div><h3>${esc(p.name)}</h3><p class="mono faint">${esc(p.slug)}</p></div>
            <div class="row"><input class="input" style="width:110px" placeholder="amount" data-package="${esc(p.slug)}">
            <button class="btn btn-ghost btn-sm" data-action="quote-bitrefill" data-slug="${esc(p.slug)}">Price</button></div></li>`).join("")
          : `<li><p>Nothing found. Try another word or country.</p></li>`}</ul>` : ""}
      </div></div>
    </div>`;
}

function renderPurchases() {
  if (!state.purchases && !state.modal) queueMicrotask(loadPurchases);
  const items = state.purchases || [];
  return `
    <div class="page-head"><span class="eyebrow">Purchases</span><h1>What your agent <em>bought</em>.</h1>
      <p>Gift card codes are shown once, on request. Store them safely.</p></div>
    <div class="bezel"><div class="core">
      ${items.length ? `<ul class="list">${items.map((p) => `
        <li>
          <div>
            <h3>${esc(p.name)}</h3>
            <p>${esc([p.denomination, p.paid, p.network, p.status].filter(Boolean).join(" · "))}
              ${p.transactionUrl ? ` · <a href="${esc(p.transactionUrl)}" target="_blank" rel="noopener">transaction ↗</a>` : ""}</p>
            ${state.revealed[p.id] ? `<pre class="result">${esc(state.revealed[p.id])}</pre>` : ""}
          </div>
          ${p.canReveal && !state.revealed[p.id] ? `<button class="btn btn-ghost btn-sm" data-action="reveal" data-id="${esc(p.id)}">Show code once</button>` : ""}
        </li>`).join("")}</ul>` : `<p>${state.purchases ? "Nothing yet." : "Loading…"}</p>`}
    </div></div>`;
}

// -- modals --

function modal(inner) {
  return `<div class="overlay" data-action="dismiss"><div class="modal bezel" role="dialog" aria-modal="true"><div class="core">${inner}</div></div></div>`;
}

function renderModal() {
  const m = state.modal;
  if (!m) { modalEl.innerHTML = ""; return; }
  if (m.type === "busy") {
    modalEl.innerHTML = `<div class="overlay"><div class="modal bezel"><div class="core busy">
      <div class="spinner"></div><p>${esc(m.text)}</p></div></div></div>`;
    return;
  }
  if (m.type === "wallets") {
    const list = wallets();
    modalEl.innerHTML = modal(`
      <div class="approval-head"><div class="avatar">${mark()}</div>
        <div><strong>Connect a wallet</strong><span>Base · USDC</span></div></div>
      ${list.length ? `<div class="wallets">${list.map(({ info }) => `
        <button class="wallet-option" data-action="connect-injected" data-uuid="${esc(info.uuid)}">
          ${info.icon ? `<img src="${esc(info.icon)}" alt="">` : `<span class="ph"></span>`}${esc(info.name)}</button>`).join("")}</div>`
        : `<p class="note warn">No wallet found in this browser. Install Rabby, MetaMask or Phantom, then reload.</p>`}
      <p class="hint">A Trezor or Ledger behind your wallet works too: the device shows every signature.</p>`);
    return;
  }
  if (m.type === "quote") {
    const q = state.quote;
    const rows = q.kind === "tool"
      ? [["Product", q.tool.name], ["Price", `${q.priceUsd} USDC`], ["Network", "Base"], ["Paid to", short(q.payTo)], ["Paid from", "your allowance"]]
      : [["Product", q.name], ["Card value", `${q.package} ${q.packageCurrency || ""}`.trim()], ["Price", `${q.priceUsd} USDC`],
         ["Network", "Base"], ["Paid to", "Bitrefill · code comes to you"], ["Refunds", "none once delivered"]];
    modalEl.innerHTML = modal(`
      <div class="approval-head"><div class="avatar">${mark()}</div>
        <div><strong>Confirm your purchase</strong><span>Paid by your agent from your allowance</span></div></div>
      <dl class="rows">${rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
      <div class="actions">
        <button class="btn btn-primary" data-action="buy">Buy · ${esc(q.priceUsd)} USDC</button>
        <button class="btn btn-ghost" data-action="dismiss-button">Cancel</button>
      </div>
      <p class="hint">Price held until ${esc(new Date(q.expiresAt * 1000).toLocaleTimeString())}.</p>`);
    return;
  }
  if (m.type === "pause") {
    modalEl.innerHTML = modal(`
      <div class="approval-head"><div class="avatar">${mark()}</div>
        <div><strong>Pause the limiter for good?</strong><span>This cannot be undone</span></div></div>
      <p>Nothing will be able to move through it again. Your allowance stays until you revoke it; you would create a new limiter to continue.</p>
      <div class="actions" style="margin-top:20px">
        <button class="btn btn-danger" data-action="pause">Pause for good</button>
        <button class="btn btn-ghost" data-action="dismiss-button">Keep it</button>
      </div>`);
  }
}

function render() {
  const signedIn = Boolean(state.session);
  document.body.classList.toggle("in-app", signedIn);
  $("#app").hidden = !signedIn;
  if (!signedIn) {
    renderNav();
    view.innerHTML = renderHero();
  } else {
    renderSidebar();
    renderMain();
  }
  renderModal();
}

// -- the chat app --

const SUGGESTIONS = [
  ["Set a $20 daily limit, $5 per purchase", "Your agent spends only inside it"],
  ["Buy crypto news", "0.001 USDC, paid from your allowance"],
  ["Find a Steam gift card in Germany", "Bitrefill gift cards, eSIMs and top-ups"],
  ["What can my agent spend today?", "Limits, allowance and what it holds"],
];

function renderSidebar() {
  $("#side-chats").innerHTML = state.chats.length
    ? state.chats.map((c) => `<button class="side-item ${c.id === state.chatId && state.view === "chat" ? "active" : ""}"
        data-action="open-chat" data-id="${esc(c.id)}"><span class="ico">💬</span><span class="t">${esc(c.title)}</span></button>`).join("")
    : `<p class="faint" style="padding:6px 10px">Your chats appear here.</p>`;
  const a = state.allowance;
  const amountLine = a?.configured
    ? `<div class="label">Can spend today</div><div class="amt">${amount(spendableToday(a))}<small>USDC</small></div>
       <div style="margin-top:8px">${statusPill(a.state)}</div>
       ${(a.staleLimiters || []).length ? `<div class="faint" style="color:var(--danger);margin-top:8px">⚠ Old limiter to revoke</div>` : ""}`
    : `<div class="label">Allowance</div><div style="margin-top:4px;font-size:14px;color:var(--text-soft)">No limits yet</div>`;
  $("#side-nav").innerHTML = `
    <button class="side-allowance ${state.view === "allowance" ? "active" : ""}" data-action="go" data-view="allowance">${amountLine}</button>
    <button class="side-item ${state.view === "purchases" ? "active" : ""}" data-action="go" data-view="purchases"><span class="ico">🧾</span>Purchases</button>
    <button class="side-item ${state.view === "telegram" ? "active" : ""}" data-action="go" data-view="telegram"><span class="ico">✈️</span>Telegram</button>
    <div class="side-account">
      <span class="account-chip" style="cursor:default"><span class="dot"></span>${esc(short(state.session.address))}</span>
      <button class="btn btn-ghost btn-sm" data-action="sign-out">Sign out</button>
    </div>`;
  $("#app").classList.toggle("menu-open", state.menuOpen);
  $("#top-title").textContent = state.view === "chat"
    ? (state.chats.find((c) => c.id === state.chatId)?.title || "New chat")
    : { allowance: "Allowance", purchases: "Purchases", telegram: "Telegram" }[state.view];
}

function renderMain() {
  const host = $("#main");
  if (state.view !== "chat") {
    const page = { allowance: renderAllowance, purchases: renderPurchases,
                   telegram: () => `<div class="page-head"><span class="eyebrow">Telegram</span><h1>Your agent in <em>Telegram</em>.</h1></div>${renderTelegram()}` }[state.view];
    host.innerHTML = `<div class="pane"><div class="pane-inner">${page()}</div></div>`;
    return;
  }
  const draft = $("#composer")?.value ?? "";
  const chat = state.messages.length || state.sending
    ? state.messages.map(renderMessage).join("") + (state.sending ? `<div class="msg assistant"><div class="avatar">${mark()}</div>
        <div class="body"><span class="typing"><i></i><i></i><i></i></span></div></div>` : "")
    : `<div class="empty"><div class="avatar">${mark()}</div>
        <h1>What should your agent <em>do</em>?</h1>
        <p>Ask in your own words. It sets your limits, finds what you need and buys it inside your limits. Your wallet signs only the approvals.</p>
        <div class="suggestions">${SUGGESTIONS.map(([t, sub]) => `<button class="suggestion" data-action="suggest" data-text="${esc(t)}">${esc(t)}<span>${esc(sub)}</span></button>`).join("")}</div>
      </div>`;
  host.innerHTML = `
    <div class="chat" id="chat"><div class="chat-inner">${chat}</div></div>
    <div class="composer-wrap">
      <form class="composer" data-form="send">
        <textarea id="composer" rows="1" placeholder="Message your agent…" aria-label="Message">${esc(draft)}</textarea>
        <button class="send" type="submit" aria-label="Send" ${state.sending ? "disabled" : ""}>↑</button>
      </form>
      <p class="composer-hint">Inside your limits your agent buys without asking. Approvals and revokes always need your wallet.</p>
    </div>`;
  autosize($("#composer"));
  const pane = $("#chat");
  pane.scrollTop = pane.scrollHeight;
}

function autosize(el) {
  if (!el) return;
  if (!el.value) { el.style.height = ""; return; }  // one row
  requestAnimationFrame(() => {                       // after layout, or scrollHeight is wrong
    el.style.height = "auto";
    el.style.height = Math.min(200, el.scrollHeight) + "px";
  });
}

function formatText(text) {
  return esc(text).replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(https:\/\/[\w./?=&%#:-]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
}

function renderMessage(m) {
  if (m.role === "user") return `<div class="msg user"><div class="bubble">${esc(m.text)}</div></div>`;
  const cards = (m.cards || []).map((card, i) => renderCard(card, `${m.id}:${i}`)).join("");
  return `<div class="msg assistant"><div class="avatar">${mark()}</div><div class="body">
    <div class="text">${formatText(m.text)}</div>${cards ? `<div class="cards">${cards}</div>` : ""}</div></div>`;
}

function renderCard(card, key) {
  const a = state.allowance;
  if (card.type === "allowance" && a?.configured) {
    return `<div class="card"><div class="spread"><h3>Your allowance</h3>${statusPill(a.state)}</div>
      <div class="kv"><div><span class="label">Can spend today</span><b>${usdc(spendableToday(a))}</b></div>
        <div><span class="label">Daily limit</span><b>${usdc(a.dailyCapAtomic)}</b></div>
        <div><span class="label">Per purchase</span><b>${usdc(a.perPurchaseCapAtomic)}</b></div>
        <div><span class="label">Held by agent</span><b>${usdc(a.floatAtomic)}</b></div></div>
      <div class="row"><button class="btn btn-ghost btn-sm" data-action="go" data-view="allowance">Open allowance</button></div></div>`;
  }
  if (card.type === "wallet" && card.old) {
    if (state.done[key]) return `<div class="card done"><h3>Old limiter revoked ✓</h3></div>`;
    return `<div class="card accent"><h3>Revoke the old limiter <span class="mono">${esc(short(card.limiter))}</span></h3>
      <p class="faint">It still may take up to ${esc(card.allowance)} USDC from your wallet, though your agent no longer uses it.</p>
      <div style="margin-top:10px">${methodSwitch()}</div>
      <div class="row"><button class="btn btn-primary btn-sm has-orb" data-action="card-wallet" data-key="${esc(key)}"
        data-kind="revoke" data-limiter="${esc(card.limiter)}">Revoke in wallet${orb}</button></div></div>`;
  }
  if (card.type === "wallet") {
    const grant = card.kind === "grant";
    if (state.done[key]) return `<div class="card done"><h3>${grant ? `Approved ${esc(card.amount)} USDC` : "Revoked"} ✓</h3></div>`;
    return `<div class="card accent"><h3>${grant ? `Approve ${esc(card.amount)} USDC for your agent` : "Revoke your agent's allowance"}</h3>
      <p class="faint">${grant ? "Your wallet will show an approval for your limiter" : "Your wallet will show an approval of 0 for your limiter"}
        ${card.limiter ? ` <span class="mono">${esc(short(card.limiter))}</span>` : ""}. Nothing moves until a purchase needs it.</p>
      <div style="margin-top:10px">${methodSwitch()}</div>
      <div class="row"><button class="btn btn-primary btn-sm has-orb" data-action="card-wallet" data-key="${esc(key)}"
        data-kind="${esc(card.kind)}" data-amount="${esc(card.amount || "")}">${grant ? "Approve in wallet" : "Revoke in wallet"}${orb}</button></div></div>`;
  }
  if (card.type === "limits_proposal") {
    if (state.done[key]) return `<div class="card done"><h3>Limiter created ✓</h3></div>`;
    return `<div class="card accent"><h3>Your limits</h3>
      <div class="fields" style="margin-top:10px">
        <div class="field"><label>Daily, USDC</label><input class="input" data-field-of="${esc(key)}" data-name="daily" value="${esc(card.daily)}"></div>
        <div class="field"><label>Per purchase</label><input class="input" data-field-of="${esc(key)}" data-name="per" value="${esc(card.per)}"></div>
        <div class="field"><label>Days</label><input class="input" data-field-of="${esc(key)}" data-name="days" value="${esc(card.days || "30")}"></div>
      </div>
      <div class="row"><button class="btn btn-primary btn-sm has-orb" data-action="card-limits" data-key="${esc(key)}" data-lang="${esc(card.lang || "en")}">Create my limiter${orb}</button></div></div>`;
  }
  if (card.type === "products") {
    return `<div class="card">${(card.items || []).map((p) => `
      <div class="product"><div><h3>${esc(p.name)}</h3><p class="faint mono">${esc(p.slug)}</p></div>
        <div class="row" style="margin:0"><input class="input" placeholder="amount" data-package="${esc(p.slug)}">
        <button class="btn btn-primary btn-sm" data-action="card-buy" data-slug="${esc(p.slug)}" data-name="${esc(p.name)}" data-lang="${esc(card.lang || "en")}">Buy</button></div></div>`).join("")}
      <p class="faint" style="margin-top:8px">Enter the card value (for example 10) and I'll buy it from your allowance.</p></div>`;
  }
  if (card.type === "receipt") {
    return `<div class="card accent"><div class="spread"><h3>${esc(card.name)}</h3><span class="status ok">Paid ${esc(card.price)} USDC</span></div>
      ${card.txId ? `<p class="faint"><a href="https://basescan.org/tx/${esc(card.txId)}" target="_blank" rel="noopener">Transaction ↗</a></p>` : ""}
      ${card.result ? `<pre class="result">${esc(card.result)}</pre>` : ""}
      ${card.giftcard ? `<div class="row"><button class="btn btn-ghost btn-sm" data-action="go" data-view="purchases">Show my code</button></div>` : ""}</div>`;
  }
  if (card.type === "purchases") {
    return `<div class="card">${(card.items || []).map((p) => `<div class="product"><div><h3>${esc(p.name)}</h3>
      <p class="faint">${esc([p.denomination, p.paid, p.status].filter(Boolean).join(" · "))}</p></div></div>`).join("")}
      <div class="row"><button class="btn btn-ghost btn-sm" data-action="go" data-view="purchases">All purchases</button></div></div>`;
  }
  if (card.type === "link_telegram") {
    return `<div class="card">${state.linkCode
      ? `<p>Send <span class="mono">/link ${esc(state.linkCode.code)}</span> to the SingIt bot within 10 minutes.</p>`
      : `<button class="btn btn-ghost btn-sm" data-action="link">Get a code</button>`}</div>`;
  }
  return "";
}

async function loadChats() {
  try { state.chats = (await api.chats()).chats || []; } catch { state.chats = []; }
}

async function sendMessage(text) {
  text = String(text || "").trim();
  if (!text || state.sending) return;
  state.view = "chat";
  state.menuOpen = false;
  state.messages.push({ id: "pending", role: "user", text, cards: [] });
  state.sending = true;
  if ($("#composer")) $("#composer").value = "";
  render();
  try {
    const reply = await api.say(state.chatId, text);
    state.chatId = reply.chatId;
    state.messages = state.messages.filter((m) => m.id !== "pending").concat(reply.messages);
    await Promise.all([loadChats(), loadAllowance().catch(() => {})]);
  } catch (error) {
    state.messages = state.messages.filter((m) => m.id !== "pending");
    toast(explain(error), true);
    if ($("#composer")) $("#composer").value = text;
  } finally {
    state.sending = false;
    render();
  }
}

async function cardAction(action) {
  if (state.sending) return;
  state.sending = true;
  render();
  try {
    const reply = await api.act(state.chatId, action);
    state.messages = state.messages.concat(reply.messages);
    await loadAllowance().catch(() => {});
  } catch (error) {
    toast(explain(error), true);
  } finally {
    state.sending = false;
    render();
  }
}

async function openChat(id) {
  state.menuOpen = false;
  await busy("Opening…", async () => {
    const chat = await api.chat(id);
    state.chatId = chat.chatId;
    state.messages = chat.messages;
    state.view = "chat";
  });
}

// -- events --

const actions = {
  connect: connectWallet,
  "connect-injected": (el) => { state.modal = null; connectInjected(el.dataset.uuid); },
  account: () => (state.session ? signOut() : connectWallet()),
  "sign-in": signIn,
  preset: (el) => {
    state.preset = Number(el.dataset.index);
    const p = PRESETS[state.preset];
    $("#daily").value = p.daily; $("#per").value = p.per; $("#days").value = p.days;
    for (const b of document.querySelectorAll(".preset")) b.classList.toggle("selected", b === el);
  },
  "create-limiter": createLimiter,
  method: (el) => {
    state.method = el.dataset.method;
    const typed = $("#grant-amount")?.value;
    render();
    if (typed !== undefined && $("#grant-amount")) $("#grant-amount").value = typed;
  },
  grant,
  revoke: () => walletOperation("revoke", { method: method() }),
  "revoke-old": (el) => walletOperation("revoke", { method: method(), limiter: el.dataset.limiter }),
  "ask-pause": () => { state.modal = { type: "pause" }; render(); },
  pause,
  link: () => busy("Getting a code…", async () => { state.linkCode = await api.linkTelegram(); }),
  unlink: () => busy("Unlinking…", async () => {
    await api.unlinkTelegram();
    state.session = { ...state.session, telegramLinked: false };
    state.linkCode = null;
  }),
  "quote-tool": (el) => quoteTool(el.dataset.id),
  "search-bitrefill": searchBitrefill,
  "quote-bitrefill": (el) => quoteBitrefill(el.dataset.slug),
  buy,
  reveal: (el) => busy("Fetching the code…", async () => {
    state.revealed[el.dataset.id] = (await api.reveal(el.dataset.id)).text || "No code.";
  }),
  "new-chat": () => { Object.assign(state, { chatId: null, messages: [], view: "chat", menuOpen: false }); render(); $("#composer")?.focus(); },
  "open-chat": (el) => openChat(el.dataset.id),
  go: (el) => {
    state.view = el.dataset.view;
    state.menuOpen = false;
    if (state.view === "purchases") state.purchases = null;
    render();
  },
  "open-menu": () => { state.menuOpen = true; render(); },
  "close-menu": () => { state.menuOpen = false; render(); },
  "sign-out": signOut,
  suggest: (el) => sendMessage(el.dataset.text),
  "card-wallet": async (el) => {
    const key = el.dataset.key;
    const ok = el.dataset.kind === "grant"
      ? await walletOperation("grant", { amount: el.dataset.amount, method: method() })
      : await walletOperation("revoke", { method: method(), ...(el.dataset.limiter ? { limiter: el.dataset.limiter } : {}) });
    if (ok) { state.done[key] = true; render(); }
  },
  "card-limits": (el) => {
    const key = el.dataset.key;
    const value = (name) => document.querySelector(`[data-field-of="${CSS.escape(key)}"][data-name="${name}"]`)?.value.trim();
    state.done[key] = true;
    cardAction({ type: "create_limiter", daily: value("daily"), per: value("per"), days: value("days"), lang: el.dataset.lang });
  },
  "card-buy": (el) => {
    const pkg = document.querySelector(`[data-package="${CSS.escape(el.dataset.slug)}"]`)?.value.trim();
    if (!pkg) { toast("Enter the card value first.", true); return; }
    cardAction({ type: "buy_giftcard", slug: el.dataset.slug, package: pkg, name: el.dataset.name, lang: el.dataset.lang });
  },
  dismiss: () => { state.modal = null; render(); },
  "dismiss-button": () => { state.modal = null; render(); },
};

document.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab && !state.modal) {
    state.tab = tab.dataset.tab;
    state.result = null;
    render();
    window.scrollTo({ top: 0 });
    return;
  }
  const el = event.target.closest("[data-action]");
  if (!el) return;
  // A click inside a modal's card is not a click on its backdrop.
  if (el.dataset.action === "dismiss" && event.target !== el) return;
  if (state.modal?.type === "busy") return;
  actions[el.dataset.action]?.(el);
});

document.addEventListener("submit", (event) => {
  if (event.target.dataset.form !== "send") return;
  event.preventDefault();
  sendMessage($("#composer").value);
});

document.addEventListener("input", (event) => {
  if (event.target.id === "composer") autosize(event.target);
});

document.addEventListener("keydown", (event) => {
  if (event.target.id === "composer" && event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage(event.target.value);
    return;
  }
  if (event.key === "Escape" && state.modal && state.modal.type !== "busy") { state.modal = null; render(); }
  if (event.key === "Enter" && event.target.id === "bf-query") searchBitrefill();
});

// -- start --

async function start() {
  discover(() => { if (state.modal?.type === "wallets") renderModal(); });
  if (appKitConfigured()) watchAppKit(setWallet).catch((error) => toast(explain(error), true));
  if (csrf()) {
    try {
      state.session = await api.session();
      await Promise.all([loadAllowance(), loadChats()]);
    } catch {
      state.session = null;
    }
  }
  render();
}

start();
