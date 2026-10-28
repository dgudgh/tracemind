const { existsSync, mkdirSync, writeFileSync } = require("node:fs");
const path = require("node:path");
const { loadAuditMenuPages, loadOpsCapabilities } = require("./frontend_audit_pages.cjs");

const DEFAULT_FRONTEND_URL = process.env.FRONTEND_URL || process.env.FRONTEND_AUDIT_URL || "http://127.0.0.1:3300/";
const DEFAULT_BACKEND_URL = process.env.BACKEND_URL || "";
const USERNAME = process.env.MESGUARD_TEST_USER || "admin";
const PASSWORD = process.env.MESGUARD_TEST_PASSWORD || "";
const REPORT_DIR = process.env.FRONTEND_AUDIT_REPORT_DIR || path.resolve(__dirname, "../reports/frontend-audit");

const BASE_PAGES = loadAuditMenuPages();
const OPS_CAPABILITIES = loadOpsCapabilities();

const SAFE_BUTTON_RE = /^(查询|搜索|刷新|重置|清空|取消|返回|展开|收起|复制|查看|预览|筛选|应用筛选|清除筛选)$/;
const RISKY_BUTTON_RE = /(删除|移除|保存|提交|确认|启用|停用|关闭告警|执行|运行|开始|停止|恢复|备份|审批|通过|拒绝|测试连接|同步|导入|导出|上传|下载|Kill|kill|清理|归档|扩容|创建|新增|编辑|重置密码|退出)/;

function loadPlaywright() {
  try {
    return require("playwright");
  } catch (error) {
    throw new Error(
      "playwright is required. Run with NODE_PATH pointing at the bundled node_modules. Original error: " +
        error.message,
    );
  }
}

function chromeExecutablePath() {
  const candidates = [
    process.env.CHROME_EXECUTABLE,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate));
}

function parseArgs(argv) {
  const args = {
    baseUrl: DEFAULT_FRONTEND_URL,
    backendUrl: DEFAULT_BACKEND_URL,
    cleanRuns: Number.parseInt(process.env.FRONTEND_AUDIT_CLEAN_RUNS || "1", 10),
    maxRuns: Number.parseInt(process.env.FRONTEND_AUDIT_MAX_RUNS || "1", 10),
    pageDelayMs: Number.parseInt(process.env.FRONTEND_AUDIT_PAGE_DELAY_MS || "1200", 10),
    actionDelayMs: Number.parseInt(process.env.FRONTEND_AUDIT_ACTION_DELAY_MS || "350", 10),
    maxSafeButtonsPerPage: Number.parseInt(process.env.FRONTEND_AUDIT_MAX_SAFE_BUTTONS || "8", 10),
    headed: process.env.FRONTEND_AUDIT_HEADED === "1",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === "--base-url") {
      args.baseUrl = argv[index + 1];
      index += 1;
    } else if (item === "--backend-url") {
      args.backendUrl = argv[index + 1];
      index += 1;
    } else if (item === "--clean-runs") {
      args.cleanRuns = Number.parseInt(argv[index + 1], 10);
      index += 1;
    } else if (item === "--max-runs") {
      args.maxRuns = Number.parseInt(argv[index + 1], 10);
      index += 1;
    } else if (item === "--headed") {
      args.headed = true;
    } else if (item === "-h" || item === "--help") {
      console.log("Usage: node scripts/check_frontend_full_audit.cjs [--base-url URL] [--clean-runs N] [--max-runs N] [--headed]");
      process.exit(0);
    }
  }
  args.cleanRuns = Number.isFinite(args.cleanRuns) && args.cleanRuns > 0 ? args.cleanRuns : 1;
  args.maxRuns = Number.isFinite(args.maxRuns) && args.maxRuns > 0 ? args.maxRuns : args.cleanRuns;
  args.baseUrl = args.baseUrl.replace(/\/$/, "");
  args.backendUrl = args.backendUrl ? args.backendUrl.replace(/\/$/, "") : "";
  return args;
}

function timestamp() {
  return new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "Z");
}

function apiBase(args) {
  if (args.backendUrl) return `${args.backendUrl}/api/v1`;
  return `${args.baseUrl}/api/v1`;
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
  if (!PASSWORD) throw new Error("MESGUARD_TEST_PASSWORD is required for the frontend audit");
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
  const username = page.locator("#username");
  if (!(await username.count())) return;
  await username.fill(USERNAME);
  await page.locator("#password").fill(PASSWORD);
  await page.getByRole("button", { name: "打开诊断工作台" }).click();
}

