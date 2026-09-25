// SingIt agent allowance: connect a wallet, set limits, sign once, let the agent buy.
// Design: docs/allowance-web-v1.md. Every rule is enforced by the API and the
// chain; this page only shows state and hands prepared requests to the wallet.

import { api, ApiError, csrf, setCsrf } from "./api.js";
import { discover, wallets, walletConnectAvailable, Wallet, walletError } from "./wallet.js";

const $ = (selector, root = document) => root.querySelector(selector);
const view = $("#view");
const accountEl = $("#account");
const tabsEl = $("#tabs");

const state = {
  wallet: null,        // the connected wallet (needed to sign)
  session: null,       // {account, address, telegramLinked}
  allowance: null,     // GET /allowance
  tab: "allowance",
  busy: "",            // what we are waiting for, shown instead of the actions
  confirmPause: false,
  linkCode: null,
  tools: null,
  pickWallet: false,   // signed in (cookie) but no wallet connected yet: show the choices
  quote: null,         // a tool or Bitrefill quote waiting for "Buy"
  result: null,        // {title, text}
  products: null,
  search: null,        // the last Bitrefill search, kept in the form
  purchases: null,
  revealed: {},
};

// -- small helpers --

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const short = (address) => (address ? `${address.slice(0, 6)}…${address.slice(-4)}` : "");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function usdc(atomic) {
  const n = BigInt(String(atomic ?? 0));
  const whole = n / 1000000n;
  const frac = (n % 1000000n).toString().padStart(6, "0").replace(/0+$/, "");
  return `${whole}${frac ? "." + frac : ""} USDC`;
}

function atomicFromUsdc(text) {
  const clean = String(text).trim();
  if (!/^\d+(\.\d{1,6})?$/.test(clean)) throw new Error("Enter an amount like 5 or 2.50.");
  const [whole, frac = ""] = clean.split(".");
  return BigInt(whole) * 1000000n + BigInt(frac.padEnd(6, "0"));
}

const usdcInput = (atomic) => usdc(atomic).replace(" USDC", "");

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
    if (error.status === 401) {
      state.session = null;
      setCsrf(null);
      return "Your session ended. Sign in again.";
    }
    return error.message;
  }
  return walletError(error);
}

async function busy(label, task) {
  state.busy = label;
  render();
  try {
    return await task();
  } catch (error) {
    toast(explain(error), true);
    return undefined;
  } finally {
    state.busy = "";
    render();
  }
}

function setBusy(label) {
  state.busy = label;
  render();
}

// -- the wallet and the session --

async function needWallet() {
  if (!state.wallet) throw new Error("Connect your wallet first (top right).");
  if (state.session && state.wallet.address.toLowerCase() !== state.session.address.toLowerCase()) {
    throw new Error(`Switch your wallet to ${short(state.session.address)}, the address you signed in with.`);
  }
  return state.wallet;
}

async function connect(choice) {
  await busy("Connecting your wallet…", async () => {
    const wallet = await Wallet.connect(choice);
    wallet.on("accountsChanged", (accounts) => {
      wallet.address = accounts?.[0] || null;
      if (!wallet.address) state.wallet = null;
      render();
    });
    state.wallet = wallet;
  });
}

async function signIn() {
  await busy("Sign the message in your wallet. It does not move funds.", async () => {
    const wallet = await needWallet();
    const { message } = await api.nonce(wallet.address);
    const signature = await wallet.signMessage(message);
    const signed = await api.verify(message, signature);
    setCsrf(signed.csrfToken);
    state.session = signed;
    await loadAllowance();
  });
}

async function signOut() {
  try { await api.logout(); } catch { /* the cookie may already be gone */ }
  setCsrf(null);
  Object.assign(state, { session: null, allowance: null, quote: null, result: null, purchases: null, linkCode: null, revealed: {} });
  render();
}

async function loadAllowance() {
  state.allowance = await api.allowance();
}

// -- actions: the limiter --

function readSetup() {
  const daily = $("#daily").value.trim();
  const per = $("#per").value.trim();
  const days = $("#days").value.trim();
  if (atomicFromUsdc(per) > atomicFromUsdc(daily)) throw new Error("The per-purchase cap cannot be above the daily cap.");
  return [daily, per, days];
}

