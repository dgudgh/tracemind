const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const menuConfigPath = path.resolve(__dirname, "../src/menuConfig.jsx");
const menuConfigSource = fs.readFileSync(menuConfigPath, "utf8");

test("asset management is the first product entry and uses one unified page", () => {
  const configStart = menuConfigSource.indexOf("export const MENU_CONFIG = [");
  const assetIndex = menuConfigSource.indexOf('key: "asset.center"', configStart);
  const dashboardIndex = menuConfigSource.indexOf('key: "home.dashboard"', configStart);
  const diagnosisIndex = menuConfigSource.indexOf('key: "diagnose.smart"', configStart);
  const monitoringIndex = menuConfigSource.indexOf('key: "monitor"', configStart);

  assert.ok(configStart >= 0, "MENU_CONFIG should be declared");
  assert.ok(assetIndex > configStart, "asset menu should exist");
  assert.ok(assetIndex < dashboardIndex, "asset management should be the first product entry");
  assert.ok(dashboardIndex < diagnosisIndex, "workbench should appear before smart diagnosis");
  assert.ok(assetIndex < monitoringIndex, "asset menu should appear before monitoring");
  assert.doesNotMatch(menuConfigSource.slice(configStart, monitoringIndex), /key: "asset"[^.]/, "asset management should not keep a redundant submenu shell");
});

test("smart diagnosis remains the default entry", () => {
  assert.match(
    menuConfigSource,
    /export const DEFAULT_OPEN_KEYS = \[\];/,
    "no menu group should be expanded by default",
  );
  assert.match(
    menuConfigSource,
    /export const DEFAULT_ACTIVE_KEY = "diagnose\.smart";/,
    "smart diagnosis should be the default page",
  );
});
