const { existsSync } = require("node:fs");

const DEFAULT_FRONTEND_URL = process.env.FRONTEND_URL || "http://127.0.0.1:3300";
const USERNAME = process.env.MESGUARD_TEST_USER || "admin";
const PASSWORD = process.env.MESGUARD_TEST_PASSWORD || "";

function loadPlaywright() {
  try {
    return require("playwright");
  } catch (error) {
    throw new Error("playwright is required. Set NODE_PATH to bundled node_modules. Original error: " + error.message);
  }
}

function chromeExecutablePath() {
  return [
    process.env.CHROME_EXECUTABLE,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  ].filter(Boolean).find((candidate) => existsSync(candidate));
}

async function loginIfNeeded(page) {
  await page.waitForFunction(
    () => Boolean(document.querySelector("#username") || document.querySelector(".admin-shell")),
    null,
    { timeout: 20000 },
  );
  if (!(await page.locator("#username").count())) return;
  if (!PASSWORD) throw new Error("MESGUARD_TEST_PASSWORD is required when the login page is shown");
  await page.locator("#username").fill(USERNAME);
  await page.locator("#password").fill(PASSWORD);
  await page.getByRole("button", { name: "打开诊断工作台" }).click();
}

async function openSmartCommand(page, command) {
  await page.locator(".admin-smart-command input").fill(command);
  await page.locator(".admin-smart-command input").press("Enter");
  await page.waitForTimeout(1200);
  await page.waitForFunction(() => Boolean(document.querySelector(".admin-content")), null, { timeout: 15000 });
}

async function expectVisibleText(page, text, failures) {
  const count = await page.getByText(text, { exact: false }).count();
  if (!count) failures.push(`页面缺少文案：${text}`);
}

async function isVisibleText(page, text) {
  const locator = page.getByText(text, { exact: false });
  const count = await locator.count();
  for (let index = 0; index < count; index += 1) {
    if (await locator.nth(index).isVisible()) return true;
  }
  return false;
}

async function expectHiddenText(page, text, failures, message) {
  if (await isVisibleText(page, text)) failures.push(message || `页面不应默认展开文案：${text}`);
}

async function openFieldHelp(page, failures) {
  const trigger = page.locator(".inspection-field-help-trigger").first();
  if (!(await trigger.count())) {
    failures.push("页面缺少字段说明弹出入口");
    return;
  }
  await trigger.click();
  await page.waitForFunction(
    () => document.body.innerText.includes("模板在系统和接口里的唯一编码"),
    null,
    { timeout: 8000 },
  );
  await expectVisibleText(page, "字段", failures);
  await expectVisibleText(page, "说明", failures);
  await expectVisibleText(page, "技术标识", failures);
  await expectVisibleText(page, "模板在系统和接口里的唯一编码", failures);
  await page.locator(".ant-modal-root .ant-modal-close").last().click();
}

async function validateTemplateCardDesign(page, failures) {
  const cards = page.locator(".inspection-template-card");
  const count = await cards.count();
  if (count < 4) {
    failures.push(`模板卡片应使用统一 inspection-template-card 样式，当前识别到 ${count} 张`);
    return;
  }
  const first = cards.first();
  if ((await first.locator(".ant-descriptions").count()) > 0) {
    failures.push("模板卡片内部不应再使用 Descriptions 堆叠字段");
  }
  if ((await first.locator(".inspection-template-card-metric").count()) < 4) {
    failures.push("模板卡片底部应展示至少 4 个紧凑指标块");
  }
  for (const selector of [
    ".inspection-template-card-title",
    ".inspection-template-card-summary",
    ".inspection-template-card-dbs",
    ".inspection-template-card-actions",
  ]) {
    if (!(await first.locator(selector).count())) {
      failures.push(`模板卡片缺少统一结构：${selector}`);
    }
  }
}

