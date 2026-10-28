import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const page = readFileSync(path.join(root, "src/CmdbAssetPage.jsx"), "utf8");
const styles = readFileSync(path.join(root, "src/styles.css"), "utf8");

test("host database tags stay inside the hosted-instance column", () => {
  assert.match(page, /<HostedInstanceSummary count=\{value\} dbTypes=\{row\.db_types\} \/>/);
  assert.match(page, /title: "承载实例",[\s\S]*?width: 190/);
  assert.match(styles, /\.asset-host-instance-types\s*\{[\s\S]*?flex-wrap: wrap;/);
  assert.match(styles, /\.asset-host-instance-types \.ant-tag\s*\{[\s\S]*?margin: 0;/);
});
