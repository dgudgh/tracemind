import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import * as antd from "antd";
import * as icons from "@ant-design/icons";
import { parse } from "@babel/parser";
import { transformWithOxc } from "vite";
import * as state from "../src/InspectionExecutionState.js";

const source = readFileSync(new URL("../src/InspectionReportDetailPage.jsx", import.meta.url), "utf8");
const ast = parse(source, { sourceType: "module", plugins: ["jsx"] });
const script = ast.program.body.filter((node) => node.type !== "ImportDeclaration")
  .map((node) => {
    const declaration = node.declaration || node;
    return source.slice(declaration.start, declaration.end);
  }).join("\n");
// Transform only in memory: no server, bundling, or production build writes.
const transformed = await transformWithOxc(script, "InspectionReportDetailPage.jsx", { jsx: { runtime: "classic" } });
const components = vm.runInNewContext(`${transformed.code}; ({ InspectionReportDetailPage, EvidenceBlock, EvidenceExplorer, CategoryTable });`, {
  React, useState: React.useState, ...antd, ...icons, ...state,
  ItsmTicketDrawer: () => null,
  stableRecordKey: (prefix, row) => `${prefix}-${row.id || row.check_item || "item"}`,
  stableValueKey: (prefix, value) => `${prefix}-${value}`,
});
const render = (name, props) => renderToStaticMarkup(React.createElement(components[name], props));
const text = (html) => html.replace(/<[^>]*>/g, "");

test("actual report render never shows stale 100 or healthy empty conclusions for incomplete results", () => {
  for (const status of ["failed", "partial", "running", undefined]) {
    const html = render("InspectionReportDetailPage", { detail: {
      task: { task_status: status, overall_score: 100 },
      summary: { score: 100, risk_level: "normal", total: 0 },
      risk_items: [], all_findings: [], conclusion: "STALE_HEALTHY_CONCLUSION",
    } });
    assert.match(html, /未生成/);
    assert.doesNotMatch(html, /ant-statistic-content-value-int">100</);
    assert.doesNotMatch(text(html), /当前报告没有需要立即处置的问题|当前巡检未发现|STALE_HEALTHY_CONCLUSION/);
    assert.match(text(html), /不能判断是否存在风险/);
  }
});

test("SQL evidence table and modal agree that normal risk without execution is unconfirmed", () => {
  const item = { id: 1, check_item: "Missing evidence", severity: "normal", evidence: { sql: [null, " "] } };
  for (const html of [render("EvidenceBlock", { item }), render("EvidenceExplorer", { items: [item] })]) {
    assert.match(text(html), /未确认/);
    assert.doesNotMatch(text(html), /执行成功|查询完成，返回 0 行/);
  }
});

test("render distinguishes real zero rows, missing saved rows, non-SQL collection and reference SQL", () => {
  const display = (evidence) => text(render("EvidenceBlock", { item: { evidence } }));
  assert.match(display({ state: "collected", sql: "select 1", row_count: 0, rows: [] }), /执行成功.*查询完成，返回 0 行/);
  assert.match(display({ state: "collected", sql: "select 1", row_count: 5, rows: [] }), /返回 5 行，未保存可展示明细/);
  assert.match(display({ state: "collected", value: 1 }), /采集完成（无 SQL 记录）/);
  const legacy = display({ status: "normal", sql: "select 1", rows: [], evidence_source: "legacy_saved_metric" });
  assert.match(legacy, /参考 SQL（历史映射，未确认执行）/);
  assert.doesNotMatch(legacy, /执行成功|查询完成，返回 0 行/);
});

test("incomplete category render suppresses stale progress and healthy conclusions", () => {
  const html = render("CategoryTable", { complete: false, items: [
    { module_key: "test", module_name: "Test", score: 100, risk_level: "normal", summary: "未发现明显风险" },
  ] });
  assert.match(text(html), /未生成.*未确认/);
  assert.doesNotMatch(html, /ant-progress/);
  assert.doesNotMatch(text(html), /未发现明显风险/);
});
