// website/llms.txt (llmstxt.org format) must exist at the publish root, start with a top-level heading, and point
// at SingIt Ask's own llms.txt and OpenAPI document so an agent can follow it from the landing to the API.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const website = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("website/llms.txt starts with a heading and links to ask.singitai.app", async () => {
  const text = await readFile(path.join(website, "llms.txt"), "utf8");
  assert.match(text, /^# /);
  assert.match(text, /https:\/\/ask\.singitai\.app\/llms\.txt/);
  assert.match(text, /https:\/\/ask\.singitai\.app\/openapi\.json/);
});
