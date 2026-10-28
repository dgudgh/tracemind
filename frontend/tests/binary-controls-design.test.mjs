import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "..");
const sourceFiles = [
  "ZabbixIntegrationPage.jsx",
  "PrometheusIntegrationPage.jsx",
  "ObservabilityCenterSections.jsx",
  "DataGovernanceOverviewPanel.jsx",
  "WorkflowManagementPages.jsx",
  "InspectionPages.jsx",
  "SystemSettingsPages.jsx",
];

const sources = sourceFiles.map((file) => ({
  file,
  source: fs.readFileSync(path.join(frontendRoot, "src", file), "utf8"),
}));

test("binary configuration fields use switches instead of numeric or option controls", () => {
  for (const { file, source } of sources) {
    assert.doesNotMatch(
      source,
      /<Form\.Item(?=[^>]*name="(?:enabled|is_default)")[^>]*>\s*<(?:InputNumber|Select)\b/,
      `${file} still renders a binary field as a numeric or option control`,
    );
  }
});

test("shared binary switch preserves backend value types", () => {
  const source = fs.readFileSync(
    path.join(frontendRoot, "src", "framework", "BinaryStatusSwitch.jsx"),
    "utf8",
  );
  assert.match(source, /trueValue\s*=\s*1/);
  assert.match(source, /falseValue\s*=\s*0/);
  assert.match(source, /onChange\?\.\(nextChecked \? trueValue : falseValue\)/);
});
