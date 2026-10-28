const { readFileSync } = require("node:fs");
const path = require("node:path");

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

const pagePath = path.resolve(__dirname, "../frontend/src/ObservabilityCenterPage.jsx");
const sectionsPath = path.resolve(__dirname, "../frontend/src/ObservabilityCenterSections.jsx");
const apiPath = path.resolve(__dirname, "../frontend/src/modules/observabilityApi.js");

const page = readFileSync(pagePath, "utf8");
const sections = readFileSync(sectionsPath, "utf8");
const api = readFileSync(apiPath, "utf8");

assert(page.includes("联动健康检查"), "可观测中心必须有“联动健康检查”页签");
assert(page.includes("LinkageHealthPage"), "可观测中心必须挂载 LinkageHealthPage");
assert(page.indexOf("联动健康检查") < page.indexOf("数据源管理"), "联动健康检查页签必须前置，避免在窄屏被隐藏");
assert(page.includes("更多"), "可观测中心页签溢出入口必须有可读的“更多”文字");
assert(sections.includes("export function LinkageHealthPage"), "必须实现联动健康检查页面组件");
assert(sections.includes("getObservabilityLinkageHealth"), "页面必须调用实例级联动健康接口");
assert(api.includes("getObservabilityLinkageHealth"), "API 模块必须导出 getObservabilityLinkageHealth");
assert(api.includes("/observability/linkage-health"), "API 模块必须调用 /observability/linkage-health");

console.log(JSON.stringify({ status: "passed", checks: 6 }, null, 2));
