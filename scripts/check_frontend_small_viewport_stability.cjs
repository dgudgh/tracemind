const { existsSync } = require("node:fs");

const DEFAULT_URL = process.env.FRONTEND_URL || "http://127.0.0.1:3300/";
const USERNAME = process.env.MESGUARD_TEST_USER || "admin";
const PASSWORD = process.env.MESGUARD_TEST_PASSWORD || "";

function loadPlaywright() {
  try {
    return require("playwright");
  } catch (error) {
    throw new Error(
      "playwright is required. Run with NODE_PATH pointing at the bundled node_modules, " +
      "or install playwright for this workspace. Original error: " + error.message,
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

async function loginIfNeeded(page) {
  await page.waitForFunction(
    () => Boolean(document.querySelector("#username") || document.querySelector(".admin-shell")),
    null,
    { timeout: 15000 },
  );
  const username = page.locator("#username");
  if (!(await username.count())) return;
  if (!PASSWORD) throw new Error("MESGUARD_TEST_PASSWORD is required when the login page is shown");
  await username.fill(USERNAME);
  await page.locator("#password").fill(PASSWORD);
  await page.getByRole("button", { name: "打开诊断工作台" }).click();
}

async function waitForAdminShell(page) {
  try {
    await page.waitForFunction(() => Boolean(document.querySelector(".admin-shell")), null, { timeout: 15000 });
  } catch (error) {
    const state = await page.evaluate(() => ({
      text: document.body.innerText.slice(0, 800),
      localStorage: { ...localStorage },
      buttons: [...document.querySelectorAll("button")].map((button) => button.innerText).slice(0, 12),
    }));
    error.message += `\nPage state while waiting for .admin-shell:\n${JSON.stringify(state, null, 2)}`;
    throw error;
  }
}

async function sampleViewport(page, viewport) {
  await page.setViewportSize(viewport);
  await page.waitForTimeout(350);

  const samples = [];
  for (let i = 0; i < 40; i += 1) {
    samples.push(await page.evaluate(() => {
      const root = document.documentElement;
      const shell = document.querySelector(".admin-shell");
      const topbar = document.querySelector(".admin-topbar");
      const content = document.querySelector(".admin-content");
      const shellRect = shell?.getBoundingClientRect();
      const topbarRect = topbar?.getBoundingClientRect();
      const contentRect = content?.getBoundingClientRect();
      return {
        appScale: getComputedStyle(root).getPropertyValue("--app-auto-scale").trim(),
        viewportWidth: root.clientWidth,
        docScrollWidth: root.scrollWidth,
        bodyScrollWidth: document.body.scrollWidth,
        shellWidth: shellRect ? Number(shellRect.width.toFixed(2)) : 0,
        topbarHeight: topbarRect ? Number(topbarRect.height.toFixed(2)) : 0,
        contentHeight: contentRect ? Number(contentRect.height.toFixed(2)) : 0,
      };
    }));
    await page.waitForTimeout(50);
  }

  const unique = (key) => [...new Set(samples.map((sample) => sample[key]))];
  const last = samples[samples.length - 1];
  const maxScrollWidth = Math.max(...samples.map((sample) => sample.docScrollWidth));
  const widthJitter = unique("shellWidth").length;
  const topbarJitter = unique("topbarHeight").length;
  const contentJitter = unique("contentHeight").length;
  const failures = [];

  if (maxScrollWidth > viewport.width) {
    failures.push(`document scrollWidth ${maxScrollWidth}px exceeds viewport ${viewport.width}px`);
  }
  if (last.bodyScrollWidth > viewport.width) {
    failures.push(`body scrollWidth ${last.bodyScrollWidth}px exceeds viewport ${viewport.width}px`);
  }
  if (widthJitter > 1) {
    failures.push(`admin shell width changed ${widthJitter} times`);
  }
  if (topbarJitter > 1) {
    failures.push(`topbar height changed ${topbarJitter} times`);
  }
  if (contentJitter > 1) {
    failures.push(`content height changed ${contentJitter} times`);
  }

  return {
    viewport,
    last,
    uniqueShellWidths: unique("shellWidth"),
    uniqueTopbarHeights: unique("topbarHeight"),
    uniqueContentHeights: unique("contentHeight"),
    failures,
  };
}

async function measureShell(page, label) {
  await page.waitForTimeout(900);
  return page.evaluate((sampleLabel) => {
    const root = document.documentElement;
    const shell = document.querySelector(".admin-shell");
    const topbar = document.querySelector(".admin-topbar");
    const content = document.querySelector(".admin-content");
    const rect = (element) => {
      const box = element?.getBoundingClientRect();
      return box ? {
        x: Number(box.x.toFixed(2)),
        y: Number(box.y.toFixed(2)),
        width: Number(box.width.toFixed(2)),
        height: Number(box.height.toFixed(2)),
      } : null;
    };
    return {
      label: sampleLabel,
      title: document.querySelector(".admin-title-block h4")?.innerText || "",
      viewportHeight: root.clientHeight,
      docScrollHeight: root.scrollHeight,
      bodyScrollHeight: document.body.scrollHeight,
      shell: rect(shell),
      topbar: rect(topbar),
      content: rect(content),
      contentClientHeight: content?.clientHeight || 0,
      contentScrollHeight: content?.scrollHeight || 0,
      bodyHasVerticalScroll: root.scrollHeight > root.clientHeight,
      contentHasVerticalScroll: content ? content.scrollHeight > content.clientHeight : false,
    };
  }, label);
}

async function navigateWithSmartCommand(page, query) {
  const input = page.locator(".admin-smart-command input");
  await input.fill(query);
  await input.press("Enter");
}

async function samplePageSwitches(page) {
  await page.setViewportSize({ width: 1000, height: 760 });
  const samples = [await measureShell(page, "start")];
  for (const query of ["数据库实例", "巡检报告", "AWR", "用户管理", "调度任务", "空间管理", "工作台"]) {
    await navigateWithSmartCommand(page, query);
    samples.push(await measureShell(page, query));
  }

  const shellHeights = samples.map((sample) => sample.shell?.height || 0);
  const topbarHeights = samples.map((sample) => sample.topbar?.height || 0);
  const maxShellDelta = Math.max(...shellHeights) - Math.min(...shellHeights);
  const maxTopbarDelta = Math.max(...topbarHeights) - Math.min(...topbarHeights);
  const bodyScrollPages = samples.filter((sample) => sample.bodyHasVerticalScroll).map((sample) => sample.label);
  const failures = [];

  if (maxShellDelta > 2) {
    failures.push(`admin shell height changed by ${maxShellDelta.toFixed(2)}px across page switches`);
  }
  if (maxTopbarDelta > 2) {
    failures.push(`topbar height changed by ${maxTopbarDelta.toFixed(2)}px across page switches`);
  }
  if (bodyScrollPages.length) {
    failures.push(`body scrollbar was used on pages: ${bodyScrollPages.join(", ")}`);
  }

  return { samples, failures };
}

(async () => {
  const { chromium } = loadPlaywright();
  const executablePath = chromeExecutablePath();
  const launchOptions = executablePath ? { executablePath } : {};
  const browser = await chromium.launch({ headless: true, ...launchOptions });
  try {
    const page = await browser.newPage({ viewport: { width: 720, height: 620 } });
    await page.goto(DEFAULT_URL, { waitUntil: "domcontentloaded" });
    await loginIfNeeded(page);
    await waitForAdminShell(page);
    await page.waitForTimeout(1000);

    const results = [];
    for (const viewport of [
      { width: 720, height: 620 },
      { width: 560, height: 700 },
    ]) {
      results.push(await sampleViewport(page, viewport));
    }
    const pageSwitchResult = await samplePageSwitches(page);

    const failures = results.flatMap((result) =>
      result.failures.map((failure) => `${result.viewport.width}x${result.viewport.height}: ${failure}`),
    );
    failures.push(...pageSwitchResult.failures.map((failure) => `page-switch: ${failure}`));
    console.log(JSON.stringify({ smallViewports: results, pageSwitches: pageSwitchResult }, null, 2));
    if (failures.length) {
      console.error("Small viewport stability check failed:");
      for (const failure of failures) console.error(`- ${failure}`);
      process.exit(1);
    }
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
