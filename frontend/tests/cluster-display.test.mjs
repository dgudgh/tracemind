import assert from "node:assert/strict";
import test from "node:test";

import { formatClusterName, formatClusterOption } from "../src/ClusterDisplay.js";

test("removes generated architecture suffixes from cluster names", () => {
  assert.equal(formatClusterName("orcl 单机集群"), "orcl");
  assert.equal(formatClusterName("orch-mysql01 复制拓扑"), "orch-mysql01");
  assert.equal(formatClusterName("MES PostgreSQL 流复制"), "MES");
  assert.equal(formatClusterName("MES Data Guard"), "MES");
});

test("preserves explicit cluster names", () => {
  assert.equal(formatClusterName("mesdb"), "mesdb");
  assert.equal(formatClusterName("Oracle RAC"), "Oracle RAC");
  assert.equal(formatClusterName(""), "");
});

test("uses one database-name-architecture format for selector options", () => {
  assert.equal(
    formatClusterOption(
      { db_type: "MySQL", name: "orch-mysql01 复制拓扑", architecture: "primary_replica" },
      "主从复制",
    ),
    "MySQL · orch-mysql01 · 主从复制",
  );
  assert.equal(
    formatClusterOption(
      { db_type: "Oracle", name: "orcl 单机集群", architecture: "single" },
      "单机",
    ),
    "Oracle · orcl · 单机",
  );
});
