import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const page = readFileSync(path.join(root, "src/CmdbAssetPage.jsx"), "utf8");
const styles = readFileSync(path.join(root, "src/styles.css"), "utf8");

test("asset details open in a centered modal instead of a side drawer", () => {
  assert.match(page, /className="asset-detail-modal"/);
  assert.match(page, /centered/);
  assert.match(page, /footer=\{null\}/);
  assert.doesNotMatch(page, /<Drawer/);
});

test("asset detail modal keeps actions visible and content scrollable", () => {
  assert.match(page, /className="asset-detail-modal-actions"/);
  assert.match(page, />测试<\/Button>/);
  assert.match(page, />编辑连接<\/Button>/);
  assert.match(page, />人工校正<\/Button>/);
  assert.match(page, />删除<\/Button>/);
  assert.match(styles, /\.asset-detail-modal \.ant-modal-body\s*\{[\s\S]*?max-height:[\s\S]*?overflow-y: auto;/);
});
