import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "..");
const source = (relativePath) => {
  const filePath = path.join(frontendRoot, relativePath);
  return existsSync(filePath) ? readFileSync(filePath, "utf8") : "";
};

const pageContracts = [
  {
    page: "src/CmdbAssetPage.jsx",
    api: "src/modules/cmdbApi.js",
    tabs: ["总览", "集群拓扑", "实例清单", "主机清单", "发现与同步", "校正与审计"],
    shared: ["ReactFlow", "InstanceDrawer", "HealthState"],
    endpoints: ["/assets/overview", "/assets/tree", "/assets/clusters", "/assets/instances", "/assets/hosts", "/assets/discovery-batches", "/assets/corrections", "/assets/discovery/preview", "/assets/discovery/apply", "/cmdb/sources", "/cmdb/sync"],
  },
  {
    page: "src/AutomationCenterPage.jsx",
    api: "src/modules/automationApi.js",
    tabs: ["脚本方案", "审批任务", "执行记录", "回滚方案"],
    shared: ["PlanSafetyNotice", "IntegrationAuditTable"],
    endpoints: ["/automation/plans", "/automation/approvals", "/automation/runs"],
  },
  {
    page: "src/EnterpriseIdentityPage.jsx",
    api: "src/modules/identityApi.js",
    tabs: ["身份源", "连接测试", "组映射", "外部身份", "登录审计"],
    shared: ["IntegrationHealthStrip", "ConnectionTestResult", "IntegrationAuditTable"],
    endpoints: ["/identity/providers", "/identity/login-audit"],
  },
];

test("domain pages expose their operational tabs, complete states, and shared integration surfaces", () => {
  for (const contract of pageContracts) {
    const page = source(contract.page);
    const api = source(contract.api);

    assert.match(page, /<Tabs\b/, `${contract.page} must use Ant Design tabs`);
    assert.match(page, /\bEmpty\b/, `${contract.page} must render an empty state`);
    assert.match(page, /\bloadError\b/, `${contract.page} must render a load error state`);
    assert.doesNotMatch(page, /T-0(?:53|58|59)/, `${contract.page} must not expose delivery identifiers`);
    assert.doesNotMatch(page, /<Card\b/, `${contract.page} must not introduce card shells`);

    for (const tab of contract.tabs) {
      assert.match(page, new RegExp(`label: ["']${tab}["']`), `${contract.page} is missing tab ${tab}`);
    }
    for (const component of contract.shared) {
      assert.match(page, new RegExp(`\\b${component}\\b`), `${contract.page} must use ${component}`);
    }
    for (const endpoint of contract.endpoints) {
      assert.ok(api.includes(endpoint), `${contract.api} is missing ${endpoint}`);
    }
  }
});

test("domain API clients stay inside the existing api boundary", () => {
  for (const contract of pageContracts) {
    const api = source(contract.api);
    assert.match(api, /import \{ api(?:, buildQuery)? \} from "\.\.\/api";/, `${contract.api} must use the shared api client`);
    assert.doesNotMatch(api, /\bfetch\(/, `${contract.api} must not bypass the shared api client`);
  }
});

test("CMDB and automation pages expose guarded operational semantics", () => {
  const cmdbPage = source("src/CmdbAssetPage.jsx");
  const automationPage = source("src/AutomationCenterPage.jsx");
  const safetyNotice = source("src/framework/integration/PlanSafetyNotice.jsx");

  assert.match(cmdbPage, /authoritative_snapshot/);
  assert.match(cmdbPage, /权威全量快照/);
  assert.match(cmdbPage, /不会直接删除/);
  assert.match(cmdbPage, /人工校正/);
  assert.match(cmdbPage, /自动识别/);
  assert.match(cmdbPage, /集群架构拓扑/);
  assert.doesNotMatch(automationPage, /operatorId/);
  assert.match(automationPage, /当前登录会话识别/);
  assert.match(safetyNotice, /方案准备阶段/);
  assert.doesNotMatch(safetyNotice, /当前仅生成方案，不会执行外部变更/);
});

test("object storage remains reachable without a selected database instance", () => {
  const router = source("src/AppPageRouter.jsx");
  const governance = source("src/DataGovernancePage.jsx");

  assert.match(router, /capability === "governance"/);
  assert.match(router, /<DataGovernancePage/);
  assert.match(governance, /selectedInstanceId \? requestedSection : "objectStorage"/);
  assert.match(governance, /if \(!instanceId\)/);
});
