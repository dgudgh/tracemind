const DEFAULT_STEPS = [
  "先核对当前目标实例或操作范围。",
  "按本页要求填写必要参数。",
  "检查参数后，点击本页相应按钮继续。",
];

const PAGE_STEPS = {
  "perf.slowsql": [
    "核对要分析的数据库实例。",
    "在本页选择时间范围并填写查询条件。",
    "根据需要使用本页的查询或采集操作。",
  ],
  "report.unified": [
    "核对所需的报告范围。",
    "选择报告类型，并填写本页所需参数。",
    "检查参数后，使用本页的报告操作继续。",
  ],
};

function findMenu(items, key) {
  if (!Array.isArray(items)) return null;
  for (const item of items) {
    if (item?.key === key) return item;
    const child = findMenu(item?.children, key);
    if (child) return child;
  }
  return null;
}

function instanceKey(value) {
  return value === undefined || value === null || value === "" ? null : String(value);
}

// This is navigation context only. It never becomes a capability result or an execution payload.
export function createManualRequirementHandoff(payload, { targetKey, instanceId, menuItems, session }) {
  if (payload?.kind !== "manual_requirement" || payload.targetKey !== targetKey
    || typeof targetKey !== "string" || !/^[a-z][a-zA-Z0-9.]{0,99}$/.test(targetKey)
    || typeof payload.text !== "string" || !payload.text.trim() || payload.text.length > 2000
    || !session?.token || !session?.loginEpoch) return null;
  const menu = findMenu(menuItems, targetKey);
  if (!menu || menu.children?.length) return null;
  return {
    targetKey,
    instanceId: instanceKey(instanceId),
    text: payload.text.trim(),
    title: typeof menu.label === "string" ? menu.label : "当前功能页",
    steps: PAGE_STEPS[targetKey] || DEFAULT_STEPS,
    session: { token: session.token, generation: session.generation, loginEpoch: session.loginEpoch },
  };
}

export function manualRequirementHandoffMatches(handoff, { activeKey, instanceId, session, menuItems }) {
  return Boolean(handoff && handoff.targetKey === activeKey && handoff.instanceId === instanceKey(instanceId)
    && session?.token && handoff.session.token === session.token
    && handoff.session.generation === session.generation && handoff.session.loginEpoch === session.loginEpoch
    && findMenu(menuItems, activeKey));
}
