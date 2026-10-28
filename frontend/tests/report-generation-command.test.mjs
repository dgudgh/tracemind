import assert from "node:assert/strict";
import test from "node:test";
import { isReportGenerationCommand } from "../src/modules/reportGeneration.js";

test("report generation prompt is one-click, report viewing is not", () => {
  assert.equal(isReportGenerationCommand("我需要生成统一运维报告和售后分析报告"), true);
  assert.equal(isReportGenerationCommand("生成售后分析报告"), true);
  assert.equal(isReportGenerationCommand("查看统一运维报告"), false);
  assert.equal(isReportGenerationCommand("打开报告中心"), false);
});
