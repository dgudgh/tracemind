// 工单与通知不在本仓库内。保留同名入口，是为了让调用方拿到明确原因
// （editionLocked + "此功能暂不支持"），而不是 404 之后只显示"失败"。
const locked = () => Promise.reject(Object.assign(
  new Error("此功能暂不支持"),
  { editionLocked: true, code: "edition_module_unavailable" },
));

export const sendAlertNotification = () => locked();
export const createSourceTicket = () => locked();
export const listItsmConfigs = () => locked();
export const listItsmTickets = () => locked();
export const getItsmBundle = () => locked();
export const getNotifyBundle = () => locked();
