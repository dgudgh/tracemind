import assert from "node:assert/strict";
import test from "node:test";
import { syncedInstanceSelection } from "../src/modules/instanceSelection.js";

const instances = [{ id: 2, db_type: "oracle" }, { id: 7, db_type: "mysql" }];
test("customer requirement entry does not silently select the first database", () => {
  for (const empty of [undefined, null, 0, 999]) assert.equal(syncedInstanceSelection(empty, instances, false), undefined);
  assert.equal(syncedInstanceSelection(7, instances, false), 7);
  assert.equal(syncedInstanceSelection(null, instances, false), undefined);
  assert.equal(syncedInstanceSelection(7, instances.slice(0, 1), false), undefined);
  assert.equal(syncedInstanceSelection(2, [], false), undefined);
});
test("existing pages retain their default first-instance selection", () => {
  assert.equal(syncedInstanceSelection(undefined, instances), 2);
  assert.equal(syncedInstanceSelection(7, instances), 7);
  assert.equal(syncedInstanceSelection(undefined, []), undefined);
});
