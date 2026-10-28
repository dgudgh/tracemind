import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "..");
const source = fs.readFileSync(
  path.join(frontendRoot, "src", "OpsLayout.jsx"),
  "utf8",
);

test("database operation pages use a flat context bar", () => {
  assert.match(source, /className="ops-context-bar"/);
  assert.match(source, /className="ops-center-tabs"/);
  assert.doesNotMatch(source, /<Tabs[^>]*type="card"/);
});

test("empty states do not create nested cards", () => {
  const emptyStateStart = source.indexOf("export function OpsEmptyState");
  const summaryStart = source.indexOf("export function CurrentInstanceSummary");
  const emptyStateSource = source.slice(emptyStateStart, summaryStart);
  assert.match(emptyStateSource, /className="ops-empty-state"/);
  assert.doesNotMatch(emptyStateSource, /<Card/);
});
