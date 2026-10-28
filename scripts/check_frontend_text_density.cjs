const { existsSync, mkdirSync, writeFileSync } = require("node:fs");
const path = require("node:path");
const { loadAuditMenuPages, loadOpsCapabilities } = require("./frontend_audit_pages.cjs");

const DEFAULT_FRONTEND_URL = process.env.FRONTEND_URL || process.env.FRONTEND_AUDIT_URL || "http://127.0.0.1:3300/";
const DEFAULT_BACKEND_URL = process.env.BACKEND_URL || "";
const USERNAME = process.env.MESGUARD_TEST_USER || "admin";
const PASSWORD = process.env.MESGUARD_TEST_PASSWORD || "";
const REPORT_DIR = process.env.FRONTEND_TEXT_DENSITY_REPORT_DIR || path.resolve(__dirname, "../reports/frontend-text-density");

const PAGES = loadAuditMenuPages();
const OPS_CAPABILITIES = loadOpsCapabilities();

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
    pageDelayMs: Number.parseInt(process.env.FRONTEND_TEXT_DENSITY_PAGE_DELAY_MS || "750", 10),
    viewports: (process.env.FRONTEND_TEXT_DENSITY_VIEWPORTS || "1225x900,900x720")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean)
      .map((item) => {
        const [width, height] = item.split("x").map((value) => Number.parseInt(value, 10));
        return { width, height };
      })
      .filter((item) => item.width > 0 && item.height > 0),
    headed: process.env.FRONTEND_TEXT_DENSITY_HEADED === "1",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === "--base-url") {
      args.baseUrl = argv[index + 1].replace(/\/$/, "");
      index += 1;
    } else if (item === "--backend-url") {
      args.backendUrl = argv[index + 1].replace(/\/$/, "");
      index += 1;
    } else if (item === "--viewports") {
      args.viewports = argv[index + 1]
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean)
        .map((value) => {
          const [width, height] = value.split("x").map((part) => Number.parseInt(part, 10));
          return { width, height };
        })
        .filter((value) => value.width > 0 && value.height > 0);
      index += 1;
    } else if (item === "--headed") {
      args.headed = true;
    } else if (item === "-h" || item === "--help") {
      console.log("Usage: node scripts/check_frontend_text_density.cjs [--base-url URL] [--viewports 1225x900,900x720] [--headed]");
      process.exit(0);
    }
  }
  if (!args.viewports.length) args.viewports = [{ width: 1225, height: 900 }];
  return args;
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
  if (!PASSWORD) throw new Error("MESGUARD_TEST_PASSWORD is required for the text-density audit");
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

async function navigateToEntry(page, entry, args) {
  const command = `打开 ${entry.engine || ""} ${entry.key} ${entry.label}`.replace(/\s+/g, " ").trim();
  await page.locator(".admin-smart-command input").fill(command);
  await page.locator(".admin-smart-command input").press("Enter");
  await page.waitForTimeout(args.pageDelayMs);
  await page.waitForFunction(() => Boolean(document.querySelector(".admin-shell .admin-content")), null, { timeout: 15000 });
}

function createEntries(instances) {
  const engineSet = new Set(instances.map((item) => String(item.db_type || "").toLowerCase()).filter(Boolean));
  const entries = PAGES.filter((entry) => !entry.engines?.length || !engineSet.size || entry.engines.some((engine) => engineSet.has(engine)));
  for (const [capability, label, engines] of OPS_CAPABILITIES) {
    const available = engines.filter((engine) => engineSet.has(engine));
    for (const engine of available.length ? available : [""]) {
      entries.push({ key: `ops.${capability}`, label, engine });
    }
  }
  return entries;
}

function issue(entry, viewport, type, message, detail = {}) {
  return {
    pageKey: entry.key,
    pageLabel: entry.label,
    engine: entry.engine || "",
    viewport: `${viewport.width}x${viewport.height}`,
    type,
    message,
    detail,
  };
}

