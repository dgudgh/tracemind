// 工单与通知属企业版「集成中心」。开源版保留同名入口，
// 是为了让调用方拿到明确原因（editionLocked），而不是 404 之后只显示"失败"。
const locked = (action) => Promise.reject(Object.assign(
  new Error(`${action}属企业版功能，开源版不可用`),
  { editionLocked: true, code: "edition_module_unavailable" },
));

export const sendAlertNotification = () => locked("告警通知");
export const createSourceTicket = () => locked("创建工单");
export const listItsmConfigs = () => locked("工单配置");
export const listItsmTickets = () => locked("工单查询");
export const getItsmBundle = () => locked("工单中心");
export const getNotifyBundle = () => locked("通知渠道");
