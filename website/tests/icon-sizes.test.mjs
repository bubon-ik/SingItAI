// Square PNG icons for MCP catalogs (Glama, mcp.so, Cline): both sizes must stay what their name says,
// so a careless re-export doesn't silently ship a mismatched icon-400.png.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const icons = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "assets", "icons");

// PNG IHDR: 8-byte signature, then a 4-byte chunk length, "IHDR", then width and height as big-endian uint32.
function pngSize(buffer) {
  assert.equal(buffer.subarray(12, 16).toString("ascii"), "IHDR");
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}

for (const [file, size] of [["icon-400.png", 400], ["icon-512.png", 512]]) {
  test(`${file} is a square ${size}x${size} PNG`, async () => {
    const buffer = await readFile(path.join(icons, file));
    assert.deepEqual(pngSize(buffer), { width: size, height: size });
  });
}