async function collectDensity(page, entry, viewport) {
  await page.setViewportSize(viewport);
  await page.waitForTimeout(300);
  return page.evaluate((expected) => {
    const visible = (element) => {
      if (!element || !(element instanceof HTMLElement)) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
    };
    const cleanText = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const rectInfo = (element) => {
      const rect = element.getBoundingClientRect();
      return {
        x: Number(rect.x.toFixed(1)),
        y: Number(rect.y.toFixed(1)),
        width: Number(rect.width.toFixed(1)),
        height: Number(rect.height.toFixed(1)),
        right: Number(rect.right.toFixed(1)),
        bottom: Number(rect.bottom.toFixed(1)),
      };
    };
    const textOf = (element) => cleanText(element.innerText || element.getAttribute("aria-label") || element.getAttribute("title") || element.getAttribute("placeholder"));
    const ignored = (element) => Boolean(element.closest(".ant-table, .ant-dropdown, .ant-tooltip, .ant-select-dropdown, .ant-modal-mask, .ant-picker-dropdown"));
    const describe = (element) => ({
      tag: element.tagName.toLowerCase(),
      text: textOf(element).slice(0, 90),
      className: String(element.className || "").slice(0, 160),
      rect: rectInfo(element),
    });
    const issues = [];

    const clippedSelectors = [
      ["button", "button-clipped", false],
      [".ant-tag", "tag-clipped", false],
      [".inspection-template-pill, .inspection-policy-pill", "pill-clipped", false],
      [".ant-segmented-item-label", "segmented-clipped", false],
      [".ant-tabs-tab-btn", "tab-clipped", false],
      [".ant-statistic-title, .ant-statistic-content", "statistic-clipped", false],
      [".ant-card-head-title", "card-title-clipped", true],
      [".inspection-template-card-metric strong, .inspection-policy-card-metric strong", "metric-clipped", false],
    ];

    for (const [selector, type, allowEllipsis] of clippedSelectors) {
      const matches = [...document.querySelectorAll(selector)]
        .filter(visible)
        .filter((element) => !ignored(element))
        .filter((element) => textOf(element).length > 1)
        .filter((element) => {
          const style = getComputedStyle(element);
          const overflowX = element.scrollWidth > element.clientWidth + 2;
          const overflowY = element.scrollHeight > element.clientHeight + 2;
          if (!(overflowX || overflowY)) return false;
          if (allowEllipsis && style.textOverflow === "ellipsis") return false;
          return true;
        })
        .map((element) => ({
          ...describe(element),
          scrollWidth: element.scrollWidth,
          clientWidth: element.clientWidth,
          scrollHeight: element.scrollHeight,
          clientHeight: element.clientHeight,
        }))
        .slice(0, 8);
      if (matches.length) issues.push({ type, message: "文字或图标被裁切", elements: matches });
    }

    const cardSelectors = [
      ".inspection-template-card",
      ".inspection-policy-card",
      ".workbench-card",
      ".backup-card",
      ".diagnosis-quick-card",
      ".ant-card",
    ];
    const narrowCards = [...document.querySelectorAll(cardSelectors.join(","))]
      .filter(visible)
      .filter((element) => !ignored(element))
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        const text = cleanText(element.innerText);
        const compactControls = element.querySelectorAll("button, .ant-tag, .ant-statistic, .ant-badge, .inspection-template-pill, .inspection-policy-pill").length;
        return rect.width < 320 && text.length > 80 && compactControls >= 4;
      })
      .map(describe)
      .slice(0, 8);
    if (narrowCards.length) {
      issues.push({ type: "narrow-card", message: "信息卡片宽度不足，容易造成文字拥挤", elements: narrowCards });
    }

    const containers = [
      ".admin-topbar",
      ".ant-card-head",
      ".ant-alert",
      ".inspection-template-card-header",
      ".inspection-policy-card-header",
      ".workbench-hero",
      ".workbench-kpi-card",
      ".diagnosis-quick-grid",
      ".inspection-quick-grid",
      ".ant-space",
    ];
    const overlaps = [];
    for (const container of [...document.querySelectorAll(containers.join(","))].filter(visible).filter((element) => !ignored(element))) {
      const children = [...container.children]
        .filter(visible)
        .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
        .filter((item) => item.text.length > 0 || item.element.querySelector("button, .ant-tag, .ant-badge, .ant-statistic"));
      for (let i = 0; i < children.length; i += 1) {
        for (let j = i + 1; j < children.length; j += 1) {
          const a = children[i];
          const b = children[j];
          const verticalOverlap = Math.min(a.rect.bottom, b.rect.bottom) - Math.max(a.rect.top, b.rect.top);
          const horizontalOverlap = Math.min(a.rect.right, b.rect.right) - Math.max(a.rect.left, b.rect.left);
          const minHeight = Math.min(a.rect.height, b.rect.height);
          if (verticalOverlap > Math.max(4, minHeight * 0.45) && horizontalOverlap > 2) {
            overlaps.push({
              container: describe(container),
              a: describe(a.element),
              b: describe(b.element),
              overlapPx: Number(horizontalOverlap.toFixed(1)),
            });
          }
        }
      }
      if (overlaps.length >= 8) break;
    }
    if (overlaps.length) {
      issues.push({ type: "element-overlap", message: "同一行元素发生重叠", elements: overlaps.slice(0, 8) });
    }

    const shortLineHeight = [...document.querySelectorAll(".admin-content button, .admin-content .ant-tag, .admin-content .ant-typography, .admin-content .ant-card-head-title")]
      .filter(visible)
      .filter((element) => !ignored(element))
      .filter((element) => textOf(element).length > 2)
      .filter((element) => {
        const style = getComputedStyle(element);
        const fontSize = Number.parseFloat(style.fontSize);
        const lineHeight = style.lineHeight === "normal" ? fontSize * 1.2 : Number.parseFloat(style.lineHeight);
        return Number.isFinite(fontSize) && Number.isFinite(lineHeight) && lineHeight < fontSize * 1.08;
      })
      .map((element) => {
        const style = getComputedStyle(element);
        return { ...describe(element), fontSize: style.fontSize, lineHeight: style.lineHeight };
      })
      .slice(0, 8);
    if (shortLineHeight.length) {
      issues.push({ type: "line-height-tight", message: "文字行高过小，视觉上会显得拥挤", elements: shortLineHeight });
    }

    return {
      expected,
      title: document.querySelector(".admin-title-block h4")?.innerText || "",
      bodyOverflowX: document.body.scrollWidth - document.body.clientWidth,
      contentOverflowX: (document.querySelector(".admin-content")?.scrollWidth || 0) - (document.querySelector(".admin-content")?.clientWidth || 0),
      issues,
    };
  }, { key: entry.key, label: entry.label, engine: entry.engine || "" });
}

