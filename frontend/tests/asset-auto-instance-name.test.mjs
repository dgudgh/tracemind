import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const page = readFileSync(new URL("../src/CmdbAssetPage.jsx", import.meta.url), "utf8");

test("automatic unmanaged instances display the host name until manually named", () => {
  assert.match(page, /const instanceDisplayName/);
  assert.match(page, /native_type === "discovered_database_instance"/);
  assert.match(page, /!row\.name_customized/);
  assert.match(page, /row\.host_name \|\| row\.host_ip \|\| row\.name/);
});
