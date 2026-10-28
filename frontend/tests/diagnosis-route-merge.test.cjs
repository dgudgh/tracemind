const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const frontendRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(frontendRoot, "..");
const menuSource = fs.readFileSync(path.join(frontendRoot, "src", "menuConfig.jsx"), "utf8");
const routerSource = fs.readFileSync(path.join(frontendRoot, "src", "AppPageRouter.jsx"), "utf8");
const unifiedPageSource = fs.readFileSync(
  path.join(frontendRoot, "src", "UnifiedDiagnosisPage.jsx"),
  "utf8",
);
const diagnosisApiSource = fs.readFileSync(
  path.join(repoRoot, "backend", "app", "api", "v1", "diagnosis_center.py"),
  "utf8",
);
const moduleConfigSource = fs.readFileSync(
  path.join(repoRoot, "backend", "app", "api", "v1", "module_config.py"),
  "utf8",
);

test("diagnosis menu keeps one unified AI workspace inside the diagnosis group", () => {
  assert.match(
    menuSource,
    /key:\s*"diagnose"[^\n]*label:\s*"智能诊断"[^\n]*children/,
  );
  assert.match(menuSource, /key:\s*"diagnose\.smart"[^\n]*label:\s*"诊断工作台"/);
  assert.doesNotMatch(menuSource, /key:\s*"diagnose\.ai"/);
  assert.doesNotMatch(menuSource, /key:\s*"diagnose\.rule"/);
});

test("legacy AI and rule diagnosis links resolve to the unified workspace", () => {
  assert.match(menuSource, /"diagnose\.ai":\s*"diagnose\.smart"/);
  assert.match(menuSource, /"diagnose\.rule":\s*"diagnose\.smart"/);
  assert.match(menuSource, /\bai:\s*"diagnose\.smart"/);
  assert.match(menuSource, /\bruleDiagnosis:\s*"diagnose\.smart"/);
});

test("page router renders a single unified diagnosis page", () => {
  assert.doesNotMatch(routerSource, /\bAiPage\b/);
  assert.match(routerSource, /"diagnose\.smart":\s*\{\s*c:\s*UnifiedDiagnosisPage/);
  assert.doesNotMatch(routerSource, /"diagnose\.ai"/);
  assert.doesNotMatch(routerSource, /"diagnose\.rule"/);
});

test("unified diagnosis restores the cockpit and opens rule analysis in a centered modal", () => {
  assert.doesNotMatch(unifiedPageSource, /<Segmented/);
  assert.match(unifiedPageSource, /DiagnosisCenterPage/);
  assert.match(unifiedPageSource, /RuleDiagnosisPage/);
  assert.match(unifiedPageSource, /<Modal/);
  assert.match(unifiedPageSource, /\bcentered\b/);
  assert.doesNotMatch(unifiedPageSource, /<Drawer/);
  assert.match(unifiedPageSource, /onOpenRuleDiagnosis/);
  assert.match(unifiedPageSource, /规则分析/);
});

test("AI intent catalog advertises only the unified workspace", () => {
  assert.match(diagnosisApiSource, /"key":\s*"diagnose\.smart",\s*"name":\s*"智能诊断"/);
  assert.doesNotMatch(diagnosisApiSource, /"key":\s*"diagnose\.ai"/);
  assert.doesNotMatch(diagnosisApiSource, /"key":\s*"diagnose\.rule"/);
});

test("module management exposes one diagnosis workspace permission", () => {
  assert.match(moduleConfigSource, /"module_key":\s*"diagnose\.smart",\s*"module_name":\s*"智能诊断"/);
  assert.doesNotMatch(moduleConfigSource, /"module_key":\s*"diagnose\.ai"/);
  assert.doesNotMatch(moduleConfigSource, /"module_key":\s*"diagnose\.rule"/);
  assert.match(moduleConfigSource, /"diagnose\.rule":\s*"diagnose\.smart"/);
});
