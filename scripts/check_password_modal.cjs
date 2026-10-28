const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright");

async function waitForModalPaint(page) {
  await page.waitForFunction(() => {
    const modal = document.querySelector(".ant-modal");
    if (!modal || !modal.clientWidth) return false;
    for (let node = modal; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (Number(style.opacity) < 0.99 || style.visibility !== "visible") return false;
    }
    return modal.getAnimations({ subtree: true }).every((animation) => animation.playState !== "running");
  });
}

// Isolated browser with mocked APIs: never submits credentials to the live database.
async function main() {
  const artifacts = fs.mkdtempSync(path.join(os.tmpdir(), "password-modal-qa-"));
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  let page;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
    context.setDefaultTimeout(10000);
    await context.addInitScript(() => localStorage.setItem("mesguard_token", "isolated-ui-test"));
    page = await context.newPage();
    const errors = [];
    const resets = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const pathname = new URL(request.url()).pathname;
      let body = { items: [] };
      let status = 200;
      if (pathname === "/api/v1/system/init/status") body = { initialized: true };
      if (pathname === "/api/v1/setup/license") body = { valid: true };
      if (pathname === "/api/v1/auth/current-user") body = { id: 1, username: "admin.test", role: "admin", source: "db" };
      if (pathname === "/api/v1/users") body = { items: [
        { id: 1, username: "admin.test", role: "admin", status: "active" },
        { id: 2, username: "operator.test", role: "user", status: "active" },
      ] };
      if (pathname === "/api/v1/users/password-policy") body = { enforce_complexity: true };
      if (pathname.endsWith("/reset-password")) {
        assert.equal(request.method(), "PUT");
        const payload = request.postDataJSON();
        resets.push({ pathname, payload });
        await new Promise((resolve) => setTimeout(resolve, 250));
        if (payload.current_password !== "OperatorCurrent1") {
          status = 400;
          body = { detail: "当前密码不正确" };
        } else {
          body = { detail: "密码已更新", reauthenticate: pathname.includes("/users/1/") };
        }
      }
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.goto(process.env.PASSWORD_QA_URL || "http://127.0.0.1:3300/");
    await page.locator(".admin-topbar-left button").click();
    await page.getByRole("menuitem", { name: /平台管理/ }).click();
    await page.getByRole("menuitem", { name: /系统管理/ }).click();
    await page.getByRole("menuitem", { name: /用户管理/ }).click();
    await page.getByRole("button", { name: "修改密码", exact: true }).click();
    let dialog = page.getByRole("dialog", { name: "修改密码 - admin.test" });
    await dialog.waitFor();
    await waitForModalPaint(page);
    await page.screenshot({ path: path.join(artifacts, "self-desktop.png") });
    await dialog.getByPlaceholder("新密码", { exact: true }).fill("NewPassword2");
    await dialog.getByPlaceholder("再次输入新密码").fill("NewPassword2");
    await dialog.getByRole("button", { name: "确认修改" }).click();
    await dialog.getByText("请输入当前登录账号的密码", { exact: true }).waitFor();
    assert.equal(resets.length, 0);
    await dialog.getByPlaceholder("当前登录账号的密码").fill("Incorrect1");
    await dialog.getByRole("button", { name: "确认修改" }).click();
    await page.getByText("当前密码不正确", { exact: true }).waitFor();
    assert.equal(await dialog.getByPlaceholder("当前登录账号的密码").inputValue(), "");
    await dialog.getByRole("button", { name: /取\s*消/ }).click();
    await dialog.waitFor({ state: "hidden" });
    await page.getByRole("button", { name: "修改密码", exact: true }).click();
    await dialog.waitFor();
    for (const input of await dialog.locator("input").all()) assert.equal(await input.inputValue(), "");
    await dialog.getByRole("button", { name: /取\s*消/ }).click();
    await dialog.waitFor({ state: "hidden" });
    await page.getByRole("button", { name: "重置密码", exact: true }).click();
    dialog = page.getByRole("dialog", { name: "重置密码 - operator.test" });
    await dialog.getByText("操作管理员当前密码（admin.test）", { exact: true }).waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    await waitForModalPaint(page);
    const bounds = await dialog.boundingBox();
    assert.ok(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= 391 && bounds.y + bounds.height <= 845, JSON.stringify(bounds));
    await page.screenshot({ path: path.join(artifacts, "administrator-mobile.png") });
    await dialog.getByPlaceholder("当前登录账号的密码").fill("OperatorCurrent1");
    await dialog.getByPlaceholder("新密码", { exact: true }).fill("NewPassword2");
    await dialog.getByPlaceholder("再次输入新密码").fill("NewPassword2");
    await dialog.getByRole("button", { name: "确认重置" }).click();
    await dialog.waitFor({ state: "hidden" });
    assert.equal(resets.length, 2);
    assert.deepEqual(Object.keys(resets[1].payload).sort(), ["current_password", "new_password"]);
    assert.equal(await page.evaluate(() => localStorage.getItem("mesguard_token")), "isolated-ui-test");
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.getByRole("button", { name: "修改密码", exact: true }).click();
    dialog = page.getByRole("dialog", { name: "修改密码 - admin.test" });
    await dialog.getByPlaceholder("当前登录账号的密码").fill("OperatorCurrent1");
    await dialog.getByPlaceholder("新密码", { exact: true }).fill("NewPassword2");
    await dialog.getByPlaceholder("再次输入新密码").fill("NewPassword2");
    await dialog.getByRole("button", { name: "确认修改" }).click();
    await page.getByRole("button", { name: "打开诊断工作台", exact: true }).waitFor();
    assert.equal(await page.evaluate(() => localStorage.getItem("mesguard_token")), null);
    assert.equal(resets.length, 3);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ status: "passed", mocked_api: true, real_password_changes: 0, artifacts }));
  } catch (error) {
    if (page) {
      console.error((await page.locator("body").innerText()).slice(0, 2400));
      await page.screenshot({ path: path.join(artifacts, "failure.png") });
      console.error(artifacts);
    }
    throw error;
  } finally {
    await browser.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
