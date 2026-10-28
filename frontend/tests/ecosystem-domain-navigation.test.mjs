import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const read = (relativePath) => readFileSync(new URL(relativePath, import.meta.url), "utf8");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const menu = read("../src/menuConfig.jsx");
const router = read("../src/AppPageRouter.jsx");

test("completed ecosystem work lives only in owning product pages", () => {
  const directRoutes = [
    "asset.center",
    "integration.notify",
    "integration.itsm",
    "monitor.observability.logs",
    "monitor.observability.grafana",
    "monitor.observability.apm",
    "backup.platform",
    "ops.governance.objectStorage",
    "protection.automation",
    "system.identity",
  ];
  directRoutes.forEach((routeKey) => {
    assert.ok(menu.includes(`key: "${routeKey}"`), `${routeKey} must remain in its owning menu`);
    assert.ok(router.includes(`"${routeKey}"`), `${routeKey} must remain directly routed`);
  });

  assert.doesNotMatch(menu, /integration\.ecosystem|生态适配/);
  assert.doesNotMatch(router, /integration\.ecosystem|EcosystemIntegrationPage/);
  assert.equal(existsSync(path.join(root, "src/EcosystemIntegrationPage.jsx")), false);
  assert.equal(existsSync(path.join(root, "src/modules/ecosystemApi.js")), false);
  assert.equal(existsSync(path.join(root, "src/modules/ecosystemOwnership.js")), false);
});

test("customer-facing menu labels do not expose task codes", () => {
  assert.doesNotMatch(menu, /T-05[1-9]|T-060/);
});