function timestamp() {
  return new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "Z");
}

function normalizeIssueKey(item) {
  return [item.pageKey, item.engine, item.viewport, item.type, item.message, JSON.stringify(item.detail).slice(0, 240)].join(" | ");
}

function writeReport(report) {
  mkdirSync(REPORT_DIR, { recursive: true });
  const base = `frontend-text-density-${timestamp()}`;
  const jsonPath = path.join(REPORT_DIR, `${base}.json`);
  const markdownPath = path.join(REPORT_DIR, `${base}.md`);
  writeFileSync(jsonPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  const lines = [
    "# 前端文字拥挤巡检报告",
    "",
    `- 时间：${report.createdAt}`,
    `- 前端：${report.baseUrl}`,
    `- 页面数：${report.pageCount}`,
    `- 视口：${report.viewports.join("、")}`,
    `- 问题数：${report.issueCount}`,
    "",
    "## 问题清单",
    "",
  ];
  if (!report.issues.length) {
    lines.push("未发现文字拥挤、重叠或裁切问题。");
  } else {
    report.issues.forEach((item, index) => {
      const name = item.engine ? `${item.pageLabel}（${item.engine}）` : item.pageLabel;
      lines.push(`${index + 1}. ${name} / ${item.viewport} / ${item.type}：${item.message}`);
      lines.push(`   - 详情：\`${JSON.stringify(item.detail).slice(0, 700)}\``);
    });
  }
  lines.push("");
  lines.push("## 覆盖页面");
  lines.push("");
  report.pages.forEach((pageResult) => {
    const name = pageResult.engine ? `${pageResult.pageLabel}（${pageResult.engine}）` : pageResult.pageLabel;
    lines.push(`- ${name}：${pageResult.issueCount ? `${pageResult.issueCount} 个问题` : "通过"}`);
  });
  writeFileSync(markdownPath, `${lines.join("\n")}\n`, "utf8");
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

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const token = await loginViaApi(args);
  const instances = await fetchInstances(args, token);
  const entries = createEntries(instances);
  const { chromium } = loadPlaywright();
  const executablePath = chromeExecutablePath();
  const browser = await chromium.launch({ headless: !args.headed, ...(executablePath ? { executablePath } : {}) });
  const pages = [];
  try {
    const page = await browser.newPage({ viewport: args.viewports[0] });
    await page.goto(args.baseUrl, { waitUntil: "domcontentloaded" });
    await loginIfNeeded(page);
    await page.waitForFunction(() => Boolean(document.querySelector(".admin-shell")), null, { timeout: 20000 });
    for (const entry of entries) {
      process.stdout.write(`文字巡检: ${entry.engine ? `${entry.engine} ` : ""}${entry.label} ... `);
      try {
        await navigateToEntry(page, entry, args);
        const issueItems = [];
        for (const viewport of args.viewports) {
          const density = await collectDensity(page, entry, viewport);
          if (density.bodyOverflowX > 2 || density.contentOverflowX > 2) {
            issueItems.push(issue(entry, viewport, "horizontal-overflow", "页面或内容区域产生横向溢出", {
              bodyOverflowX: density.bodyOverflowX,
              contentOverflowX: density.contentOverflowX,
            }));
          }
          for (const item of density.issues) {
            issueItems.push(issue(entry, viewport, item.type, item.message, { elements: item.elements }));
          }
        }
        const uniqueIssues = [...new Map(issueItems.map((item) => [normalizeIssueKey(item), item])).values()];
        pages.push({ pageKey: entry.key, pageLabel: entry.label, engine: entry.engine || "", issueCount: uniqueIssues.length, issues: uniqueIssues });
        console.log(uniqueIssues.length ? `${uniqueIssues.length} issue(s)` : "ok");
      } catch (error) {
        const failed = issue(entry, args.viewports[0], "audit-failed", "页面文字巡检失败", { error: error.message });
        pages.push({ pageKey: entry.key, pageLabel: entry.label, engine: entry.engine || "", issueCount: 1, issues: [failed] });
        console.log("failed");
      }
    }
  } finally {
    await closeBrowserWithTimeout(browser);
  }
  const issues = [...new Map(pages.flatMap((item) => item.issues).map((item) => [normalizeIssueKey(item), item])).values()];
  const report = {
    createdAt: new Date().toISOString(),
    baseUrl: args.baseUrl,
    viewports: args.viewports.map((item) => `${item.width}x${item.height}`),
    pageCount: pages.length,
    issueCount: issues.length,
    pages,
    issues,
  };
  const reports = writeReport(report);
  console.log(JSON.stringify({ ok: !issues.length, pageCount: report.pageCount, issueCount: issues.length, reports }, null, 2));
  if (issues.length) process.exit(1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