async function fetchPolicies(page) {
  return page.evaluate(async () => {
    const token = localStorage.getItem("mesguard_token");
    const response = await fetch("/api/v1/inspection/policies", {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) throw new Error(`list policies failed: ${response.status}`);
    return response.json();
  });
}

async function updatePolicyReportConfig(page, policyId) {
  return page.evaluate(async (id) => {
    const token = localStorage.getItem("mesguard_token");
    const headers = {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    };
    const response = await fetch(`/api/v1/inspection/policies/${id}`, {
      method: "PUT",
      headers,
      body: JSON.stringify({
        instance_ids: [],
        report_config: {
          include_summary: false,
          include_findings: true,
          include_actions: false,
          include_raw_data: true,
          merge_probe: "updated",
        },
      }),
    });
    if (!response.ok) throw new Error(`update policy failed: ${response.status}`);
    return response.json();
  }, policyId);
}

async function closeWithTimeout(target) {
  try {
    await Promise.race([
      target.close(),
      new Promise((resolve) => setTimeout(resolve, 3000)),
    ]);
  } catch {
    // Best-effort cleanup; keep the original test result visible.
  }
}

function validateDefaultMetricTemplates(policyData, failures) {
  const items = policyData.items || [];
  const expected = [
    ["oracle_standard", "Oracle 默认巡检模板", "oracle", 20, ["AWR 快照覆盖", "归档日志生成速率", "无效对象数量"]],
    ["mysql_standard", "MySQL 默认巡检模板", "mysql", 20, ["实时采样指标", "auto_increment 使用率", "专家级调优指标"]],
    ["postgresql_standard", "PostgreSQL 默认巡检模板", "postgresql", 13, ["表统计", "索引使用分析", "等待事件"]],
    ["sqlserver_standard", "SQL Server 默认巡检模板", "sqlserver", 17, ["等待统计 TOP 10", "死锁信息", "文件组信息"]],
  ];
  for (const [policyKey, name, dbType, minItems, requiredMetricNames] of expected) {
    const policy = items.find((row) => row.policy_key === policyKey);
    if (!policy) {
      failures.push(`缺少数据库专属默认模板：${name}`);
      continue;
    }
    if (!policy.read_only) failures.push(`${name} 应为内置只读模板`);
    if (!policy.db_types?.includes(dbType)) failures.push(`${name} 适用数据库未包含 ${dbType}`);
    if (!Array.isArray(policy.item_config) || policy.item_config.length < minItems) {
      failures.push(`${name} 至少应包含 ${minItems} 个默认指标，当前 ${policy.item_config?.length || 0} 个`);
      continue;
    }
    for (const metricName of requiredMetricNames) {
      if (!policy.item_config.some((metric) => metric.name === metricName)) {
        failures.push(`${name} 缺少关键巡检指标：${metricName}`);
      }
    }
    const incomplete = policy.item_config.filter((metric) =>
      !metric.normal_range || !metric.warning_range || !metric.critical_range ||
      !metric.unit || !metric.collect_method || !metric.advice || !metric.description,
    );
    if (incomplete.length) {
      failures.push(`${name} 存在缺少正常/警告/严重范围、单位、采集方式、处理建议或说明的指标：${incomplete.map((item) => item.key).join(", ")}`);
    }
  }
}

async function deletePolicyByName(page, name) {
  const policy = await page.evaluate(async (policyName) => {
    const token = localStorage.getItem("mesguard_token");
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    const response = await fetch("/api/v1/inspection/policies", { headers });
    if (!response.ok) return null;
    const data = await response.json();
    const item = (data.items || []).find((row) => row.name === policyName);
    if (!item) return null;
    const deleted = await fetch(`/api/v1/inspection/policies/${item.id}`, { method: "DELETE", headers });
    return { id: item.id, ok: deleted.ok, status: deleted.status };
  }, name);
  return policy;
}

async function run() {
  const baseUrl = (process.argv.includes("--base-url") ? process.argv[process.argv.indexOf("--base-url") + 1] : DEFAULT_FRONTEND_URL).replace(/\/$/, "");
  const { chromium } = loadPlaywright();
  const executablePath = chromeExecutablePath();
  const browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  const failures = [];
  let createdTemplateName = "";
  try {
    const page = await browser.newPage({ viewport: { width: 1360, height: 860 } });
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await loginIfNeeded(page);
    await page.waitForFunction(() => Boolean(document.querySelector(".admin-shell")), null, { timeout: 20000 });
    await openSmartCommand(page, "打开 巡检模板");

    const policyDataBeforeEdit = await fetchPolicies(page);
    validateDefaultMetricTemplates(policyDataBeforeEdit, failures);
    for (const text of ["Oracle 默认巡检模板", "MySQL 默认巡检模板", "PostgreSQL 默认巡检模板", "SQL Server 默认巡检模板"]) {
      await expectVisibleText(page, text, failures);
    }

    await expectVisibleText(page, "字段说明", failures);
    await expectHiddenText(page, "模板在系统和接口里的唯一编码", failures, "字段说明内容不应默认直接显示");
    await openFieldHelp(page, failures);
    await expectVisibleText(page, "新增自定义模板", failures);
    await validateTemplateCardDesign(page, failures);

    const oracleCard = page.locator(".inspection-config-card").filter({ hasText: "Oracle 默认巡检模板" }).first();
    if (await oracleCard.count()) {
      await oracleCard.getByRole("button", { name: /查\s*看/ }).click();
      await expectVisibleText(page, "正常范围", failures);
      await expectVisibleText(page, "警告范围", failures);
      await expectVisibleText(page, "严重范围", failures);
      await expectVisibleText(page, "单位", failures);
      await expectVisibleText(page, "采集方式", failures);
      await expectVisibleText(page, "采集 SQL", failures);
      await expectVisibleText(page, "处理建议", failures);
      await expectVisibleText(page, "表空间使用率", failures);
      await expectVisibleText(page, "AWR 快照覆盖", failures);
      await page.locator(".ant-drawer .ant-drawer-close").last().click();
      await page.waitForFunction(() => !document.querySelector(".ant-drawer-open"), null, { timeout: 8000 });
    }

    const firstMore = page.getByRole("button", { name: /更多/ }).first();
    if (await firstMore.count()) {
      await firstMore.click();
      await expectVisibleText(page, "删除模板", failures);
      await page.keyboard.press("Escape");
    } else {
      failures.push("模板卡片缺少更多菜单");
    }

    await page.getByRole("button", { name: "新增自定义模板" }).click();
    await expectVisibleText(page, "检查项设置", failures);
    await expectHiddenText(page, "模板在系统和接口里的唯一编码", failures, "编辑弹窗字段说明内容不应默认直接显示");
    await expectVisibleText(page, "新增检查项", failures);
    await expectVisibleText(page, "从指标库添加", failures);
    await expectVisibleText(page, "自定义 SQL 指标", failures);
    await expectVisibleText(page, "正常范围", failures);
    await expectVisibleText(page, "采集 SQL", failures);
    await expectVisibleText(page, "阈值设置", failures);
    await expectVisibleText(page, "报告内容", failures);

    createdTemplateName = `巡检模板功能测试_${Date.now()}`;
    const itemRows = page.locator(".inspection-item-editor-row");
    const initialItemRowCount = await itemRows.count();
    await page.getByRole("button", { name: "新增检查项" }).click();
    const afterAddCount = await itemRows.count();
    if (afterAddCount !== initialItemRowCount + 1) {
      failures.push(`新增检查项后行数不正确：${initialItemRowCount} -> ${afterAddCount}`);
    }
    await itemRows.last().getByRole("button", { name: "删除" }).click();
    const afterDeleteCount = await itemRows.count();
    if (afterDeleteCount !== initialItemRowCount) {
      failures.push(`删除检查项后行数不正确：${afterAddCount} -> ${afterDeleteCount}`);
    }

    await page.getByLabel("模板名称").fill(createdTemplateName);
    const saveStart = Date.now();
    await page.locator(".ant-modal-root .ant-modal-footer .ant-btn-primary").click();
    await page.waitForFunction((name) => document.body.innerText.includes(name), createdTemplateName, { timeout: 12000 });
    const saveMs = Date.now() - saveStart;
    if (saveMs > 5000) failures.push(`新增模板保存耗时过长：${saveMs}ms`);

    const createdPolicyData = await fetchPolicies(page);
    const createdPolicy = (createdPolicyData.items || []).find((row) => row.name === createdTemplateName);
    if (!createdPolicy) {
      failures.push("新增模板保存后接口未返回该模板");
    } else {
      if (createdPolicy.read_only) failures.push("新增模板应为自定义可编辑，而不是内置只读");
      if (!Array.isArray(createdPolicy.item_config) || createdPolicy.item_config.length !== 1) {
        failures.push(`新增模板检查项数量不正确：${createdPolicy.item_config?.length}`);
      }
      if (!createdPolicy.categories?.includes("basic")) failures.push("新增模板分类未正确保存 basic");
      if (!createdPolicy.action_config?.includes("export_report")) failures.push("新增模板动作未正确保存 export_report");
      if (createdPolicy.report_config?.include_summary !== true) failures.push("新增模板报告内容 include_summary 未正确保存");
      const updatedPolicy = await updatePolicyReportConfig(page, createdPolicy.id);
      if (updatedPolicy.report_config?.include_summary !== false) failures.push("更新模板报告内容 include_summary 未正确覆盖旧值");
      if (updatedPolicy.report_config?.include_raw_data !== true) failures.push("更新模板报告内容 include_raw_data 未正确保存");
      if (updatedPolicy.report_config?.merge_probe !== "updated") failures.push("更新模板报告内容高级字段未正确合并");
    }

    const createdCard = page.locator(".inspection-config-card").filter({ hasText: createdTemplateName }).first();
    await createdCard.getByRole("button", { name: /更多/ }).click();
    await page.locator(".ant-dropdown:not(.ant-dropdown-hidden)").last().getByText("删除模板", { exact: true }).click();
    await page.locator(".ant-modal-confirm .ant-btn-primary").last().click();
    await page.waitForFunction((name) => !document.body.innerText.includes(name), createdTemplateName, { timeout: 12000 });
    const afterDeletePolicyData = await fetchPolicies(page);
    if ((afterDeletePolicyData.items || []).some((row) => row.name === createdTemplateName)) {
      failures.push("删除模板后接口仍返回该模板");
    } else {
      createdTemplateName = "";
    }

    if (failures.length) {
      console.error(JSON.stringify({ ok: false, failures }, null, 2));
      process.exitCode = 1;
    } else {
      console.log(JSON.stringify({ ok: true }, null, 2));
    }
  } finally {
    if (createdTemplateName) {
      const cleanupPage = await browser.newPage({ viewport: { width: 1360, height: 860 } });
      try {
        await cleanupPage.goto(baseUrl, { waitUntil: "domcontentloaded" });
        await loginIfNeeded(cleanupPage);
        await deletePolicyByName(cleanupPage, createdTemplateName);
      } catch {
        // Best-effort cleanup; keep the original test failure visible.
      } finally {
        await closeWithTimeout(cleanupPage);
      }
    }
    await closeWithTimeout(browser);
  }
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