async function createLimiter() {
  let fields;
  try { fields = readSetup(); } catch (error) { toast(error.message, true); return; }
  await busy("Creating your limiter on Base. This takes about a minute…", async () => {
    await api.setup(...fields);
    await loadAllowance();
    toast("Your limiter is ready. Now allow it to spend from your wallet.");
  });
}

async function walletOperation(kind, body) {
  await busy("Preparing…", async () => {
    const wallet = await needWallet();
    const prepared = await api.prepare(kind, body);
    setBusy(`${prepared.walletShows} Confirm it in your wallet.`);
    const answer = prepared.method === "permit"
      ? { operation: prepared.operation, signature: await wallet.signTypedData(prepared.typedData) }
      : { operation: prepared.operation, txHash: await wallet.sendTransaction(prepared.tx) };
    setBusy("Waiting for Base to confirm it…");
    let op = await api.submit(kind, answer);
    const deadline = Date.now() + 180000;
    while (!["DONE", "FAILED", "EXPIRED"].includes(op.state) && Date.now() < deadline) {
      await sleep(2000);
      op = await api.operation(prepared.operation);
    }
    await loadAllowance();
    if (op.state === "DONE") toast(op.detail || "Done.");
    else toast(op.detail || `The request ended as ${op.state.toLowerCase()}.`, true);
  });
}

function chosenMethod(name) {
  return document.querySelector(`input[name="${name}"]:checked`)?.value || "approve";
}

function grant() {
  let amount;
  try { amount = usdcInput(atomicFromUsdc($("#grant-amount").value)); } catch (error) { toast(error.message, true); return; }
  walletOperation("grant", { amount, method: chosenMethod("grant-method") });
}

function revoke() {
  walletOperation("revoke", { method: chosenMethod("revoke-method") });
}

async function pause() {
  state.confirmPause = false;
  await busy("Pausing your limiter…", async () => {
    await api.pause();
    await loadAllowance();
    toast("Paused for good. To be thorough, also revoke the allowance from your wallet.");
  });
}

async function linkTelegram() {
  await busy("Getting a code…", async () => {
    state.linkCode = await api.linkTelegram();
  });
}

async function unlinkTelegram() {
  await busy("Unlinking…", async () => {
    await api.unlinkTelegram();
    state.session = { ...state.session, telegramLinked: false };
    state.linkCode = null;
  });
}

// -- actions: the shop --

async function loadTools() {
  await busy("Loading the catalog…", async () => {
    state.tools = (await api.tools()).tools;
  });
  state.tools ??= [];  // a failure is shown once, not retried on every render
  render();
}

async function quoteTool(id) {
  const tool = state.tools.find((t) => t.id === id);
  const body = { tool: id };
  for (const name of requiredFields(tool)) {
    const value = document.querySelector(`[data-field="${id}:${name}"]`)?.value.trim();
    if (!value) { toast(`Fill in ${name}.`, true); return; }
    body[name] = value;
  }
  await busy("Asking the seller for the price…", async () => {
    const quote = await api.toolQuote(body);
    state.quote = { kind: "tool", ...quote };
    state.result = null;
  });
}

async function searchBitrefill() {
  // Read the form before busy() redraws the page.
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
  if (!pkg) { toast("Enter the denomination you want.", true); return; }
  await busy("Asking Bitrefill for the price…", async () => {
    const quote = await api.bitrefillQuote(slug, pkg);
    state.quote = { kind: "bitrefill", ...quote };
    state.result = null;
  });
}

async function buy() {
  const quote = state.quote;
  await busy("Buying from your allowance…", async () => {
    const result = quote.kind === "tool" ? await api.toolBuy(quote.quoteId) : await api.bitrefillBuy(quote.quoteId);
    state.quote = null;
    state.result = { title: quote.kind === "tool" ? quote.tool.name : quote.name, text: result.text || "Done." };
    state.purchases = null;
    await loadAllowance();
  });
}

async function loadPurchases() {
  await busy("Loading your purchases…", async () => {
    state.purchases = (await api.purchases()).purchases || [];
  });
  state.purchases ??= [];
  render();
}

