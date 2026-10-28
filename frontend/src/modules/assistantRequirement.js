const REASON_TEXT = {
  ai_not_configured: "未配置可用模型，请手动选择功能。",
  ai_unavailable: "模型暂不可用，请手动选择功能。",
  budget_exhausted: "模型预算已用完，请手动选择功能。",
  no_capability: "当前没有可用能力，请手动选择功能。",
  no_match: "没有匹配到已登记能力，请手动选择功能。",
  low_confidence: "需求匹配不确定，请手动选择功能。",
  invalid_output: "模型返回了不可执行的结果，请手动选择功能。",
  invalid_params: "需要补充或修正能力参数。",
  missing_instance: "需要先选择目标实例。",
};

// Natural-language requests always try server-side model dispatch first.
// A failed/unavailable model may suggest a page, but never starts client actions.
export async function resolveCustomerRequirement({ text, instanceId, requestId: providedRequestId,
  answers = {}, recoverOnly = false, dispatch, readReceipt, resolveNavigation }) {
  let result;
  const requestId = providedRequestId || globalThis.crypto?.randomUUID?.() || `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  try {
    try {
      if (recoverOnly) {
        result = readReceipt ? await readReceipt(requestId) : null;
      } else {
        result = await dispatch({ text, instance_id: instanceId || null, request_id: requestId,
          ...(Object.keys(answers).length ? { answers } : {}) });
      }
      result = { ...(result || {}), requestId };
    } catch (dispatchError) {
      if (recoverOnly && [401, 403].includes(dispatchError?.status)) throw dispatchError;
      if (!recoverOnly && dispatchError?.status >= 400 && dispatchError.status < 500) throw dispatchError;
      // The POST may have reached the server before the network dropped. Re-query the
      // receipt by request id instead of re-calling the model.
      result = !recoverOnly && readReceipt ? await readReceipt(requestId).catch(() => null) : null;
      if (result === null) {
        if (!recoverOnly && dispatchError?.status >= 400) throw dispatchError;
        return { mode: "pending", requestId, reason: "请求状态尚未确认，正在保留原编号。请回查原任务，不要重复提交。" };
      }
      result = { ...result, requestId };
    }
  } catch (error) {
    // Auth, permissions, validation and execution failures are real errors.
    // Only an unreachable transport falls back without a server response.
    if (error?.status >= 400) {
      const labels = {
        authentication_required: "登录状态已失效，请重新登录。",
        permission_denied: "当前账号没有该操作权限。",
        provider_unavailable: "模型服务暂不可用，请稍后重试或手动选择功能。",
        capability_not_authorized: "该能力未登记或当前账号无权使用。",
        stale_definition_version: "能力版本已变化，请刷新后重试。",
        idempotency_conflict: "请求编号已被其他内容使用，请重新提交。",
      };
      const raw = String(error.code || error.message || "");
      const key = Object.keys(labels).find((candidate) => raw.includes(candidate));
      const label = labels[key];
      if (label) throw Object.assign(new Error(label), { status: error.status, code: error.code });
      throw error;
    }
    return { mode: "pending", requestId, reason: "调度连接中断，请使用原请求编号回查任务。" };
  }
  if (result?.action === "failed") {
    throw Object.assign(new Error(result.reason || "原请求处理失败，请查看原因后发起新任务"),
      { status: 409, code: result.reason_code || "dispatch_failed", requestId });
  }
  if (result?.action === "pending" || result?.reason_code === "request_in_progress" || recoverOnly && !result?.action) {
    return { mode: "pending", requestId, reason: result.reason || "原请求正在处理，请回查状态。",
      planId: result.plan?.plan_id };
  }
  if (result?.action === "clarify") {
    return { mode: "clarify", requestId, questions: result.questions || [], answers: result.answers || answers,
      goal: result.goal, reason: result.reason };
  }
  if (result?.action === "batch" && result.batch?.batch_id) {
    return { mode: "batch", batchId: result.batch.batch_id, reason: result.reason, requestId: result.requestId };
  }
  if (["executed", "confirm", "workflow"].includes(result?.action) && result.plan?.plan_id) {
    return { mode: "plan", capability: result.capability, planId: result.plan.plan_id, requestId: result.requestId };
  }
  if (result?.action === "navigate" && result.source === "ai" && result.capability) {
    return { mode: "input", capability: result.capability, params: result.params || {},
      reason: REASON_TEXT[result.reason_code] || result.reason, requestId: result.requestId };
  }
  const localNavigation = resolveNavigation(text);
  const navigation = result?.target_key
    ? { ...localNavigation, targetKey: result.target_key, title: result.reason || localNavigation?.title }
    : localNavigation;
  return {
    mode: "manual",
    requestId,
    navigation,
    reason: REASON_TEXT[result?.reason_code] || result?.reason || (result?.source === "ai"
      ? "需求未形成可执行计划，请在功能页面手动操作。"
      : result
        ? "AI 未配置或暂不可用，请在功能页面手动操作。"
        : "需求调度连接中断，请先在任务清单核对结果，再手动操作。"),
  };
}
