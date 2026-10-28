import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const apiSource = readFileSync(new URL("../src/api.js", import.meta.url), "utf8");

test("global license failures stop the authenticated request storm", () => {
  assert.match(apiSource, /SESSION_BLOCKING_LICENSE_REASONS/);
  assert.match(apiSource, /machine_mismatch/);
  assert.match(apiSource, /clearAuthToken\(\);\s*emitAuthExpired\(\);/s);
});
