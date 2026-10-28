import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { reportEvidence } from "../src/reportEvidence.mjs";

const report = (evidence) => ({ content_json: { analysis_detail: { kind: "slow_sql_ai", evidence } }, evidence_manifest: [] });

test("embedded SQL, exact plan and zero-valued metrics count as three saved evidence items", () => {
  const input = report({ sql_text: "select 0 from dual", plan_text: "exact plan", plan_status: "collected", metrics: { executions: 0 }, child_number: 0 });
  const before = JSON.stringify(input);
  const result = reportEvidence(input);
  assert.equal(result.count, 3);
  assert.deepEqual(result.items.map((item) => item.key), ["sql", "plan", "metrics"]);
  assert.equal(result.source.child_number, 0);
  assert.equal(JSON.stringify(input), before);
});

test("missing or failed plans and empty metrics never inflate the evidence count", () => {
  for (const metrics of [{}, { executions: null }, [], null]) {
    const result = reportEvidence(report({ sql_text: "select 1", plan_text: "error", plan_status: "failed", metrics }));
    assert.equal(result.count, 1);
    assert.deepEqual(result.items.map((item) => item.key), ["sql"]);
  }
  assert.equal(reportEvidence(report({ sql_text: " ", plan_status: "collected", plan_text: " " })).count, 0);
});

test("legacy indexes remain visible without inventing embedded evidence", () => {
  const manifest = [{ key: "source_export", name: "source", uri: "/export" }];
  assert.equal(reportEvidence({ evidence_manifest: manifest }).count, 1);
  assert.equal(reportEvidence({ content_json: { analysis_detail: { kind: "other", evidence: { sql_text: "ignore" } } } }).count, 0);
  assert.equal(reportEvidence(null).count, 0);
  assert.equal(reportEvidence({ evidence_manifest: {} }).count, 0);
});

test("full SQL is retained as text, without parsing untrusted markup", () => {
  const sql = "<script>do_not_execute()</script>\n" + "x".repeat(5000);
  assert.equal(reportEvidence(report({ sql_text: sql })).items[0].text, sql);
  const component = readFileSync(new URL("../src/ReportEvidencePanel.jsx", import.meta.url), "utf8");
  assert.match(component, /\{item\.text\}/);
  assert.doesNotMatch(component, /dangerouslySetInnerHTML|fetch\(/);
});
