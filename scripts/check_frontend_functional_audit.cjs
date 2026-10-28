const { existsSync, mkdirSync, writeFileSync } = require("node:fs");
const path = require("node:path");
const { loadAuditMenuPages, loadOpsCapabilities } = require("./frontend_audit_pages.cjs");

const DEFAULT_FRONTEND_URL = process.env.FRONTEND_URL || process.env.FRONTEND_AUDIT_URL || "http://127.0.0.1:3300/";
const DEFAULT_BACKEND_URL = process.env.BACKEND_URL || "";
const USERNAME = process.env.MESGUARD_TEST_USER || "admin";
const PASSWORD = process.env.MESGUARD_TEST_PASSWORD || "";
const REPORT_DIR = process.env.FRONTEND_FUNCTIONAL_REPORT_DIR || path.resolve(__dirname, "../reports/frontend-functional-audit");

const PAGES = loadAuditMenuPages();
const OPS_CAPABILITIES = loadOpsCapabilities();

const SAFE_BUTTON_RE = /^(刷新|查询|搜索|重置|清空|清除|清除时间|查看|详情|预览|诊断|分析|本地分析|生成方案|展开|收起|复制|验证|校验|统计|应用筛选|清除筛选|测试|连通性诊断)$/i;
const RISKY_BUTTON_RE = /(删除|移除|保存|提交|确认|确定|启用|停用|关闭告警|关闭此告警|全部关闭|执行|运行|开始|停止|恢复|备份|审批|通过|拒绝|同步|导入|导出|上传|下载|Kill|kill|清理|归档|扩容|创建|新增|编辑|重置密码|退出|隔离|10天|15天|Close)$/i;
const EFFECT_EXPECTED_RE = /^(刷新|查询|搜索|查看|详情|预览|诊断|分析|本地分析|生成方案|验证|校验|统计|测试|连通性诊断)$/i;
const TEXT_ANOMALY_RE = /(\bundefined\b|\bNaN\b|Invalid Date|\[object Object\]|TypeError|ReferenceError|Cannot read properties|Cannot access|\u951f\u65a4\u62f7|\uFFFD|nullnull|null undefined|undefined undefined)/;
const PLACEHOLDER_RE = /^(--|-|暂无数据|暂无|无数据|无|N\/A)$/i;
const API_BODY_SAMPLE_LIMIT = Number.parseInt(process.env.FRONTEND_FUNCTIONAL_API_BODY_SAMPLE_LIMIT || "800", 10);
let eventBucketSeq = 0;

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

function parseArgs(argv) {
  const args = {
    baseUrl: DEFAULT_FRONTEND_URL.replace(/\/$/, ""),
    backendUrl: DEFAULT_BACKEND_URL.replace(/\/$/, ""),
    pageDelayMs: Number.parseInt(process.env.FRONTEND_FUNCTIONAL_PAGE_DELAY_MS || "1200", 10),
    actionDelayMs: Number.parseInt(process.env.FRONTEND_FUNCTIONAL_ACTION_DELAY_MS || "900", 10),
    maxButtonsPerPage: Number.parseInt(process.env.FRONTEND_FUNCTIONAL_MAX_BUTTONS || "10", 10),
    maxInputsPerPage: Number.parseInt(process.env.FRONTEND_FUNCTIONAL_MAX_INPUTS || "3", 10),
    slowApiMs: Number.parseInt(process.env.FRONTEND_FUNCTIONAL_SLOW_API_MS || "3000", 10),
    verySlowApiMs: Number.parseInt(process.env.FRONTEND_FUNCTIONAL_VERY_SLOW_API_MS || "8000", 10),
    slowPageMs: Number.parseInt(process.env.FRONTEND_FUNCTIONAL_SLOW_PAGE_MS || "6000", 10),
    slowActionMs: Number.parseInt(process.env.FRONTEND_FUNCTIONAL_SLOW_ACTION_MS || "5000", 10),
    headed: process.env.FRONTEND_FUNCTIONAL_HEADED === "1",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === "--base-url") {
      args.baseUrl = argv[index + 1].replace(/\/$/, "");
      index += 1;
    } else if (item === "--backend-url") {
      args.backendUrl = argv[index + 1].replace(/\/$/, "");
      index += 1;
    } else if (item === "--page-delay-ms") {
      args.pageDelayMs = Number.parseInt(argv[index + 1], 10);
      index += 1;
    } else if (item === "--max-buttons") {
      args.maxButtonsPerPage = Number.parseInt(argv[index + 1], 10);
      index += 1;
    } else if (item === "--slow-api-ms") {
      args.slowApiMs = Number.parseInt(argv[index + 1], 10);
      index += 1;
    } else if (item === "--very-slow-api-ms") {
      args.verySlowApiMs = Number.parseInt(argv[index + 1], 10);
      index += 1;
    } else if (item === "--slow-page-ms") {
      args.slowPageMs = Number.parseInt(argv[index + 1], 10);
      index += 1;
    } else if (item === "--slow-action-ms") {
      args.slowActionMs = Number.parseInt(argv[index + 1], 10);
      index += 1;
    } else if (item === "--headed") {
      args.headed = true;
    } else if (item === "-h" || item === "--help") {
      console.log("Usage: node scripts/check_frontend_functional_audit.cjs [--base-url URL] [--backend-url URL] [--max-buttons N] [--slow-api-ms N] [--slow-page-ms N] [--slow-action-ms N] [--headed]");
      process.exit(0);
    }
  }
  return args;
}