async function reveal(id) {
  await busy("Fetching the code…", async () => {
    state.revealed[id] = (await api.reveal(id)).text || "No code.";
  });
}

function requiredFields(tool) {
  const schema = tool?.inputSchema || {};
  return (schema.required || []).filter((name) => schema.properties?.[name]);
}

// -- rendering --

function renderAccount() {
  if (state.session) {
    accountEl.innerHTML = `
      <span class="badge ok">${esc(short(state.session.address))}</span>
      ${state.wallet ? "" : `<button class="btn small" data-action="pick-wallet">Connect wallet</button>`}
      <button class="btn small" data-action="sign-out">Sign out</button>`;
  } else if (state.wallet) {
    accountEl.innerHTML = `<span class="badge">${esc(short(state.wallet.address))}</span>`;
  } else {
    accountEl.innerHTML = "";
  }
}

function walletChoices() {
  const list = wallets();
  const buttons = list.map(({ info }) => `
    <button class="btn wallet-btn" data-action="connect" data-uuid="${esc(info.uuid)}">
      ${info.icon ? `<img src="${esc(info.icon)}" alt="">` : ""}${esc(info.name)}
    </button>`).join("");
  const walletConnect = walletConnectAvailable()
    ? `<button class="btn wallet-btn" data-action="connect" data-uuid="walletconnect">WalletConnect (mobile)</button>` : "";
  if (!buttons && !walletConnect) {
    return `<p class="notice warn">No wallet found in this browser. Install Rabby, MetaMask or Phantom, then reload.</p>`;
  }
  return `<div class="row">${buttons}${walletConnect}</div>`;
}

function renderSignIn() {
  return `
    <h1>Give your agent an allowance, not your keys.</h1>
    <p class="lead">Your money stays in your wallet. You set a daily limit and a per-purchase limit once; the agent buys
      inside them without asking again, and you can revoke it with one signature.</p>
    <div class="card stack">
      ${state.wallet ? `
        <h2>Sign in</h2>
        <p>Your wallet will ask you to sign a message for SingIt. It does not move funds or approve spending.</p>
        <div class="row"><button class="btn primary" data-action="sign-in">Sign in as ${esc(short(state.wallet.address))}</button></div>`
      : `<h2>Connect your wallet</h2>
        <p>Rabby, MetaMask or Phantom on Base. A Trezor or Ledger behind them works too: the device shows every signature.</p>
        ${walletChoices()}`}
    </div>`;
}

function renderSetup(title) {
  const presets = [["5", "1", "30"], ["20", "5", "30"], ["100", "10", "90"]];
  return `
    <div class="card stack">
      <h2>${esc(title)}</h2>
      <p>Your limiter is a small contract that only lets your agent spend within these limits. Creating it moves no money;
        we pay its gas.</p>
      <div class="choice">${presets.map(([d, p, n]) => `
        <label><input type="radio" name="preset" data-action="preset" data-preset="${d},${p},${n}"> $${d}/day · $${p} per purchase · ${n} days</label>`).join("")}
      </div>
      <div class="fields">
        <div><label for="daily">Daily limit (USDC)</label><input id="daily" type="text" inputmode="decimal" value="5"></div>
        <div><label for="per">Per purchase (USDC)</label><input id="per" type="text" inputmode="decimal" value="1"></div>
        <div><label for="days">Lasts (days)</label><input id="days" type="number" min="1" max="90" value="30"></div>
      </div>
      <div class="row"><button class="btn primary" data-action="create-limiter">Create my limiter</button></div>
    </div>`;
}

function methodChoice(name, allowance) {
  const hasGas = BigInt(allowance.ownerEthWei || "0") >= 50000000000000n;
  const approve = hasGas ? "checked" : "";
  const permit = hasGas ? "" : "checked";
  return `
    <div class="choice">
      <label><input type="radio" name="${name}" value="approve" ${approve}> Transaction (you pay a little ETH gas)</label>
      <label><input type="radio" name="${name}" value="permit" ${permit}> Signature only (no gas; we send it)</label>
    </div>
    ${hasGas ? "" : `<p class="muted">Your wallet has no ETH on Base, so a gas-free signature is selected.</p>`}`;
}