async function waitForAdminShell(page) {
  await page.waitForFunction(() => Boolean(document.querySelector(".admin-shell")), null, { timeout: 20000 });
}

async function submitSmartCommand(page, command) {
  await page.locator(".admin-smart-command input").fill(command);
  await page.locator(".admin-smart-command input").press("Enter");
}

async function navigateToPage(page, entry, args) {
  const command = `打开 ${entry.engine || ""} ${entry.key} ${entry.label}`.replace(/\s+/g, " ").trim();
  await submitSmartCommand(page, command);
  await page.waitForTimeout(args.pageDelayMs);
  await page.waitForFunction(
    () => Boolean(document.querySelector(".admin-shell") && document.querySelector(".admin-content")),
    null,
    { timeout: 15000 },
  );
}

function issue(page, severity, type, message, detail = {}) {
  return {
    pageKey: page.key,
    pageLabel: page.label,
    engine: page.engine || "",
    severity,
    type,
    message,
    detail,
  };
}

function normalizeIssueKey(item) {
  return [item.pageKey, item.engine || "", item.type, item.message].join(" | ");
}

async function collectPageMetrics(page, entry) {
  return page.evaluate((expected) => {
    const root = document.documentElement;
    const content = document.querySelector(".admin-content");
    const shell = document.querySelector(".admin-shell");
    const topbar = document.querySelector(".admin-topbar");
    const title = document.querySelector(".admin-title-block h4")?.innerText?.trim() || "";
    const breadcrumb = document.querySelector(".admin-breadcrumb")?.innerText?.trim() || "";
    const contentText = content?.innerText?.trim() || "";
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
    };
    const rectInfo = (element) => {
      const rect = element?.getBoundingClientRect();
      return rect
        ? {
            x: Number(rect.x.toFixed(2)),
            y: Number(rect.y.toFixed(2)),
            width: Number(rect.width.toFixed(2)),
            height: Number(rect.height.toFixed(2)),
          }
        : null;
    };
    const isInsideManagedHorizontalScroll = (element) => {
      if (element.closest(".ant-table-body, .ant-table-content, .ant-table-container")) return true;
      for (let parent = element.parentElement; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent);
        const scrollsHorizontally = parent.scrollWidth > parent.clientWidth + 2;
        const clipsOrScrolls = /(auto|scroll|hidden)/.test(style.overflowX);
        if (scrollsHorizontally && clipsOrScrolls) return true;
      }
      return false;
    };
    const overflowElements = [...document.querySelectorAll("button, input, textarea, select, .ant-card, .ant-table-wrapper, .ant-tabs, .ant-alert")]
      .filter(visible)
      .filter((element) => !isInsideManagedHorizontalScroll(element))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          tag: element.tagName,
          text: (element.innerText || element.getAttribute("placeholder") || element.getAttribute("aria-label") || "").trim().slice(0, 80),
          className: String(element.className || "").slice(0, 120),
          left: Number(rect.left.toFixed(2)),
          right: Number(rect.right.toFixed(2)),
          width: Number(rect.width.toFixed(2)),
        };
      })
      .filter((item) => item.left < -2 || item.right > root.clientWidth + 2)
      .slice(0, 12);
    const clippedButtons = [...document.querySelectorAll("button")]
      .filter(visible)
      .filter((button) => (button.innerText || button.getAttribute("aria-label") || button.title || "").trim())
      .filter((button) => button.scrollWidth > button.clientWidth + 2 || button.scrollHeight > button.clientHeight + 2)
      .map((button) => ({
        text: (button.innerText || button.getAttribute("aria-label") || "").trim().slice(0, 80),
        scrollWidth: button.scrollWidth,
        clientWidth: button.clientWidth,
        scrollHeight: button.scrollHeight,
        clientHeight: button.clientHeight,
      }))
      .slice(0, 12);
    const safeButtons = [...document.querySelectorAll(".admin-content button")]
      .filter(visible)
      .map((button, index) => ({
        index,
        text: (button.innerText || button.getAttribute("aria-label") || button.title || "").trim().replace(/\s+/g, " "),
        disabled: button.disabled || button.getAttribute("aria-disabled") === "true",
      }))
      .filter((button) => button.text);
    const tabTexts = [...document.querySelectorAll(".admin-content .ant-tabs-tab-btn")]
      .filter(visible)
      .map((tab) => tab.innerText.trim())
      .filter(Boolean)
      .slice(0, 24);
    return {
      expected,
      url: location.href,
      documentTitle: document.title,
      title,
      breadcrumb,
      contentLength: contentText.replace(/\s+/g, "").length,
      contentSample: contentText.slice(0, 500),
      hasViteOverlay: Boolean(document.querySelector("vite-error-overlay")),
      hasReactErrorText: /ReferenceError|TypeError|Cannot read properties|Minified React error|Unhandled Runtime Error/.test(document.body?.innerText || ""),
      docScrollWidth: root.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      viewportWidth: root.clientWidth,
      docScrollHeight: root.scrollHeight,
      viewportHeight: root.clientHeight,
      shell: rectInfo(shell),
      topbar: rectInfo(topbar),
      content: rectInfo(content),
      contentHasVerticalScroll: content ? content.scrollHeight > content.clientHeight + 2 : false,
      overflowElements,
      clippedButtons,
      safeButtons,
      tabTexts,
    };
  }, { key: entry.key, label: entry.label, engine: entry.engine || "" });
}

