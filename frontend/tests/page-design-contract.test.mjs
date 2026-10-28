import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { MESGUARD_ADMIN_THEME } from "../src/framework/adminTheme.js";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "..");
const routerSource = fs.readFileSync(
  path.join(frontendRoot, "src", "AppPageRouter.jsx"),
  "utf8",
);
const stylesSource = fs.readFileSync(
  path.join(frontendRoot, "src", "styles.css"),
  "utf8",
);
const workflowSource = fs.readFileSync(
  path.join(frontendRoot, "src", "WorkflowManagementPages.jsx"),
  "utf8",
);
const adminShellSource = fs.readFileSync(
  path.join(frontendRoot, "src", "framework", "AdminShell.jsx"),
  "utf8",
);

test("every routed page is rendered inside the shared page frame", () => {
  assert.match(routerSource, /className="app-page-frame"/);
  assert.match(routerSource, /data-page-key=\{activeKey\}/);
  assert.match(routerSource, /<AppPageContent \{\.\.\.props\} onManualRequirementHandoff=\{receiveManualHandoff\} \/>/);
});

test("the admin theme uses a restrained operational radius scale", () => {
  assert.equal(MESGUARD_ADMIN_THEME.token.borderRadius, 8);
  assert.equal(MESGUARD_ADMIN_THEME.token.borderRadiusLG, 8);
  assert.equal(MESGUARD_ADMIN_THEME.components.Button.borderRadius, 7);
  assert.equal(MESGUARD_ADMIN_THEME.components.Card.borderRadiusLG, 8);
  assert.equal(MESGUARD_ADMIN_THEME.components.Menu.itemBorderRadius, 7);
  assert.equal(MESGUARD_ADMIN_THEME.components.Select.borderRadius, 7);
});

test("the shared page frame owns the common responsive surfaces", () => {
  const requiredSelectors = [
    ".app-page-frame",
    ".app-page-frame .ant-card",
    ".app-page-frame .ant-card .ant-card",
    ".app-page-frame .ant-tabs-nav-wrap",
    ".app-page-frame .ant-table-wrapper",
    ".app-page-frame .ant-statistic-content",
    ".app-page-frame .ant-col-xs-6",
    ".app-page-frame .ant-space:not(.ant-space-vertical):not(.ant-space-compact)",
    ".app-page-frame .workbench-hero",
    ".app-page-frame .inspection-kpi-card",
    ".app-page-frame .inspection-ranking-card",
  ];

  for (const selector of requiredSelectors) {
    assert.ok(
      stylesSource.includes(selector),
      `shared responsive design rule is missing: ${selector}`,
    );
  }

  assert.ok(
    stylesSource.lastIndexOf("Unified page design system")
      > stylesSource.lastIndexOf("Final override: quick entries removed"),
    "the shared design layer must stay after legacy page overrides",
  );
});

test("workflow pages do not add a second page gutter", () => {
  assert.doesNotMatch(workflowSource, /<div style=\{\{ padding: 16 \}\}>/);
  assert.equal(
    (workflowSource.match(/className="workflow-management-page"/g) || []).length,
    2,
  );
});

test("workflow actions and statuses use consistent Chinese labels", () => {
  assert.doesNotMatch(workflowSource, />\s*(?:Test|Edit|Del|Sync|Close|ON|OFF|Yes)\s*</);
  assert.doesNotMatch(workflowSource, /message\.(?:success|error)\("(?:OK|Failed|Closed)"/);
});

test("the AI diagnosis workspace keeps the original cockpit visual identity", () => {
  assert.match(
    stylesSource,
    /\.diagnosis-single-entry-card\s*\{[\s\S]*?border-radius:\s*32px;[\s\S]*?backdrop-filter:\s*blur\(26px\);/,
  );
  assert.match(
    stylesSource,
    /\.diagnosis-single-textarea\.ant-input\s*\{[\s\S]*?font-size:\s*28px;/,
  );
  assert.match(
    stylesSource,
    /\.app-page-frame:not\(\[data-page-key="diagnose\.smart"\]\) \[class\$="-card"\]/,
  );
  assert.doesNotMatch(stylesSource, /operational density without glass effects/);
  assert.doesNotMatch(stylesSource, /\.app-page-frame \.diagnosis-single-entry-card/);
});

test("compact navigation uses an overlay drawer and never squeezes page content", () => {
  assert.match(adminShellSource, /compactNavigation/);
  assert.match(adminShellSource, /mobileMenuOpen/);
  assert.match(adminShellSource, /window\.matchMedia\(COMPACT_NAVIGATION_QUERY\)/);
  assert.match(adminShellSource, /collapsedWidth=\{compactNavigation \? 0 : 76\}/);
  assert.match(adminShellSource, /open=\{mobileMenuOpen\}/);
  assert.match(adminShellSource, /setMobileMenuOpen\(false\)/);
});

test("route changes reset the shared content scroll position", () => {
  assert.match(adminShellSource, /useLayoutEffect/);
  assert.match(adminShellSource, /contentRef/);
  assert.match(adminShellSource, /contentRef\.current\?\.scrollTo\(\{ top: 0, left: 0, behavior: "auto" \}\)/);
  assert.match(adminShellSource, /\[activeKey\]/);
  assert.match(adminShellSource, /<Content ref=\{contentRef\} className="content admin-content">/);
});
