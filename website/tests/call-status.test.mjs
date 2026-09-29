import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// Run the real card handler and renderer with a fake API; no browser wallet or payment.
const source = readFileSync(new URL("../app/main.js", import.meta.url), "utf8");
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from);
  return source.slice(from, to);
}
const functions = section("async function cardAction(", "const chatLink =")
  + section("function renderCard(", "// -- account menu");

function setup(act) {
  const state = { sending: false, done: {}, messages: [], chatId: "chat" };
  const frames = [];
  const context = vm.createContext({ state, api: { act }, startThinking() {}, stopThinking() {},
    loadAllowance: async () => {}, toast() {}, explain: String,
    render: () => frames.push(context.renderCard({ type: "call_draft" }, "call")),
  });
  vm.runInContext(functions, context);
  return { context, state, frames, press: () => context.cardAction({ type: "start_call" }, "call") };
}

test("pending and refused requests never display Call started", async () => {
  let finish, calls = 0;
  const ui = setup(() => { calls++; return new Promise(resolve => { finish = resolve; }); });
  const waiting = ui.press();
  assert.match(ui.frames.at(-1), /Requesting call/);
  await ui.press();
  assert.equal(calls, 1);
  finish({ messages: [{ text: "The seller refused the request (HTTP 400)", cards: [] }] });
  await waiting;
  assert.match(ui.frames.at(-1), /Call not confirmed/);
  assert.ok(ui.frames.every(html => !html.includes("Call started")));
  await ui.press();
  assert.equal(calls, 1, "a consumed draft must not be submitted again");
});

test("only a returned call ID confirms the start", async () => {
  const ui = setup(async () => ({ messages: [{ cards: [{ type: "call", callId: "abc-123-def" }] }] }));
  await ui.press();
  assert.match(ui.frames.at(-1), /Call started ✓/);
});

test("a call card without a call ID is not success", async () => {
  const ui = setup(async () => ({ messages: [{ cards: [{ type: "call" }] }] }));
  await ui.press();
  assert.match(ui.frames.at(-1), /Call not confirmed/);
});

test("a lost response shows an unknown outcome without retrying", async () => {
  let calls = 0;
  const ui = setup(async () => { calls++; throw new Error("timeout"); });
  await ui.press();
  assert.match(ui.frames.at(-1), /Call status unknown/);
  await ui.press();
  assert.equal(calls, 1);
});

test("a busy chat does not mark an unsubmitted card as done", async () => {
  const ui = setup(async () => { throw new Error("must not send"); });
  ui.state.sending = true;
  await ui.press();
  assert.equal(ui.state.done.call, undefined);
});
