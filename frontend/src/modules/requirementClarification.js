const empty = (value) => value === undefined || value === null || (typeof value === "string" && !value.trim());
const answerKey = /^[a-z][a-z0-9_]{0,39}$/;
const INSTANCE_KEYS = new Set(["instance", "instance_id", "selected_instance_id"]);

export function normalizeClarificationAnswers(value, questions = []) {
  if (value === undefined || value === null) return { answers: {} };
  if (typeof value !== "object" || Array.isArray(value)) return { error: "已有补充信息格式无效，请取消后重新发起" };
  const entries = Object.entries(value);
  const instanceKeys = new Set([...INSTANCE_KEYS,
    ...questions.filter((question) => question?.type === "instance").map((question) => question.key)]);
  const answers = {};
  for (const [key, raw] of entries) {
    if (instanceKeys.has(key)) continue;
    if (Object.keys(answers).length >= 10) return { error: "已有补充信息过多，请取消后重新发起" };
    if (!answerKey.test(key) || ["__proto__", "prototype", "constructor"].includes(key)) {
      return { error: "已有补充信息包含无效字段，请取消后重新发起" };
    }
    if (typeof raw === "string") {
      const text = raw.trim();
      if (!text || text.length > 2000) return { error: `已有补充信息无效：${key}` };
      answers[key] = text;
    } else if (typeof raw === "number" && Number.isSafeInteger(raw)) {
      answers[key] = raw;
    } else if (typeof raw === "boolean") {
      answers[key] = raw;
    } else {
      return { error: `已有补充信息无效：${key}` };
    }
  }
  return { answers };
}

export function buildClarificationSubmission({ questions = [], values = {}, initialAnswers = {}, selectedInstanceId, instances = [] }) {
  const normalized = normalizeClarificationAnswers(initialAnswers, questions);
  if (normalized.error) return normalized;
  const missing = questions.find((question) => question.required && empty(values[question.key]));
  if (missing) return { error: `请补充：${missing.label}` };
  const answers = { ...normalized.answers };
  let instanceId = selectedInstanceId || null;
  for (const question of questions) {
    const value = values[question.key];
    if (question.type === "instance") {
      if (!empty(value)) instanceId = value;
    } else if (!empty(value)) {
      if (!answerKey.test(question.key || "")) return { error: "补充问题包含无效字段，请取消后重新发起" };
      const answer = question.type === "number" ? Number(value) : String(value).trim();
      if (question.type === "number" && !Number.isSafeInteger(answer)) return { error: `请填写有效整数：${question.label}` };
      answers[question.key] = answer;
    }
  }
  if (instanceId && !instances.some((item) => item.id === instanceId)) {
    return { error: "目标实例不在当前账号可见范围内" };
  }
  const checked = normalizeClarificationAnswers(answers, questions);
  return checked.error ? checked : { answers: checked.answers, instanceId };
}
