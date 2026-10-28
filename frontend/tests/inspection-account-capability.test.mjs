import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runPage = readFileSync(path.join(root, "src/InspectionPages.jsx"), "utf8");
const reportPage = readFileSync(path.join(root, "src/InspectionReportDetailPage.jsx"), "utf8");

test("inspection pages show the backend capability assessment", () => {
  for (const source of [runPage, reportPage]) {
    assert.match(source, /account_capability_label/);
    assert.doesNotMatch(source, /read_only\s*\?\s*"只读降级采集"\s*:\s*"完整巡检能力"/);
  }
});