function stateBadge(stateCode) {
  const map = { granted: ["ok", "active"], waiting_for_grant: ["warn", "waiting for your allowance"],
                paused: ["bad", "paused"], expired: ["bad", "expired"] };
  const [cls, text] = map[stateCode] || ["", stateCode];
  return `<span class="badge ${cls}">${esc(text)}</span>`;
}

function renderAllowance() {
  const a = state.allowance;
  if (!a) return `<div class="card"><p>Loading…</p></div>`;
  if (!a.configured) return renderSetup("Set your limits");
  const limiter = a.limiter;
  const blocked = a.state === "paused" || a.state === "expired";
  const spentToday = BigInt(a.dailyCapAtomic) - BigInt(a.remainingTodayAtomic);
  return `
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h2>Your agent's allowance</h2>${stateBadge(a.state)}
      </div>
      <div class="grid">
        <div class="stat"><div class="label">Allowed from your wallet</div><div class="value">${usdc(a.allowanceAtomic)}</div></div>
        <div class="stat"><div class="label">Left today</div><div class="value">${usdc(a.remainingTodayAtomic)}</div></div>
        <div class="stat"><div class="label">Agent float</div><div class="value">${usdc(a.floatAtomic)}</div></div>
        <div class="stat"><div class="label">Your wallet</div><div class="value">${usdc(a.ownerUsdcAtomic)}</div></div>
      </div>
      <p class="muted" style="margin-top:14px">
        Limits: ${usdc(a.dailyCapAtomic)} a day (${usdc(spentToday)} spent today), ${usdc(a.perPurchaseCapAtomic)} a purchase,
        until ${esc(new Date(a.expiry * 1000).toLocaleString())}.<br>
        Limiter <span class="mono">${esc(limiter)}</span> ·
        <a href="https://base.blockscout.com/address/${esc(limiter)}?tab=contract" target="_blank" rel="noopener">verified code</a> ·
        <a href="https://revoke.cash/address/${esc(a.owner)}?chainId=8453" target="_blank" rel="noopener">revoke.cash</a>
      </p>
    </div>

    ${blocked ? `
      <div class="card stack"><p class="notice warn">This limiter is ${esc(a.state)}. Create a new one to continue;
        if the old one still has an allowance, revoke it below.</p></div>
      ${renderSetup("Create a new limiter")}` : `
      <div class="card stack">
        <h2>${a.state === "granted" ? "Change the allowance" : "Allow it to spend"}</h2>
        <p>This is the total your agent may take from your wallet through the limiter, still at most
          ${usdc(a.dailyCapAtomic)} a day. Your wallet will show the limiter's address: check it ends in
          <span class="mono">${esc(limiter.slice(-4))}</span>.</p>
        <div class="fields"><div><label for="grant-amount">Amount (USDC)</label>
          <input id="grant-amount" type="text" inputmode="decimal" value="${esc(usdcInput(a.dailyCapAtomic))}"></div></div>
        ${methodChoice("grant-method", a)}
        <div class="row"><button class="btn primary" data-action="grant">Allow from my wallet</button></div>
      </div>`}

    ${BigInt(a.allowanceAtomic) > 0n ? `
      <div class="card stack">
        <h2>Revoke</h2>
        <p>Sets the allowance to 0. The agent's float comes back to your wallet.</p>
        ${methodChoice("revoke-method", a)}
        <div class="row"><button class="btn" data-action="revoke">Revoke the allowance</button></div>
      </div>` : ""}

    ${renderActivity(a)}
    ${renderTelegram()}
    ${blocked ? "" : `
      <div class="card danger stack">
        <h2>Emergency stop</h2>
        <p>Pauses the limiter forever. Nothing can move through it again; you would create a new one.</p>
        <div class="row">${state.confirmPause
          ? `<button class="btn danger" data-action="pause">Yes, pause it for good</button>
             <button class="btn" data-action="cancel-pause">Cancel</button>`
          : `<button class="btn danger" data-action="ask-pause">Pause the limiter</button>`}</div>
      </div>`}`;
}

