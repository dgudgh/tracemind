import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "..");
const sourceRoot = path.join(frontendRoot, "src");
const stylesSource = fs.readFileSync(path.join(sourceRoot, "styles.css"), "utf8");

function jsxFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const absolutePath = path.join(directory, entry.name);
    if (entry.isDirectory()) return jsxFiles(absolutePath);
    return entry.isFile() && entry.name.endsWith(".jsx") ? [absolutePath] : [];
  });
}

test("all modal dialogs use the centered viewport-safe layout policy", () => {
  const requiredRules = [
    ".ant-modal-root .ant-modal-wrap",
    "place-items: center",
    ".ant-modal-root .ant-modal-content",
    "max-height: calc(100dvh - 48px)",
    ".ant-modal-root .ant-modal-body",
    "overscroll-behavior: contain",
  ];

  for (const rule of requiredRules) {
    assert.ok(stylesSource.includes(rule), `missing modal layout rule: ${rule}`);
  }
});

test("business details and forms do not use side drawers", () => {
  const allowedDrawerFile = path.join(sourceRoot, "framework", "AdminShell.jsx");
  const violations = jsxFiles(sourceRoot)
    .filter((file) => file !== allowedDrawerFile)
    .filter((file) => /<Drawer\b/.test(fs.readFileSync(file, "utf8")))
    .map((file) => path.relative(frontendRoot, file));

  assert.deepEqual(violations, []);

  const navigationSource = fs.readFileSync(allowedDrawerFile, "utf8");
  assert.equal(
    (navigationSource.match(/<Drawer\b/g) || []).length,
    2,
    "only the mobile navigation and expert tools may remain side drawers",
  );
});