function timestamp() {
  return new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "Z");
}

function apiBase(args) {
  return `${args.backendUrl || args.baseUrl}/api/v1`;
}

async function apiJson(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!response.ok) {
    const detail = data?.detail || data?.message || text || `HTTP ${response.status}`;
    throw new Error(`${response.status} ${detail}`);
  }
  return data;
}

async function loginViaApi(args) {
  if (!PASSWORD) throw new Error("MESGUARD_TEST_PASSWORD is required for the functional audit");
  const data = await apiJson(`${apiBase(args)}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: USERNAME, password: PASSWORD }),
  });
  return data.token;
}

async function fetchInstances(args, token) {
  try {
    const data = await apiJson(`${apiBase(args)}/instances?page=1&page_size=100`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    return data.items || [];
  } catch {
    return [];
  }
}

async function loginIfNeeded(page) {
  await page.waitForFunction(
    () => Boolean(document.querySelector("#username") || document.querySelector(".admin-shell")),
    null,
    { timeout: 20000 },
  );
  if (!(await page.locator("#username").count())) return;
  await page.locator("#username").fill(USERNAME);
  await page.locator("#password").fill(PASSWORD);
  await page.getByRole("button", { name: "打开诊断工作台" }).click();
}

async function submitSmartCommand(page, command) {
  await page.locator(".admin-smart-command input").fill(command);
  await page.locator(".admin-smart-command input").press("Enter");
}

async function navigateToEntry(page, entry, args) {
  const command = `打开 ${entry.engine || ""} ${entry.key} ${entry.label}`.replace(/\s+/g, " ").trim();
  await submitSmartCommand(page, command);
  await page.waitForTimeout(args.pageDelayMs);
  await page.waitForFunction(() => Boolean(document.querySelector(".admin-shell .admin-content")), null, { timeout: 15000 });
}

function createEntries(instances) {
  const engineSet = new Set(instances.map((item) => String(item.db_type || "").toLowerCase()).filter(Boolean));
  const entries = PAGES.filter((entry) => !entry.engines?.length || !engineSet.size || entry.engines.some((engine) => engineSet.has(engine)));
  for (const [capability, label, engines] of OPS_CAPABILITIES) {
    const targets = engines.filter((engine) => engineSet.has(engine));
    for (const engine of targets.length ? targets : [""]) {
      entries.push({ key: `ops.${capability}`, label, engine });
    }
  }
  return entries;
}

function issue(entry, severity, type, message, detail = {}) {
  return {
    pageKey: entry.key,
    pageLabel: entry.label,
    engine: entry.engine || "",
    severity,
    type,
    message,
    detail,
  };
}

function newEventBucket() {
  eventBucketSeq += 1;
  return { id: eventBucketSeq, consoleErrors: [], pageErrors: [], apiCalls: [], requestFailures: [] };
}

function summarizeJsonPayload(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { type: Array.isArray(value) ? "array" : typeof value, itemCount: Array.isArray(value) ? value.length : undefined };
  }
  const keys = Object.keys(value).slice(0, 20);
  const summary = { type: "object", keys };
  for (const key of ["engine", "section", "status", "plan_type", "title", "message", "detail"]) {
    if (typeof value[key] === "string" || typeof value[key] === "number" || typeof value[key] === "boolean") {
      summary[key] = value[key];
    }
  }
  if (Array.isArray(value.items)) summary.itemCount = value.items.length;
  if (Array.isArray(value.warnings)) summary.warningCount = value.warnings.length;
  if (typeof value.script === "string") summary.scriptSample = value.script.slice(0, 260);
  if (typeof value.sql === "string") summary.sqlSample = value.sql.slice(0, 260);
  return summary;
}

function apiSemanticWarnings(call) {
  const warnings = [];
  const text = [call.bodySample, call.jsonSummary?.scriptSample, call.jsonSummary?.sqlSample].filter(Boolean).join("\n");
  if (text && TEXT_ANOMALY_RE.test(text)) {
    warnings.push("接口成功返回中包含异常文本");
  }
  if (!call.jsonSummary || call.status < 200 || call.status >= 400) return warnings;
  const summary = call.jsonSummary;
  if (summary.status === "unknown_section") {
    warnings.push("接口返回未知模块 section");
  }
  if (/未知方案类型/.test(summary.scriptSample || "")) {
    warnings.push("方案接口返回未知方案类型");
  }
  const planMatch = call.path.match(/\/api\/v1\/operations\/([^/]+)\/[^/]+\/plans\/([^/?#]+)/);
  if (planMatch) {
    const [, expectedEngine, expectedPlanType] = planMatch;
    if (summary.engine && String(summary.engine).toLowerCase() !== expectedEngine.toLowerCase()) {
      warnings.push(`方案引擎不匹配：期望 ${expectedEngine}，实际 ${summary.engine}`);
    }
    if (summary.plan_type && String(summary.plan_type) !== expectedPlanType) {
      warnings.push(`方案类型不匹配：期望 ${expectedPlanType}，实际 ${summary.plan_type}`);
    }
  }
  const sectionMatch = call.path.match(/\/api\/v1\/operations\/([^/]+)\/[^/]+\/([^/?#]+)$/);
  if (sectionMatch && !sectionMatch[2].startsWith("plans")) {
    const [, expectedEngine, expectedSection] = sectionMatch;
    if (summary.engine && String(summary.engine).toLowerCase() !== expectedEngine.toLowerCase()) {
      warnings.push(`模块引擎不匹配：期望 ${expectedEngine}，实际 ${summary.engine}`);
    }
    if (summary.section && String(summary.section) !== expectedSection) {
      warnings.push(`模块 section 不匹配：期望 ${expectedSection}，实际 ${summary.section}`);
    }
  }
  return warnings;
}

function summarizeCalls(calls) {
  return calls.map((call) => ({
    method: call.method,
    status: call.status,
    path: call.path,
    durationMs: call.durationMs,
    responseBytes: call.responseBytes,
    ok: call.status >= 200 && call.status < 400,
    bodySample: call.bodySample,
    jsonSummary: call.jsonSummary,
    semanticWarnings: apiSemanticWarnings(call),
  }));
}

function hashText(value) {
  let hash = 0;
  const text = String(value || "");
  for (let index = 0; index < text.length; index += 1) {
    hash = ((hash << 5) - hash + text.charCodeAt(index)) | 0;
  }
  return String(hash);
}

async function collectOutput(page) {
  return page.evaluate(({ textAnomalySource, placeholderSource }) => {
    const textAnomaly = new RegExp(textAnomalySource);
    const placeholder = new RegExp(placeholderSource, "i");
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
    };
    const textOf = (element) => (element?.innerText || element?.textContent || "").trim().replace(/\s+/g, " ");
    const content = document.querySelector(".admin-content");
    const contentText = textOf(content);
    const cards = [...document.querySelectorAll(".admin-content .ant-card")]
      .filter(visible)
      .map((card) => textOf(card).slice(0, 220))
      .filter(Boolean)
      .slice(0, 12);
    const tables = [...document.querySelectorAll(".admin-content .ant-table-wrapper")]
      .filter(visible)
      .map((table) => {
        const rows = table.querySelectorAll(".ant-table-tbody tr:not(.ant-table-measure-row)").length;
        const headers = [...table.querySelectorAll(".ant-table-thead th")]
          .map((cell) => textOf(cell))
          .filter(Boolean)
          .slice(0, 12);
        const empty = Boolean(table.querySelector(".ant-empty, .ant-table-placeholder"));
        const sample = textOf(table).slice(0, 260);
        return { rows, headers, empty, sample };
      })
      .slice(0, 8);
    const modals = [...document.querySelectorAll(".ant-modal, .ant-drawer")]
      .filter(visible)
      .map((modal) => textOf(modal).slice(0, 400))
      .filter(Boolean)
      .slice(0, 4);
    const messages = [...document.querySelectorAll(".ant-message-notice, .ant-notification-notice, .ant-alert")]
      .filter(visible)
      .map((message) => textOf(message).slice(0, 260))
      .filter(Boolean)
      .slice(0, 8);
    const stats = [...document.querySelectorAll(".ant-statistic")]
      .filter(visible)
      .map((stat) => textOf(stat))
      .filter((text) => text && !placeholder.test(text))
      .slice(0, 12);
    const anomalySamples = [contentText, ...cards, ...tables.map((table) => table.sample), ...modals, ...messages]
      .filter((text) => textAnomaly.test(text))
      .map((text) => text.slice(0, 260))
      .slice(0, 8);
    const enabledButtons = [...document.querySelectorAll(".admin-content button")]
      .filter(visible)
      .map((button, index) => ({
        index,
        text: (button.innerText || button.getAttribute("aria-label") || button.title || "").trim().replace(/\s+/g, " "),
        disabled: button.disabled || button.getAttribute("aria-disabled") === "true",
      }))
      .filter((button) => button.text);
    const inputs = [...document.querySelectorAll(".admin-content input")]
      .map((input, index) => ({
        index,
        visible: visible(input),
        type: input.type || "text",
        placeholder: input.placeholder || "",
        value: input.value || "",
        disabled: input.disabled || input.readOnly,
        role: input.getAttribute("role") || "",
      }))
      .filter((input) => input.visible)
      .filter((input) => !["password", "file", "date", "datetime-local", "time", "checkbox", "radio"].includes(input.type));
    return {
      title: document.querySelector(".admin-title-block h4")?.innerText?.trim() || "",
      contentLength: contentText.replace(/\s+/g, "").length,
      contentHash: Array.from(contentText).reduce((acc, ch) => ((acc << 5) - acc + ch.charCodeAt(0)) | 0, 0),
      contentSample: contentText.slice(0, 500),
      cards,
      tables,
      modals,
      messages,
      stats,
      anomalySamples,
      enabledButtons,
      inputs,
      selectedTabs: [...document.querySelectorAll(".admin-content .ant-tabs-tab-active")]
        .map((tab) => textOf(tab))
        .filter(Boolean),
      tabTexts: [...document.querySelectorAll(".admin-content .ant-tabs-tab-btn")]
        .filter(visible)
        .map((tab) => textOf(tab))
        .filter(Boolean)
        .slice(0, 24),
    };
  }, { textAnomalySource: TEXT_ANOMALY_RE.source, placeholderSource: PLACEHOLDER_RE.source });
}

async function closeOverlays(page) {
  await page.evaluate(() => {
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
    };
    const overlayRoots = [...document.querySelectorAll(".ant-modal, .ant-drawer, .ant-popover, .ant-dropdown")].filter(visible);
    const button = overlayRoots.flatMap((root) => [...root.querySelectorAll(".ant-modal-close, .ant-drawer-close, button")])
      .filter(visible)
      .find((candidate) => {
        const text = (candidate.innerText || candidate.getAttribute("aria-label") || candidate.title || "").trim();
        return ["取消", "关闭", "Close"].includes(text) || candidate.classList.contains("ant-modal-close") || candidate.classList.contains("ant-drawer-close");
      });
    button?.click();
  });
}

async function performAction(page, eventStore, args, entry, action) {
  const before = await collectOutput(page);
  eventStore.current = newEventBucket();
  const startedAt = Date.now();
  let actionError = null;
  try {
    if (action.kind === "tab") {
      await page.evaluate((label) => {
        const visible = (element) => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
        };
        const target = [...document.querySelectorAll(".admin-content .ant-tabs-tab-btn")]
          .filter(visible)
          .find((tab) => (tab.innerText || "").trim() === label);
        target?.click();
      }, action.label);
    } else if (action.kind === "button") {
      await page.evaluate((label) => {
        const visible = (element) => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
        };
        const target = [...document.querySelectorAll(".admin-content button")]
          .filter(visible)
          .find((button) => (button.innerText || button.getAttribute("aria-label") || button.title || "").trim().replace(/\s+/g, " ") === label);
        target?.click();
      }, action.label);
    } else if (action.kind === "input") {
      const locator = page.locator(".admin-content input").nth(action.index);
      if (await locator.count()) {
        await locator.fill(action.value);
        await locator.press("Enter");
      }
    }
  } catch (error) {
    actionError = { message: error.message, stack: String(error.stack || "").slice(0, 800) };
  }
  await page.waitForTimeout(args.actionDelayMs);
  await closeOverlays(page);
  await page.waitForTimeout(150);
  const actionDurationMs = Date.now() - startedAt;
  const after = await collectOutput(page);
  const events = eventStore.current || newEventBucket();
  eventStore.current = null;
  const uiChanged = before.contentHash !== after.contentHash || before.modals.join("|") !== after.modals.join("|") || before.messages.join("|") !== after.messages.join("|");
  const apiCalls = summarizeCalls(events.apiCalls);
  const result = {
    ...action,
    actionError,
    actionDurationMs,
    uiChanged,
    apiCalls,
    messages: after.messages,
    modals: after.modals,
    tableSummary: after.tables.map((table) => ({ rows: table.rows, empty: table.empty, headers: table.headers.slice(0, 6), sample: table.sample.slice(0, 120) })),
    outputSample: after.contentSample,
    anomalies: after.anomalySamples,
    consoleErrors: events.consoleErrors,
    pageErrors: events.pageErrors,
    requestFailures: events.requestFailures,
  };
  result.issues = issuesForAction(entry, result, args);
  return result;
}

function apiCallIssues(entry, label, call, args) {
  const issues = [];
  const ok = typeof call.ok === "boolean" ? call.ok : call.status >= 200 && call.status < 400;
  if (!ok) {
    issues.push(issue(entry, call.status >= 500 ? "major" : "minor", "api-error", `功能点「${label}」接口返回 HTTP ${call.status}`, call));
  }
  for (const warning of call.semanticWarnings || []) {
    issues.push(issue(entry, "major", "api-output-mismatch", `功能点「${label}」接口输出语义异常：${warning}`, call));
  }
  if (Number.isFinite(call.durationMs) && call.durationMs >= args.slowApiMs) {
    issues.push(issue(entry, call.durationMs >= args.verySlowApiMs ? "major" : "minor", "slow-api", `功能点「${label}」接口耗时 ${call.durationMs}ms，超过 ${args.slowApiMs}ms`, call));
  }
  return issues;
}

function issuesForAction(entry, result, args) {
  const issues = [];
  if (result.actionError) {
    issues.push(issue(entry, "major", "action-error", `功能点「${result.label}」执行失败`, result.actionError));
  }
  for (const call of result.apiCalls) {
    issues.push(...apiCallIssues(entry, result.label, call, args));
  }
  for (const request of result.requestFailures) {
    issues.push(issue(entry, "major", "request-failed", `功能点「${result.label}」请求失败`, request));
  }
  for (const error of result.pageErrors) {
    issues.push(issue(entry, "blocker", "page-error", `功能点「${result.label}」触发前端异常`, error));
  }
  for (const error of result.consoleErrors) {
    issues.push(issue(entry, "major", "console-error", `功能点「${result.label}」输出控制台错误`, error));
  }
  if (result.anomalies.length) {
    issues.push(issue(entry, "major", "output-anomaly", `功能点「${result.label}」输出存在异常文本`, { samples: result.anomalies }));
  }
  if (Number.isFinite(result.actionDurationMs) && result.actionDurationMs >= args.slowActionMs) {
    issues.push(issue(entry, "minor", "slow-action", `功能点「${result.label}」执行耗时 ${result.actionDurationMs}ms，超过 ${args.slowActionMs}ms`, {
      actionDurationMs: result.actionDurationMs,
      apiCalls: result.apiCalls,
      outputSample: result.outputSample.slice(0, 240),
    }));
  }
  if (result.kind === "button" && EFFECT_EXPECTED_RE.test(result.label) && !result.apiCalls.length && !result.uiChanged && !result.messages.length && !result.modals.length) {
    issues.push(issue(entry, "minor", "no-visible-effect", `功能点「${result.label}」没有接口、提示、弹窗或页面变化`, {
      tableSummary: result.tableSummary,
      outputSample: result.outputSample.slice(0, 240),
    }));
  }
  if (entry.key === "ops.ha" && result.label === "生成方案") {
    const planText = [result.outputSample, ...result.modals].join("\n");
    const planUrls = result.apiCalls.map((call) => call.url).join("\n");
    if (/数据治理|历史归档|分区治理|VACUUM|\bgovernance\b|\/plans\/governance\b/i.test(`${planText}\n${planUrls}`)) {
      issues.push(issue(entry, "major", "wrong-functional-output", "高可用生成方案输出了数据治理类内容", {
        apiCalls: result.apiCalls,
        sample: planText.slice(0, 360),
      }));
    }
    if (!/(高可用|HA|Failover|Switchover|切换|主备|复制|Always\s*On|Data\s*Guard|Patroni|replication)/i.test(planText)) {
      issues.push(issue(entry, "major", "missing-functional-output", "高可用生成方案缺少高可用/切换/复制相关输出", {
        apiCalls: result.apiCalls,
        sample: planText.slice(0, 360),
      }));
    }
  }
  return issues;
}

function pageOutputIssues(entry, output) {
  const issues = [];
  if (output.contentLength < 4) {
    issues.push(issue(entry, "major", "blank-output", "页面内容区为空，无法验证功能输出", { sample: output.contentSample }));
  }
  if (output.anomalySamples.length) {
    issues.push(issue(entry, "major", "output-anomaly", "页面初始输出存在异常文本", { samples: output.anomalySamples }));
  }
  const nonEmptyTables = output.tables.filter((table) => table.rows > 0);
  const hasStructuredOutput = output.cards.length || output.stats.length || output.tables.length || output.modals.length || output.messages.length;
  if (!hasStructuredOutput && output.contentLength < 80) {
    issues.push(issue(entry, "minor", "thin-output", "页面缺少可验证的结构化输出", { sample: output.contentSample }));
  }
  for (const table of nonEmptyTables) {
    if (!table.headers.length) {
      issues.push(issue(entry, "minor", "table-header-missing", "表格存在数据行但缺少表头", { sample: table.sample }));
    }
  }
  return issues;
}

function buildActions(output, args) {
  const actions = [];
  const seen = new Set();
  for (const label of output.tabTexts) {
    const key = `tab:${label}`;
    if (!seen.has(key)) {
      actions.push({ kind: "tab", label });
      seen.add(key);
    }
  }
  for (const button of output.enabledButtons) {
    const label = button.text;
    if (!label || !SAFE_BUTTON_RE.test(label) || RISKY_BUTTON_RE.test(label)) continue;
    const key = `button:${label}`;
    if (seen.has(key)) continue;
    actions.push({ kind: "button", label });
    seen.add(key);
    if (actions.filter((item) => item.kind === "button").length >= args.maxButtonsPerPage) break;
  }
  const searchInputs = output.inputs
    .filter((input) => !input.disabled)
    .filter((input) => /搜索|查询|关键|名称|SQL|实例|规则|指标|描述|工单/i.test(input.placeholder || input.role || ""))
    .slice(0, args.maxInputsPerPage);
  for (const input of searchInputs) {
    const value = /SQL/i.test(input.placeholder) ? "select" : "orcl";
    actions.push({ kind: "input", label: input.placeholder || `input-${input.index}`, index: input.index, value });
  }
  return actions;
}

function percentile(values, ratio) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
  if (!sorted.length) return 0;
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(sorted.length * ratio) - 1));
  return sorted[index];
}

function computePerformanceSummary(pages, args) {
  const pageLoads = pages
    .filter((item) => Number.isFinite(item.pageLoadMs))
    .map((item) => ({ page: item.page, durationMs: item.pageLoadMs }));
  const actions = pages.flatMap((pageResult) => (pageResult.actions || [])
    .filter((action) => Number.isFinite(action.actionDurationMs))
    .map((action) => ({
      page: pageResult.page,
      kind: action.kind,
      label: action.label,
      durationMs: action.actionDurationMs,
      apiCalls: action.apiCalls || [],
    })));
  const apiCalls = pages.flatMap((pageResult) => [
    ...(pageResult.loadApiCalls || []).map((call) => ({ ...call, page: pageResult.page, phase: "load", label: "页面加载" })),
    ...(pageResult.actions || []).flatMap((action) => (action.apiCalls || []).map((call) => ({ ...call, page: pageResult.page, phase: "action", label: action.label }))),
  ]).filter((call) => Number.isFinite(call.durationMs));

  const topPages = pageLoads.slice().sort((a, b) => b.durationMs - a.durationMs).slice(0, 12);
  const topActions = actions.slice().sort((a, b) => b.durationMs - a.durationMs).slice(0, 12);
  const topApis = apiCalls.slice().sort((a, b) => b.durationMs - a.durationMs).slice(0, 20);
  const outputWarnings = apiCalls
    .filter((call) => (call.semanticWarnings || []).length)
    .map((call) => ({
      page: call.page,
      label: call.label,
      path: call.path,
      status: call.status,
      semanticWarnings: call.semanticWarnings,
      jsonSummary: call.jsonSummary,
    }))
    .slice(0, 30);

  return {
    thresholds: {
      slowPageMs: args.slowPageMs,
      slowActionMs: args.slowActionMs,
      slowApiMs: args.slowApiMs,
      verySlowApiMs: args.verySlowApiMs,
    },
    pageLoad: {
      count: pageLoads.length,
      p50Ms: percentile(pageLoads.map((item) => item.durationMs), 0.5),
      p95Ms: percentile(pageLoads.map((item) => item.durationMs), 0.95),
      maxMs: topPages[0]?.durationMs || 0,
      slowCount: pageLoads.filter((item) => item.durationMs >= args.slowPageMs).length,
      top: topPages,
    },
    action: {
      count: actions.length,
      p50Ms: percentile(actions.map((item) => item.durationMs), 0.5),
      p95Ms: percentile(actions.map((item) => item.durationMs), 0.95),
      maxMs: topActions[0]?.durationMs || 0,
      slowCount: actions.filter((item) => item.durationMs >= args.slowActionMs).length,
      top: topActions,
    },
    api: {
      count: apiCalls.length,
      p50Ms: percentile(apiCalls.map((item) => item.durationMs), 0.5),
      p95Ms: percentile(apiCalls.map((item) => item.durationMs), 0.95),
      maxMs: topApis[0]?.durationMs || 0,
      slowCount: apiCalls.filter((item) => item.durationMs >= args.slowApiMs).length,
      verySlowCount: apiCalls.filter((item) => item.durationMs >= args.verySlowApiMs).length,
      top: topApis,
    },
    outputWarnings,
  };
}

function collectSkippedRisky(output) {
  return output.enabledButtons
    .filter((button) => button.text && RISKY_BUTTON_RE.test(button.text))
    .map((button) => button.text)
    .filter((value, index, array) => array.indexOf(value) === index)
    .slice(0, 30);
}

async function auditPage(page, entry, args, eventStore) {
  eventStore.current = newEventBucket();
  const pageStartedAt = Date.now();
  await navigateToEntry(page, entry, args);
  await page.waitForTimeout(400);
  const pageLoadMs = Date.now() - pageStartedAt;
  const initialEvents = eventStore.current;
  eventStore.current = null;
  const initialOutput = await collectOutput(page);
  const actions = buildActions(initialOutput, args);
  const actionResults = [];
  for (const action of actions) {
    actionResults.push(await performAction(page, eventStore, args, entry, action));
  }
  const finalOutput = await collectOutput(page);
  const riskyActions = collectSkippedRisky(finalOutput);
  const issues = [
    ...pageOutputIssues(entry, initialOutput),
    ...initialEvents.consoleErrors.map((error) => issue(entry, "major", "console-error", "页面加载时输出控制台错误", error)),
    ...initialEvents.pageErrors.map((error) => issue(entry, "blocker", "page-error", "页面加载时触发前端异常", error)),
    ...initialEvents.apiCalls.flatMap((call) => apiCallIssues(entry, "页面加载", call, args)),
    ...(pageLoadMs >= args.slowPageMs
      ? [issue(entry, "minor", "slow-page", `页面加载耗时 ${pageLoadMs}ms，超过 ${args.slowPageMs}ms`, {
        pageLoadMs,
        apiCalls: summarizeCalls(initialEvents.apiCalls).sort((a, b) => (b.durationMs || 0) - (a.durationMs || 0)).slice(0, 8),
        outputSample: initialOutput.contentSample.slice(0, 240),
      })]
      : []),
    ...actionResults.flatMap((result) => result.issues || []),
  ];
  const uniqueIssues = [...new Map(issues.map((item) => [[item.pageKey, item.engine, item.type, item.message, JSON.stringify(item.detail).slice(0, 120)].join("|"), item])).values()];
  return {
    page: entry,
    pageLoadMs,
    loadApiCalls: summarizeCalls(initialEvents.apiCalls),
    initialOutput,
    finalOutput,
    actionCount: actionResults.length,
    actions: actionResults,
    riskyActions,
    skippedRiskyCount: riskyActions.length,
    issueCount: uniqueIssues.length,
    issues: uniqueIssues,
  };
}

function renderMarkdown(report) {
  const lines = [];
  lines.push("# 前端功能点详细巡检报告");
  lines.push("");
  lines.push(`- 时间：${report.createdAt}`);
  lines.push(`- 前端：${report.baseUrl}`);
  lines.push(`- 页面数：${report.pageCount}`);
  lines.push(`- 执行动作数：${report.actionCount}`);
  lines.push(`- 问题数：${report.issueCount}`);
  lines.push(`- 跳过高风险动作数：${report.skippedRiskyCount}`);
  if (report.performance) {
    lines.push(`- 页面加载 P95：${report.performance.pageLoad.p95Ms}ms，最慢：${report.performance.pageLoad.maxMs}ms`);
    lines.push(`- 功能动作 P95：${report.performance.action.p95Ms}ms，最慢：${report.performance.action.maxMs}ms`);
    lines.push(`- API P95：${report.performance.api.p95Ms}ms，最慢：${report.performance.api.maxMs}ms`);
  }
  lines.push("");
  lines.push("## 问题清单");
  lines.push("");
  if (!report.issues.length) {
    lines.push("未发现问题。");
  } else {
    report.issues.forEach((item, index) => {
      const page = item.engine ? `${item.pageLabel}（${item.engine}）` : item.pageLabel;
      lines.push(`${index + 1}. [${item.severity}] ${page} / ${item.type}：${item.message}`);
      if (item.detail && Object.keys(item.detail).length) {
        lines.push(`   - 详情：\`${JSON.stringify(item.detail).slice(0, 700)}\``);
      }
    });
  }
  lines.push("");
  if (report.performance) {
    lines.push("## 输出准确性与性能");
    lines.push("");
    lines.push(`- 慢页面阈值：${report.performance.thresholds.slowPageMs}ms，慢功能动作阈值：${report.performance.thresholds.slowActionMs}ms，慢 API 阈值：${report.performance.thresholds.slowApiMs}ms`);
    lines.push(`- 慢页面数：${report.performance.pageLoad.slowCount}`);
    lines.push(`- 慢功能动作数：${report.performance.action.slowCount}`);
    lines.push(`- 慢 API 数：${report.performance.api.slowCount}，严重慢 API 数：${report.performance.api.verySlowCount}`);
    lines.push(`- API 输出语义警告数：${report.performance.outputWarnings.length}`);
    lines.push("");
    lines.push("### 最慢页面");
    report.performance.pageLoad.top.slice(0, 8).forEach((item, index) => {
      const page = item.page.engine ? `${item.page.label}（${item.page.engine}）` : item.page.label;
      lines.push(`${index + 1}. ${page}：${item.durationMs}ms`);
    });
    lines.push("");
    lines.push("### 最慢功能动作");
    report.performance.action.top.slice(0, 8).forEach((item, index) => {
      const page = item.page.engine ? `${item.page.label}（${item.page.engine}）` : item.page.label;
      lines.push(`${index + 1}. ${page} / ${item.label}：${item.durationMs}ms`);
    });
    lines.push("");
    lines.push("### 最慢 API");
    report.performance.api.top.slice(0, 12).forEach((item, index) => {
      const page = item.page.engine ? `${item.page.label}（${item.page.engine}）` : item.page.label;
      lines.push(`${index + 1}. ${page} / ${item.label} / ${item.path}：${item.durationMs}ms，HTTP ${item.status}`);
    });
    lines.push("");
  }
  lines.push("");
  lines.push("## 页面与功能点覆盖");
  lines.push("");
  report.pages.forEach((pageResult) => {
    const page = pageResult.page.engine ? `${pageResult.page.label}（${pageResult.page.engine}）` : pageResult.page.label;
    lines.push(`- ${page}：${pageResult.issueCount ? `${pageResult.issueCount} 个问题` : "通过"}，页面加载 ${pageResult.pageLoadMs ?? "-"}ms，执行 ${pageResult.actionCount} 个动作，跳过高风险 ${pageResult.skippedRiskyCount} 个`);
    const actionText = pageResult.actions.map((action) => `${action.kind}:${action.label}${action.actionDurationMs ? ` ${action.actionDurationMs}ms` : ""}${action.apiCalls.length ? `(${action.apiCalls.map((call) => `${call.status}/${call.durationMs ?? "-"}ms`).join("/")})` : ""}`).slice(0, 12).join("；");
    if (actionText) lines.push(`  - 已测：${actionText}`);
    if (pageResult.riskyActions.length) lines.push(`  - 未自动确认：${pageResult.riskyActions.join("、")}`);
  });
  lines.push("");
  return lines.join("\n");
}