function renderActivity(a) {
  const alerts = a.alerts || [];
  const ops = a.operations || [];
  if (!alerts.length && !ops.length) return "";
  return `
    <div class="card">
      <h2>Activity</h2>
      <ul class="list">
        ${alerts.map((x) => `<li><span>${x.severity === "ALARM" ? "⚠️ " : ""}${esc(x.text)}</span>
          <span class="muted">${esc(new Date(x.createdAt * 1000).toLocaleString())}</span></li>`).join("")}
        ${ops.map((line) => `<li><span class="mono">${esc(line)}</span></li>`).join("")}
      </ul>
    </div>`;
}

function renderTelegram() {
  const linked = state.session?.telegramLinked;
  return `
    <div class="card stack">
      <h2>Telegram</h2>
      ${linked
        ? `<p>Your SingIt bot chat uses this allowance and gets the watcher's notices.</p>
           <div class="row"><button class="btn" data-action="unlink">Unlink Telegram</button></div>`
        : state.linkCode
          ? `<p class="notice">Send <span class="mono">/link ${esc(state.linkCode.code)}</span> to the SingIt bot within 10 minutes.
               Then reload this page.</p>`
          : `<p>Use the same allowance from the SingIt bot and get notices there.</p>
             <div class="row"><button class="btn" data-action="link">Link Telegram</button></div>`}
    </div>`;
}

function renderQuote() {
  const q = state.quote;
  if (!q) return "";
  const rows = q.kind === "tool"
    ? [["Product", q.tool.name], ["Price", `${q.priceUsd} USDC`], ["Network", "Base (USDC)"],
       ["Paid to", q.payTo], ["Paid from", "your allowance"]]
    : [["Product", q.name], ["Denomination", `${q.package} ${q.packageCurrency || ""}`.trim()],
       ["Price", `${q.priceUsd} USDC`], ["Network", "Base (USDC)"],
       ["Paid to", "Bitrefill (x402); the code comes to you"], ["Refunds", "none once delivered"]];
  return `
    <div class="confirm">
      <h3>Confirm your purchase</h3>
      <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      <div class="row">
        <button class="btn primary" data-action="buy">Buy for ${esc(q.priceUsd)} USDC</button>
        <button class="btn" data-action="cancel-quote">Cancel</button>
      </div>
      <p class="muted" style="margin-top:8px">Valid until ${esc(new Date(q.expiresAt * 1000).toLocaleTimeString())}.</p>
    </div>`;
}

function renderShop() {
  const a = state.allowance;
  if (!a?.configured || a.state !== "granted") {
    return `<div class="card"><p class="notice warn">Set up and allow your limiter first (Allowance tab).</p></div>`;
  }
  if (!state.tools && !state.busy) queueMicrotask(loadTools);
  return `
    ${state.quote || state.result ? `<div class="card stack">
      ${renderQuote()}
      ${state.result ? `<h3>${esc(state.result.title)}</h3><pre class="result">${esc(state.result.text)}</pre>` : ""}
    </div>` : ""}
    <div class="card stack">
      <h2>Paid tools</h2>
      <p>The agent pays per call over x402, from your allowance.</p>
      <ul class="list">${(state.tools || []).map((tool) => `
        <li>
          <div><h3>${esc(tool.name)}</h3><p class="muted">${esc(tool.description || "")}</p>
            ${requiredFields(tool).map((name) => `<input type="text" placeholder="${esc(name)}" data-field="${esc(tool.id)}:${esc(name)}" style="margin-top:6px">`).join("")}
          </div>
          <button class="btn small" data-action="quote-tool" data-id="${esc(tool.id)}">Get price</button>
        </li>`).join("")}</ul>
    </div>
    <div class="card stack">
      <h2>Gift cards (Bitrefill)</h2>
      <div class="fields">
        <div><label for="bf-query">Search</label><input id="bf-query" type="text" placeholder="amazon, steam, netflix…" value="${esc(state.search?.query)}"></div>
        <div><label for="bf-country">Country (optional)</label><input id="bf-country" type="text" maxlength="2" placeholder="DE" value="${esc(state.search?.country)}"></div>
      </div>
      <div class="row"><button class="btn" data-action="search-bitrefill">Search</button></div>
      ${state.products ? `<ul class="list">${state.products.length ? state.products.slice(0, 10).map((p) => `
        <li><div><h3>${esc(p.name)}</h3><p class="muted mono">${esc(p.slug)}</p></div>
          <div class="row"><input type="text" placeholder="amount" data-package="${esc(p.slug)}" style="width:110px">
          <button class="btn small" data-action="quote-bitrefill" data-slug="${esc(p.slug)}">Price</button></div></li>`).join("")
        : "<li>Nothing found.</li>"}</ul>` : ""}
    </div>`;
}