async function exerciseTabs(page, args) {
  const actions = [];
  const tabCount = await page.locator(".admin-content .ant-tabs-tab-btn").count();
  const limit = Math.min(tabCount, 12);
  for (let index = 0; index < limit; index += 1) {
    const result = await page.evaluate((tabIndex) => {
      const visible = (element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
      };
      const tabs = [...document.querySelectorAll(".admin-content .ant-tabs-tab-btn")].filter(visible);
      const target = tabs[tabIndex];
      if (!target) return null;
      target.click();
      return target.innerText.trim();
    }, index);
    if (!result) continue;
    actions.push({ type: "tab", label: result });
    await page.waitForTimeout(args.actionDelayMs);
    await closeTransientOverlays(page);
  }
  return actions;
}

async function exerciseSafeButtons(page, args) {
  const actions = [];
  for (let attempts = 0; attempts < args.maxSafeButtonsPerPage; attempts += 1) {
    const clicked = await page.evaluate(({ safeSource, riskySource, seen }) => {
      const safe = new RegExp(safeSource);
      const risky = new RegExp(riskySource, "i");
      const visible = (element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
      };
      const buttons = [...document.querySelectorAll(".admin-content button")]
        .filter(visible)
        .filter((button) => !button.disabled && button.getAttribute("aria-disabled") !== "true")
        .map((button) => ({ button, text: (button.innerText || button.getAttribute("aria-label") || button.title || "").trim().replace(/\s+/g, " ") }))
        .filter((item) => item.text && safe.test(item.text) && !risky.test(item.text))
        .filter((item) => !seen.includes(item.text));
      const target = buttons[0];
      if (!target) return null;
      target.button.click();
      return target.text;
    }, { safeSource: SAFE_BUTTON_RE.source, riskySource: RISKY_BUTTON_RE.source, seen: actions.map((item) => item.label) });
    if (!clicked) break;
    actions.push({ type: "button", label: clicked });
    await page.waitForTimeout(args.actionDelayMs);
    await closeTransientOverlays(page);
  }
  return actions;
}

async function closeTransientOverlays(page) {
  await page.evaluate(() => {
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
    };
    const overlayRoots = [...document.querySelectorAll(".ant-modal, .ant-drawer, .ant-popover, .ant-dropdown")].filter(visible);
    const closeButton = overlayRoots.flatMap((root) => [...root.querySelectorAll(".ant-modal-close, .ant-drawer-close, button")])
      .filter(visible)
      .find((button) => {
        const text = (button.innerText || button.getAttribute("aria-label") || button.title || "").trim();
        return ["关闭", "取消", "Close"].includes(text) || button.classList.contains("ant-modal-close") || button.classList.contains("ant-drawer-close");
      });
    closeButton?.click();
  });
}

