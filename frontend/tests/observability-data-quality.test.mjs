import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const pageSource = await readFile(new URL("../src/ObservabilityCenterSections.jsx", import.meta.url), "utf8");
const shellSource = await readFile(new URL("../src/ObservabilityCenterPage.jsx", import.meta.url), "utf8");
const apiSource = await readFile(new URL("../src/modules/observabilityApi.js", import.meta.url), "utf8");

test("observability data quality exposes explicit evaluation policy history and retention", () => {
  assert.match(pageSource, /export function DataQualityPage/);
  assert.match(pageSource, /const \{ Text, Title \} = Typography/);
  assert.match(pageSource, /立即评估/);
  assert.match(pageSource, /完整率/);
  assert.match(pageSource, /时钟偏差/);
  assert.match(pageSource, /指标基数预算/);
  assert.match(pageSource, /DELETE_EXPIRED_OBSERVABILITY/);
  assert.match(apiSource, /data-quality\/evaluate/);
  assert.match(apiSource, /data-quality\/policies/);
  assert.match(apiSource, /data-quality\/history/);
  assert.match(apiSource, /data-quality\/retention/);
});

test("observability header derives health from evaluated data instead of a fixed success label", () => {
  assert.match(shellSource, /QUALITY_HEADER_META/);
  assert.match(shellSource, /dataQuality\?\.overall_status/);
  assert.doesNotMatch(shellSource, /采集链路运行中/);
});