function renderPurchases() {
  if (!state.purchases && !state.busy) queueMicrotask(loadPurchases);
  const items = state.purchases || [];
  return `
    <div class="card">
      <h2>Purchases</h2>
      ${items.length ? `<ul class="list">${items.map((p) => `
        <li>
          <div>
            <h3>${esc(p.name)}</h3>
            <p class="muted">${esc([p.denomination, p.paid, p.network, p.status].filter(Boolean).join(" · "))}
              ${p.transactionUrl ? ` · <a href="${esc(p.transactionUrl)}" target="_blank" rel="noopener">transaction</a>` : ""}</p>
            ${state.revealed[p.id] ? `<pre class="result">${esc(state.revealed[p.id])}</pre>` : ""}
          </div>
          ${p.canReveal && !state.revealed[p.id] ? `<button class="btn small" data-action="reveal" data-id="${esc(p.id)}">Show code once</button>` : ""}
        </li>`).join("")}</ul>` : `<p>${state.purchases ? "No purchases yet." : "Loading…"}</p>`}
    </div>`;
}

function render() {
  renderAccount();
  const signedIn = Boolean(state.session);
  tabsEl.hidden = !signedIn;
  for (const tab of tabsEl.querySelectorAll(".tab")) tab.classList.toggle("active", tab.dataset.tab === state.tab);

  if (state.busy) {
    view.innerHTML = `<div class="card"><p class="row"><span class="spinner"></span> ${esc(state.busy)}</p></div>`;
    return;
  }
  if (!signedIn) {
    view.innerHTML = renderSignIn();
    return;
  }
  if (state.pickWallet) {
    view.innerHTML = `<div class="card stack"><h2>Connect your wallet</h2>
      <p>To sign, connect the wallet for ${esc(short(state.session.address))}.</p>${walletChoices()}</div>`;
    return;
  }
  view.innerHTML = { allowance: renderAllowance, shop: renderShop, purchases: renderPurchases }[state.tab]();
}

// -- events --

const actions = {
  "connect": async (el) => {
    const choice = el.dataset.uuid === "walletconnect" ? "walletconnect" : wallets().find((w) => w.info.uuid === el.dataset.uuid);
    state.pickWallet = false;
    await connect(choice);
  },
  "pick-wallet": () => { state.pickWallet = true; render(); },
  "sign-in": signIn,
  "sign-out": signOut,
  "refresh": () => busy("Refreshing…", loadAllowance),
  "preset": (el) => {
    const [d, p, n] = el.dataset.preset.split(",");
    $("#daily").value = d; $("#per").value = p; $("#days").value = n;
  },
  "create-limiter": createLimiter,
  "grant": grant,
  "revoke": revoke,
  "ask-pause": () => { state.confirmPause = true; render(); },
  "cancel-pause": () => { state.confirmPause = false; render(); },
  "pause": pause,
  "link": linkTelegram,
  "unlink": unlinkTelegram,
  "quote-tool": (el) => quoteTool(el.dataset.id),
  "search-bitrefill": searchBitrefill,
  "quote-bitrefill": (el) => quoteBitrefill(el.dataset.slug),
  "buy": buy,
  "cancel-quote": () => { state.quote = null; render(); },
  "reveal": (el) => reveal(el.dataset.id),
};

document.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) {
    state.tab = tab.dataset.tab;
    state.result = null;
    render();
    return;
  }
  const el = event.target.closest("[data-action]");
  if (el && actions[el.dataset.action] && !state.busy) actions[el.dataset.action](el);
});

// -- start --

async function start() {
  discover(render);
  if (csrf()) {
    try {
      state.session = await api.session();
      await loadAllowance();
    } catch {
      state.session = null;
    }
  }
  render();
}

start();
