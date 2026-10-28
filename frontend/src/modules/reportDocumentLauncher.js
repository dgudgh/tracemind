const CAPABILITY_ID = "report.document.generate";

export function reportDocumentLaunchState(items, instances, instanceId) {
  const target = Array.isArray(instances)
    ? instances.find((item) => item?.id === instanceId && Number.isSafeInteger(item.id) && item.id > 0)
    : null;
  if (!target) return { capability: null, target: null, reason: "请先在顶部选择报告目标实例" };
  const candidates = Array.isArray(items) ? items.filter((item) => item?.capability_id === CAPABILITY_ID) : [];
  if (candidates.length !== 1) return { capability: null, target, reason: "当前账号没有可用的分析报告生成能力" };
  const capability = candidates[0];
  const valid = capability.enabled === true && capability.availability === "available"
    && capability.execution === "server" && capability.handler_status === "wired"
    && capability.plan_executable === true && capability.kind === "collect"
    && capability.requires_instance === true && capability.version === 3 && capability.definition_version === 3
    && capability.output_schema === "report.document_result/v1"
    && Array.isArray(capability.authorized_engines)
    && capability.authorized_engines.includes(target.db_type);
  return valid ? { capability, target, reason: "" }
    : { capability: null, target, reason: "分析报告能力当前不可用或不支持所选实例" };
}