function issuesFromMetrics(entry, metrics) {
  const issues = [];
  if (metrics.hasViteOverlay) issues.push(issue(entry, "blocker", "render-error", "页面出现 Vite/React 错误遮罩"));
  if (metrics.hasReactErrorText) issues.push(issue(entry, "blocker", "render-error", "页面正文包含前端运行时错误文本", { sample: metrics.contentSample }));
  if (!metrics.title) issues.push(issue(entry, "major", "missing-title", "页面标题为空"));
  if (metrics.contentLength < 4) issues.push(issue(entry, "major", "blank-content", "页面内容区为空或接近空白", { contentSample: metrics.contentSample }));
  if (metrics.docScrollWidth > metrics.viewportWidth + 2 || metrics.bodyScrollWidth > metrics.viewportWidth + 2) {
    issues.push(issue(entry, "major", "horizontal-overflow", "页面产生横向溢出", {
      viewportWidth: metrics.viewportWidth,
      docScrollWidth: metrics.docScrollWidth,
      bodyScrollWidth: metrics.bodyScrollWidth,
    }));
  }
  if (metrics.overflowElements.length) {
    issues.push(issue(entry, "minor", "element-overflow", "存在元素越出视口", { elements: metrics.overflowElements }));
  }
  if (metrics.clippedButtons.length) {
    issues.push(issue(entry, "minor", "button-clipped", "存在按钮文字或图标被裁切", { buttons: metrics.clippedButtons }));
  }
  return issues;
}

function issuesFromEvents(entry, events) {
  const issues = [];
  for (const event of events.pageErrors) {
    issues.push(issue(entry, "blocker", "page-error", "浏览器捕获到未处理脚本错误", event));
  }
  for (const event of events.consoleErrors) {
    issues.push(issue(entry, "major", "console-error", "控制台输出 error", event));
  }
  for (const event of events.apiErrors) {
    const status = Number(event.status);
    issues.push(issue(entry, status >= 500 ? "major" : "minor", "api-error", `接口返回 HTTP ${event.status}`, event));
  }
  for (const event of events.requestFailures) {
    issues.push(issue(entry, "major", "request-failed", "请求失败", event));
  }
  return issues;
}

function createPageEntries(instances) {
  const engineSet = new Set(instances.map((item) => String(item.db_type || "").toLowerCase()).filter(Boolean));
  const entries = [...BASE_PAGES];
  for (const [capability, label, engines] of OPS_CAPABILITIES) {
    const available = engines.filter((engine) => engineSet.has(engine));
    const targets = available.length ? available : [""];
    for (const engine of targets) {
      entries.push({
        key: `ops.${capability}`,
        label,
        engine,
        commandLabel: engine ? `${engine.toUpperCase()} ${label}` : label,
      });
    }
  }
  return entries.filter((entry) => {
    if (!entry.engines?.length) return true;
    return !engineSet.size || entry.engines.some((engine) => engineSet.has(engine));
  });
}

async function auditPage(page, entry, args, eventStore) {
  eventStore.current = { pageErrors: [], consoleErrors: [], apiErrors: [], requestFailures: [] };
  await navigateToPage(page, entry, args);
  const before = await collectPageMetrics(page, entry);
  const tabActions = await exerciseTabs(page, args);
  const buttonActions = await exerciseSafeButtons(page, args);
  await page.waitForTimeout(args.actionDelayMs);
  const after = await collectPageMetrics(page, entry);
  const pageIssues = [
    ...issuesFromMetrics(entry, before),
    ...issuesFromMetrics(entry, after),
    ...issuesFromEvents(entry, eventStore.current),
  ];
  const uniqueIssues = [...new Map(pageIssues.map((item) => [normalizeIssueKey(item), item])).values()];
  return {
    page: entry,
    before,
    after,
    actions: [...tabActions, ...buttonActions],
    issueCount: uniqueIssues.length,
    issues: uniqueIssues,
  };
}

function renderMarkdown(report) {
  const lines = [];
  lines.push(`# 前端全页面巡检报告`);
  lines.push("");
  lines.push(`- 时间：${report.createdAt}`);
  lines.push(`- 前端：${report.baseUrl}`);
  lines.push(`- 页面数：${report.pageCount}`);
  lines.push(`- 问题数：${report.issueCount}`);
  lines.push(`- 连续无问题轮次：${report.cleanStreak}/${report.cleanRunsTarget}`);
  lines.push("");
  if (!report.issues.length) {
    lines.push("## 问题清单");
    lines.push("");
    lines.push("未发现问题。");
  } else {
    lines.push("## 问题清单");
    lines.push("");
    report.issues.forEach((item, index) => {
      const pageName = item.engine ? `${item.pageLabel}（${item.engine}）` : item.pageLabel;
      lines.push(`${index + 1}. [${item.severity}] ${pageName} / ${item.type}：${item.message}`);
      if (item.detail && Object.keys(item.detail).length) {
        lines.push(`   - 详情：\`${JSON.stringify(item.detail).slice(0, 500)}\``);
      }
    });
  }
  lines.push("");
  lines.push("## 覆盖页面");
  lines.push("");
  report.pages.forEach((pageResult) => {
    const pageName = pageResult.page.engine ? `${pageResult.page.label}（${pageResult.page.engine}）` : pageResult.page.label;
    lines.push(`- ${pageName}：${pageResult.issueCount ? `${pageResult.issueCount} 个问题` : "通过"}，交互 ${pageResult.actions.length} 个`);
  });
  lines.push("");
  return lines.join("\n");
}

