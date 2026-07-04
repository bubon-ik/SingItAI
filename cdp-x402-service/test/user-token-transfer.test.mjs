import test from "node:test";
import assert from "node:assert/strict";

import { humanTokenAmountToAtomic } from "../src/user-token-transfer.mjs";

test("converts human token amounts to atomic units", () => {
  assert.equal(humanTokenAmountToAtomic("1", 18).toString(), "1000000000000000000");
  assert.equal(humanTokenAmountToAtomic("0.1", 6).toString(), "100000");
});
