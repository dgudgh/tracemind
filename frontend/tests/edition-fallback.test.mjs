import assert from "node:assert/strict";
import test from "node:test";

import { isEditionLocked, optionalEditionCall } from "../src/modules/editionFallback.js";

const editionError = () => Object.assign(new Error("当前为开源版，该模块需要企业版授权"), {
  status: 403,
  code: "edition_module_unavailable",
  editionLocked: true,
});

test("edition-locked calls degrade to an empty, flagged result", async () => {
  const value = await optionalEditionCall(Promise.reject(editionError()));
  assert.deepEqual(value, { items: [], editionLocked: true });
  assert.equal(isEditionLocked(value), true);
});

test("resolved calls pass through untouched", async () => {
  const payload = { items: [{ id: 1 }], total: 1 };
  assert.deepEqual(await optionalEditionCall(Promise.resolve(payload)), payload);
  assert.equal(isEditionLocked(payload), false);
});

test("non-edition failures still reject", async () => {
  // 只降级版本限制：网络错误、500、真正的权限问题必须继续冒泡，
  // 否则页面会把故障显示成「没有数据」。
  for (const error of [
    Object.assign(new Error("boom"), { status: 500 }),
    Object.assign(new Error("denied"), { status: 403, code: "permission_denied", editionLocked: false }),
    new Error("network"),
  ]) {
    await assert.rejects(optionalEditionCall(Promise.reject(error)), (thrown) => thrown === error);
  }
});

test("isEditionLocked is true when any part of a bundle is locked", () => {
  assert.equal(isEditionLocked({ items: [] }, { items: [], editionLocked: true }), true);
  assert.equal(isEditionLocked(undefined, null, { items: [] }), false);
});