function writeReport(report) {
  mkdirSync(REPORT_DIR, { recursive: true });
  const base = `frontend-full-audit-${timestamp()}`;
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
  } catch (error) {
    console.warn(`browser close skipped: ${error.message}`);
  }
}

async function runAudit(args, runIndex, token, instances) {
  const { chromium } = loadPlaywright();
  const executablePath = chromeExecutablePath();
  const launchOptions = executablePath ? { executablePath } : {};
  const browser = await chromium.launch({ headless: !args.headed, ...launchOptions });
  const eventStore = { current: null };
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.on("pageerror", (error) => {
      if (!eventStore.current) return;
      eventStore.current.pageErrors.push({ message: error.message, stack: String(error.stack || "").slice(0, 1200) });
    });
    page.on("console", (message) => {
      if (!eventStore.current || message.type() !== "error") return;
      const text = message.text();
      if (/ResizeObserver loop completed|favicon.ico/.test(text)) return;
      eventStore.current.consoleErrors.push({ text: text.slice(0, 1200), location: message.location() });
    });
    page.on("response", (response) => {
      if (!eventStore.current) return;
      const url = response.url();
      if (!url.includes("/api/v1/")) return;
      const status = response.status();
      if (status < 400) return;
      if (url.includes("/auth/current-user") && status === 401) return;
      eventStore.current.apiErrors.push({ status, url });
    });
    page.on("requestfailed", (request) => {
      if (!eventStore.current) return;
      const url = request.url();
      if (!url.includes("/api/v1/")) return;
      eventStore.current.requestFailures.push({ url, failure: request.failure()?.errorText || "unknown" });
    });

    await page.goto(args.baseUrl, { waitUntil: "domcontentloaded" });
    await loginIfNeeded(page);
    await page.evaluate((authToken) => {
      localStorage.setItem("mesguard_token", authToken);
    }, token);
    await waitForAdminShell(page);

    const entries = createPageEntries(instances);
    const pages = [];
    for (const entry of entries) {
      process.stdout.write(`巡检 ${runIndex}: ${entry.engine ? `${entry.engine} ` : ""}${entry.label} ... `);
      try {
        const result = await auditPage(page, entry, args, eventStore);
        pages.push(result);
        process.stdout.write(result.issueCount ? `${result.issueCount} issue(s)\n` : "ok\n");
      } catch (error) {
        const failed = {
          page: entry,
          before: null,
          after: null,
          actions: [],
          issueCount: 1,
          issues: [issue(entry, "blocker", "audit-error", "巡检脚本无法完成该页面", { message: error.message, stack: String(error.stack || "").slice(0, 1200) })],
        };
        pages.push(failed);
        process.stdout.write("audit failed\n");
      }
    }
    const issues = pages.flatMap((item) => item.issues);
    return { pages, issues };
  } finally {
    await closeBrowserWithTimeout(browser);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const token = await loginViaApi(args);
  const instances = await fetchInstances(args, token);
  let cleanStreak = 0;
  let lastReport = null;

  for (let runIndex = 1; runIndex <= args.maxRuns; runIndex += 1) {
    const result = await runAudit(args, runIndex, token, instances);
    if (result.issues.length === 0) cleanStreak += 1;
    else cleanStreak = 0;

    lastReport = {
      ok: cleanStreak >= args.cleanRuns,
      createdAt: new Date().toISOString(),
      baseUrl: args.baseUrl,
      cleanRunsTarget: args.cleanRuns,
      cleanStreak,
      runIndex,
      pageCount: result.pages.length,
      issueCount: result.issues.length,
      instances: instances.map((item) => ({ id: item.id, name: item.instance_name, db_type: item.db_type })),
      pages: result.pages,
      issues: result.issues,
    };
    const paths = writeReport(lastReport);
    lastReport.reportPaths = paths;
    console.log(JSON.stringify({
      run: runIndex,
      ok: lastReport.ok,
      issueCount: lastReport.issueCount,
      cleanStreak,
      reports: paths,
    }, null, 2));

    if (lastReport.ok) break;
    if (result.issues.length > 0) break;
  }

  if (!lastReport?.ok) {
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
