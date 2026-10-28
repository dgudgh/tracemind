import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const page = readFileSync(path.join(root, "src/CmdbAssetPage.jsx"), "utf8");
const state = readFileSync(path.join(root, "src/AssetConnectionState.js"), "utf8");

test("failed live discovery keeps known topology while marking current validation failure", () => {
  assert.match(state, /\["failed", "partial"\]\.includes\(attributes\.discovery_status\)/);
  assert.match(page, /PostgreSQL 拒绝采集机连接/);
  assert.match(page, /实际生效的 pg_hba\.conf/);
  assert.match(page, /<DiscoveredArchitecture item=\{row\} \/>/);
  assert.match(page, /<DiscoveredRole item=\{row\} \/>/);
  assert.match(page, /<DiscoveredArchitecture item=\{cluster\} \/>/);
  assert.match(page, /key === "architecture" \? <DiscoveredArchitecture item=\{detail\} \/>/);
  assert.match(page, /key === "role" \? <DiscoveredRole item=\{detail\} \/>/);
  assert.match(page, /已保留上次识别的架构/);
  assert.match(page, /const isKnownPrimary = \(item\) => PRIMARY_ROLES\.has/);
  assert.match(page, /const isMembership = edge\.relation_type === "member_of"/);
  assert.match(page, /const relationLabel = isMembership \? "集群成员" : "复制（已存关系）"/);
  assert.match(page, /\$\{relationLabel\}（待验证）/);
  assert.match(page, /架构已识别，\$\{failedMembers\.length \|\| 1\} 个节点当前连接验证失败/);
  assert.match(page, /集群架构尚未采集/);
  assert.match(page, /result\.warning_count/);
});

test("successful connection test is distinct from topology refresh", () => {
  assert.match(page, /successfulConnectionAfterDiscovery/);
  assert.match(page, /failedConnectionAfterDiscovery/);
  assert.match(page, /connectionTestIsCurrent/);
  assert.match(page, /连接已恢复，拓扑等待刷新/);
  assert.match(page, /当前连接测试失败/);
  assert.match(page, /本次只验证连接，不会改写拓扑/);
  assert.match(page, /点击“自动识别”后才会刷新架构和节点角色/);
  assert.match(page, /setDetail\(await getNormalizedAssetInstance\(row\.id\)\)/);
  assert.match(page, /applyData\(await fetchAll\(\)\)/);
});

test("failed connection responses refresh the recorded asset state", () => {
  assert.match(page, /const refreshConnectionState = async \(\) =>/);
  assert.match(
    page,
    /catch \(error\) \{\s+await refreshConnectionState\(\);\s+Modal\.error\(\{ title: "数据库连接测试失败"/,
  );
});

test("topology selector excludes stale automatic clusters without members", () => {
  assert.match(page, /clusters\.filter\(\(item\) => item\.instance_count > 0 \|\| item\.discovery_source === "manual"\)/);
  assert.match(page, /options=\{topologyClusters\.map/);
  assert.match(page, /暂无包含实例的集群/);
});
