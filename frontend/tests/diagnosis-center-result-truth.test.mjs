import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse } from "@babel/parser";

const source = readFileSync(new URL("../src/DiagnosisCenterPage.jsx", import.meta.url), "utf8");
const ast = parse(source, { sourceType: "module", plugins: ["jsx"] });
const names = ["compactText", "shortTime", "statusLabel", "countEvidenceForLayer",
  "diagnosisExecutionPresentation", "buildFaultChain"];
const declarations = names.map((name) => {
  const node = ast.program.body.find((item) => item.type === "FunctionDeclaration" && item.id.name === name);
  assert.ok(node, `Production function ${name} exists`);
  return source.slice(node.start, node.end);
});
const presentation = new Function(`${declarations.join("\n")}
  return { diagnosisExecutionPresentation, buildFaultChain };`)();

test("incident header derives the executed diagnosis result without defaulting to plan_only", () => {
  assert.deepEqual(presentation.diagnosisExecutionPresentation({
    status: "root_caused", ai_result: { execution_status: "success", data_quality: "good" },
  }), { label: "诊断完成", color: "success" });
  assert.equal(presentation.diagnosisExecutionPresentation({ ai_result: { execution_status: "partial" } }).label, "部分完成");
  assert.equal(presentation.diagnosisExecutionPresentation({ ai_result: { status: "failed" } }).label, "失败");
  assert.equal(presentation.diagnosisExecutionPresentation({ status: "diagnosing", ai_result: {} }).label, "证据待补充");
  assert.match(source, /<Tag color=\{executionView\.color\}>\{executionView\.label\}<\/Tag>/);
});

test("fault chain renders only persisted topology and never invents placeholder infrastructure", () => {
  const empty = presentation.buildFaultChain({ detail: { topology: { nodes: [] }, root_cause_summary: "锁等待" },
    selectedInstance: { instance_name: "orders-db" }, evidenceRows: [{ source: "inspection" }], timelineRows: [] });
  assert.deepEqual(empty, []);

  const nodes = presentation.buildFaultChain({ detail: { topology: { nodes: [
    { node_key: "edge", node_type: "gateway", node_name: "真实网关", status: "normal", alert_count: 0 },
    { node_key: "db", node_type: "database", node_name: "orders-db", status: "critical", alert_count: 2, is_root_cause: true },
  ] } }, selectedInstance: { instance_name: "fallback-db" }, evidenceRows: [], timelineRows: [] });
  assert.equal(nodes.length, 2);
  assert.equal(nodes[0].asset, "真实网关");
  assert.equal(nodes[0].critical, false);
  assert.equal(nodes[1].critical, true);
  assert.equal(nodes[1].suspected, true);
  assert.doesNotMatch(JSON.stringify(nodes), /Nginx|submitOrder|Oracle \/ CoreDB|row lock wait/);
  assert.match(source, /暂无持久化传播链证据；需要采集并保存真实拓扑后才能展示传播关系/);
});
