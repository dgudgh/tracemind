const assert = require("node:assert/strict");
const test = require("node:test");

const { buildMenuContract, compareContracts } = require("../../scripts/check_menu_contract.cjs");

test("menu contract contains a unique default page and the current asset-first structure", () => {
  const contract = buildMenuContract();
  const keys = contract.entries.map((entry) => entry.key);

  assert.equal(contract.default_active_key, "diagnose.smart");
  assert.equal(contract.entries[0].key, "asset.center");
  assert.equal(new Set(keys).size, keys.length);
  assert.ok(keys.includes("diagnose.snapshot.agents"));
});

test("menu contract comparison reports structural drift", () => {
  const baseline = {
    default_active_key: "a",
    entries: [{ key: "a", label: "A", parent_key: null }],
    legacy_key_map: {},
  };
  const current = {
    default_active_key: "b",
    entries: [{ key: "b", label: "B", parent_key: null }],
    legacy_key_map: { old: "b" },
  };

  const diff = compareContracts(baseline, current);

  assert.deepEqual(diff.added_keys, ["b"]);
  assert.deepEqual(diff.removed_keys, ["a"]);
  assert.equal(diff.default_changed, true);
  assert.equal(diff.legacy_map_changed, true);
});
