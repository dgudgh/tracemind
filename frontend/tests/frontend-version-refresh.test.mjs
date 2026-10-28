import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/framework/frontendVersion.js", import.meta.url),
  "utf8"
);
const apiSource = readFileSync(
  new URL("../src/api.js", import.meta.url),
  "utf8"
);
const appRoot = readFileSync(
  new URL("../src/AppRoot.jsx", import.meta.url),
  "utf8"
);

test("the running app checks for a newer entry bundle without using cache", () => {
  assert.ok(apiSource.includes('cache: "no-store"'));
  assert.ok(source.includes("fetchFrontendHtml()"));
  assert.ok(source.includes("latestAsset === currentAsset"));
  assert.ok(source.includes("reloadPage()"));
  assert.ok(source.includes("visibilitychange"));
  assert.ok(source.includes('window.addEventListener("focus"'));
  assert.ok(appRoot.includes("watchFrontendVersion()"));
});
