import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  formatReplicationLagSeconds,
  hasReplicationLagData,
  replicationLagSeconds,
  replicationLagTone,
} from "../src/ReplicationLag.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const page = readFileSync(path.join(root, "src/CmdbAssetPage.jsx"), "utf8");

test("replication lag keeps zero as a real collected value", () => {
  const item = {
    transport_lag_seconds: 0,
    apply_lag_seconds: 0,
  };

  assert.equal(replicationLagSeconds(item), 0);
  assert.equal(hasReplicationLagData(item), true);
  assert.equal(formatReplicationLagSeconds(replicationLagSeconds(item)), "0 秒");
  assert.equal(replicationLagTone(item), "green");
});

test("replication lag uses the slower transport or apply path", () => {
  const item = {
    attributes: {
      transport_lag_seconds: 3,
      apply_lag_seconds: 65,
    },
  };

  assert.equal(replicationLagSeconds(item), 65);
  assert.equal(formatReplicationLagSeconds(65), "1.1 分钟");
  assert.equal(replicationLagTone(item), "red");
});

test("asset center exposes replication lag in topology list and details", () => {
  assert.match(page, /复制链路/);
  assert.match(page, /title: "复制延迟"/);
  assert.match(page, /延迟 <ReplicationLagValue item=\{item\} compact/);
  assert.match(page, /Data Guard 延迟由备库节点计算/);
  assert.match(page, /replicationLagMetric\(item, "transport_lag_seconds"\)/);
  assert.match(page, /replicationLagMetric\(item, "apply_lag_seconds"\)/);
});
