import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = (name) => readFileSync(
  new URL(`../src/framework/integration/${name}.jsx`, import.meta.url),
  "utf8",
);

test("integration framework exposes the four shared operational surfaces", () => {
  assert.match(source("IntegrationHealthStrip"), /export function IntegrationHealthStrip/);
  assert.match(source("ConnectionTestResult"), /export function ConnectionTestResult/);
  assert.match(source("PlanSafetyNotice"), /export function PlanSafetyNotice/);
  assert.match(source("IntegrationAuditTable"), /export function IntegrationAuditTable/);
});

test("shared integration surfaces avoid nested card shells", () => {
  for (const name of [
    "IntegrationHealthStrip",
    "ConnectionTestResult",
    "PlanSafetyNotice",
    "IntegrationAuditTable",
  ]) {
    assert.doesNotMatch(source(name), /<Card\b/);
  }
});
