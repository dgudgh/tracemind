/**
 * 版本门禁的前端降级工具。
 *
 * 开源版访问企业版接口会拿到 `403 {detail:{code:"edition_*"}}`，`api.js` 已把它标记成
 * `error.editionLocked`。这类 403 的性质是「该功能不在本版本」，不是「这次请求出错」：
 *
 * - 不能让 `Promise.all` 整体 reject，否则整页只剩一条「需要企业版授权」的报错弹窗，
 *   用户既看不到本版本可用的数据，也分不清是故障还是版本差异；
 * - 也不该静默吞掉——页面需要知道哪一块被锁了，才能就地说明而不是显示一个空表格。
 *
 * 因此统一转成 `{ items: [], editionLocked: true }`：调用方照常取值，另用
 * `isEditionLocked(...)` 判断要不要渲染「企业版功能」提示。
 */
export const optionalEditionCall = (promise) =>
  promise.catch((error) => {
    if (error?.editionLocked) return { items: [], editionLocked: true };
    throw error;
  });

export const isEditionLocked = (...parts) => parts.some((part) => part?.editionLocked === true);