function writeReport(report) {
  mkdirSync(REPORT_DIR, { recursive: true });
  const base = `frontend-functional-audit-${timestamp()}`;
  const jsonPath = path.join(REPORT_DIR, `${base}.json`);
  const markdownPath = path.join(REPORT_DIR, `${base}.md`);
  writeFileSync(jsonPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  writeFileSync(markdownPath, renderMarkdown(report), "utf8");
  return { jsonPath, markdownPath };
}

async function closeBrowserWithTimeout(browser) {
  try {
    await Promise.race([
      browser.close(),
      new Promise((resolve) => setTimeout(resolve, 3000)),
    ]);
  } catch {
    // Best-effort cleanup; keep the original test result visible.
  }
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  const token = await loginViaApi(args);
  const instances = await fetchInstances(args, token);
  const { chromium } = loadPlaywright();
  const executablePath = chromeExecutablePath();
  const browser = await chromium.launch({ headless: !args.headed, ...(executablePath ? { executablePath } : {}) });
  const eventStore = { current: null };
  const requestStartedAt = new WeakMap();
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.on("console", (message) => {
      if (!eventStore.current || message.type() !== "error") return;
      const text = message.text();
      if (/ResizeObserver loop completed|favicon.ico/.test(text)) return;
      eventStore.current.consoleErrors.push({ text: text.slice(0, 1200), location: message.location() });
    });
    page.on("pageerror", (error) => {
      if (!eventStore.current) return;
      eventStore.current.pageErrors.push({ message: error.message, stack: String(error.stack || "").slice(0, 1200) });
    });
    page.on("requestfailed", (request) => {
      if (!eventStore.current || !request.url().includes("/api/v1/")) return;
      eventStore.current.requestFailures.push({ url: request.url(), failure: request.failure()?.errorText || "unknown" });
    });
    page.on("request", (request) => {
      if (!request.url().includes("/api/v1/")) return;
      requestStartedAt.set(request, { startedAt: Date.now(), bucketId: eventStore.current?.id || null });
    });
    page.on("response", async (response) => {
      if (!eventStore.current || !response.url().includes("/api/v1/")) return;
      const bucket = eventStore.current;
      const request = response.request();
      const requestTiming = requestStartedAt.get(request);
      if (!requestTiming || requestTiming.bucketId !== bucket.id) return;
      const durationMs = Number.isFinite(requestTiming.startedAt) ? Date.now() - requestTiming.startedAt : null;
      let bodySample = "";
      let jsonSummary = null;
      let responseBytes = null;
      const headers = response.headers();
      const contentType = headers["content-type"] || "";
      const contentLength = Number.parseInt(headers["content-length"] || "", 10);
      if (Number.isFinite(contentLength)) responseBytes = contentLength;
      if (response.status() >= 400) {
        try {
          bodySample = (await response.text()).slice(0, 500);
        } catch {
          bodySample = "";
        }
      } else if (/application\/json|\+json|text\//i.test(contentType)) {
        try {
          const text = await response.text();
          responseBytes = Buffer.byteLength(text, "utf8");
          bodySample = text.slice(0, API_BODY_SAMPLE_LIMIT);
          if (/application\/json|\+json/i.test(contentType)) {
            jsonSummary = summarizeJsonPayload(JSON.parse(text || "null"));
          }
        } catch {
          bodySample = "";
        }
      }
      if (eventStore.current?.id !== bucket.id) return;
      bucket.apiCalls.push({
        method: request.method(),
        status: response.status(),
        url: response.url(),
        path: new URL(response.url()).pathname,
        durationMs,
        responseBytes,
        bodySample,
        jsonSummary,
      });
    });

    await page.goto(args.baseUrl, { waitUntil: "domcontentloaded" });
    await loginIfNeeded(page);
    await page.evaluate((authToken) => localStorage.setItem("mesguard_token", authToken), token);
    await page.waitForFunction(() => Boolean(document.querySelector(".admin-shell")), null, { timeout: 20000 });

    const pages = [];
    for (const entry of createEntries(instances)) {
      process.stdout.write(`功能巡检: ${entry.engine ? `${entry.engine} ` : ""}${entry.label} ... `);
      try {
        const result = await auditPage(page, entry, args, eventStore);
        pages.push(result);
        process.stdout.write(result.issueCount ? `${result.issueCount} issue(s), ${result.actionCount} actions\n` : `ok, ${result.actionCount} actions\n`);
      } catch (error) {
        const failed = {
          page: entry,
          initialOutput: null,
          finalOutput: null,
          actionCount: 0,
          actions: [],
          riskyActions: [],
          skippedRiskyCount: 0,
          issueCount: 1,
          issues: [issue(entry, "blocker", "audit-error", "功能巡检无法完成该页面", { message: error.message, stack: String(error.stack || "").slice(0, 1200) })],
        };
        pages.push(failed);
        process.stdout.write("audit failed\n");
      }
    }
    const issues = pages.flatMap((item) => item.issues);
    const performance = computePerformanceSummary(pages, args);
    const report = {
      ok: issues.length === 0,
      createdAt: new Date().toISOString(),
      baseUrl: args.baseUrl,
      thresholds: {
        slowApiMs: args.slowApiMs,
        verySlowApiMs: args.verySlowApiMs,
        slowPageMs: args.slowPageMs,
        slowActionMs: args.slowActionMs,
      },
      pageCount: pages.length,
      actionCount: pages.reduce((sum, item) => sum + item.actionCount, 0),
      skippedRiskyCount: pages.reduce((sum, item) => sum + item.skippedRiskyCount, 0),
      issueCount: issues.length,
      instances: instances.map((item) => ({ id: item.id, name: item.instance_name, db_type: item.db_type })),
      performance,
      pages,
      issues,
    };
    const paths = writeReport(report);
    console.log(JSON.stringify({ ok: report.ok, issueCount: report.issueCount, actionCount: report.actionCount, skippedRiskyCount: report.skippedRiskyCount, reports: paths }, null, 2));
    process.exitCode = report.ok ? 0 : 1;
  } finally {
    await closeBrowserWithTimeout(browser);
  }
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
