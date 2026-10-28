import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";

import {
  findMenuKeyPath,
  resolveAccordionOpenKeys,
} from "../src/modules/menuNavigation.js";

const menuItems = [
  { key: "home.dashboard", label: "工作台" },
  {
    key: "inspect",
    label: "巡检",
    children: [
      { key: "inspect.overview", label: "巡检总览" },
      {
        key: "inspect.results",
        label: "任务与报告",
        children: [
          { key: "inspect.reports", label: "巡检报告" },
        ],
      },
    ],
  },
  {
    key: "perf",
    label: "性能分析",
    children: [
      { key: "perf.overview", label: "性能总览" },
    ],
  },
];

test("findMenuKeyPath returns the complete path for a nested page", () => {
  assert.deepEqual(
    findMenuKeyPath(menuItems, "inspect.reports"),
    ["inspect", "inspect.results", "inspect.reports"],
  );
});

test("findMenuKeyPath returns an empty path for an unknown page", () => {
  assert.deepEqual(findMenuKeyPath(menuItems, "missing.page"), []);
});

test("opening another root group closes the previous root group", () => {
  assert.deepEqual(
    resolveAccordionOpenKeys(menuItems, ["inspect", "inspect.results"], ["inspect", "inspect.results", "perf"]),
    ["perf"],
  );
});

test("opening a nested group keeps its root group open", () => {
  assert.deepEqual(
    resolveAccordionOpenKeys(menuItems, ["inspect"], ["inspect", "inspect.results"]),
    ["inspect", "inspect.results"],
  );
});

test("closing the active root group clears its nested open keys", () => {
  assert.deepEqual(
    resolveAccordionOpenKeys(menuItems, ["inspect", "inspect.results"], []),
    [],
  );
});

test("smart navigation only recommends menus visible to the current user", async () => {
  const vite = await createServer({
    root: new URL("..", import.meta.url).pathname, appType: "custom",
    server: { middlewareMode: true, hmr: false }, logLevel: "silent",
  });
  try {
    const { resolveSmartNavigation } = await vite.ssrLoadModule("/src/modules/smartNavigation.js");
    const visible = [{ key: "home.dashboard", label: "工作台", icon: null }];
    assert.equal(resolveSmartNavigation("用户管理", { menuItems: visible }), null);
    const hit = resolveSmartNavigation("工作台", { menuItems: visible });
    assert.equal(hit?.targetKey, "home.dashboard");
  } finally {
    await vite.close();
  }
});
