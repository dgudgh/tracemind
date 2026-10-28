const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..");
const defaultBaselineFile = path.join(repoRoot, "contracts", "baselines", "frontend-menu-v1.json");

function loadMenuContract(baselineFile = defaultBaselineFile) {
  if (!fs.existsSync(baselineFile)) {
    throw new Error(
      `Menu contract baseline is missing: ${path.relative(repoRoot, baselineFile)}. ` +
        "Run node scripts/check_menu_contract.cjs --update after reviewing the menu change.",
    );
  }
  const contract = JSON.parse(fs.readFileSync(baselineFile, "utf8"));
  if (!Array.isArray(contract.entries)) {
    throw new Error(`Menu contract has no entries array: ${path.relative(repoRoot, baselineFile)}`);
  }
  return contract;
}

function supportedEngines(entry) {
  return [...new Set([...(entry.engines || []), ...(entry.db_types || [])])]
    .map((value) => String(value).toLowerCase())
    .filter(Boolean);
}

function loadAuditMenuPages(baselineFile = defaultBaselineFile) {
  return loadMenuContract(baselineFile).entries
    .filter((entry) => !entry.child_keys?.length && entry.data !== "opsCapability")
    .map((entry) => ({
      key: entry.key,
      label: entry.label,
      engines: supportedEngines(entry),
    }));
}

function loadOpsCapabilities(baselineFile = defaultBaselineFile) {
  return loadMenuContract(baselineFile).entries
    .filter((entry) => entry.data === "opsCapability")
    .map((entry) => [entry.key.replace(/^ops\./, ""), entry.label, supportedEngines(entry)]);
}

module.exports = {
  loadAuditMenuPages,
  loadMenuContract,
  loadOpsCapabilities,
};
