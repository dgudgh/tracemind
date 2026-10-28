// Turn a capability's server-declared parameter schema into the fields a manual form
// renders. The schema is descriptive only: the backend re-validates every value, so the
// browser must send exactly the typed parameters and never free text or a prompt.

const INTEGER = "integer";
const BOOLEAN = "boolean";

const CUSTOMER_CAPABILITY_NAMES = {
  "report.document.generate": "生成分析报告",
};
const CUSTOMER_FIELD_LABELS = {
  report_type: "报告类型",
  window_hours: "最近小时数（7 天 = 168 小时）",
  format: "报告格式",
  include_ai: "使用 AI 分析",
};
const CUSTOMER_ENUM_LABELS = {
  report_type: { unified: "统一运维", after_sales: "售后分析" },
  format: { markdown: "Markdown", html: "HTML" },
};

export function customerCapabilityName(capabilityId) {
  return CUSTOMER_CAPABILITY_NAMES[capabilityId] || "已授权任务";
}

export function customerCapabilityFieldLabel(field) {
  return CUSTOMER_FIELD_LABELS[field?.name] || field?.label || field?.name || "参数";
}

export function customerCapabilityEnumLabel(fieldName, value) {
  return CUSTOMER_ENUM_LABELS[fieldName]?.[value] || String(value);
}

function nonNullType(spec) {
  if (typeof spec.type === "string" && spec.type !== "null") return spec.type;
  if (Array.isArray(spec.type)) return spec.type.find((item) => item !== "null") || "string";
  if (Array.isArray(spec.anyOf)) {
    const branch = spec.anyOf.find((item) => item && item.type && item.type !== "null");
    return branch ? nonNullType(branch) : "string";
  }
  return "string";
}

function enumValues(spec) {
  if (Array.isArray(spec.enum)) return [...spec.enum];
  if (Array.isArray(spec.anyOf)) {
    for (const branch of spec.anyOf) {
      if (branch && Array.isArray(branch.enum)) return [...branch.enum];
    }
  }
  return null;
}

export function schemaFields(schema) {
  const properties = schema && typeof schema === "object" ? schema.properties : null;
  if (!properties || typeof properties !== "object") return [];
  const required = new Set(Array.isArray(schema.required) ? schema.required : []);
  return Object.entries(properties).map(([name, spec]) => {
    const value = spec && typeof spec === "object" ? spec : {};
    const type = nonNullType(value);
    return {
      name,
      label: typeof value.title === "string" && value.title ? value.title : name,
      type,
      enum: enumValues(value),
      minimum: Number.isFinite(value.minimum) ? value.minimum : null,
      maximum: Number.isFinite(value.maximum) ? value.maximum : null,
      maxLength: Number.isInteger(value.maxLength) ? value.maxLength : null,
      pattern: typeof value.pattern === "string" ? value.pattern : null,
      default: Object.hasOwn(value, "default") ? value.default : undefined,
      required: required.has(name),
    };
  });
}

export function initialValues(fields) {
  const values = {};
  for (const field of fields) {
    if (field.default !== undefined && field.default !== null) values[field.name] = field.default;
  }
  return values;
}

export function validateValues(fields, values) {
  const problems = [];
  for (const field of fields) {
    const raw = values[field.name];
    const empty = raw === undefined || raw === null || raw === "";
    if (empty) {
      if (field.required) problems.push(`请填写 ${field.label}`);
      continue;
    }
    if (field.enum && !field.enum.includes(raw)) { problems.push(`${field.label} 不在允许取值内`); continue; }
    if (field.type === INTEGER) {
      if (!Number.isSafeInteger(raw)) problems.push(`${field.label} 必须是整数`);
      else if (field.minimum !== null && raw < field.minimum) problems.push(`${field.label} 不得小于 ${field.minimum}`);
      else if (field.maximum !== null && raw > field.maximum) problems.push(`${field.label} 不得大于 ${field.maximum}`);
    } else if (field.type === BOOLEAN) {
      if (typeof raw !== "boolean") problems.push(`${field.label} 必须是布尔值`);
    } else if (typeof raw !== "string") {
      problems.push(`${field.label} 必须是文本`);
    } else if (field.maxLength !== null && raw.length > field.maxLength) {
      problems.push(`${field.label} 长度不得超过 ${field.maxLength}`);
    } else if (field.pattern) {
      try {
        if (!new RegExp(field.pattern).test(raw)) problems.push(`${field.label} 格式不正确`);
      } catch {
        problems.push(`${field.label} 的校验规则不可用`);
      }
    }
  }
  return problems;
}

export function buildParams(fields, values) {
  const problems = validateValues(fields, values);
  if (problems.length) throw new Error(problems[0]);
  const params = {};
  for (const field of fields) {
    const raw = values[field.name];
    if (raw === undefined || raw === null || raw === "") continue;
    params[field.name] = field.type === INTEGER ? Number(raw) : field.type === BOOLEAN ? Boolean(raw) : raw;
  }
  return params;
}

export function isExecutable(item) {
  return Boolean(item && item.execution === "server" && item.plan_executable === true
    && item.availability === "available" && item.handler_status === "wired");
}

export function effectNeedsApproval(item) {
  return Boolean(item && !["read", "query"].includes(item.kind));
}

export function targetProblem(item, instances, targetId) {
  if (!item) return "请选择能力";
  const selected = instances.find((row) => row.id === targetId);
  if (item.requires_instance) {
    if (!selected) return "该能力必须选择目标实例";
    if (Array.isArray(item.authorized_engines) && item.authorized_engines.length
      && !item.authorized_engines.includes(selected.db_type)) return "目标引擎不在该能力支持范围内";
  } else if (selected && Array.isArray(item.authorized_engines) && item.authorized_engines.length
    && !item.authorized_engines.includes(selected.db_type)) {
    return "目标引擎不在该能力支持范围内";
  }
  return "";
}

export function planPayload(item, { targetId, params, requestApproval, validForSeconds }) {
  const approval = effectNeedsApproval(item) ? true : Boolean(requestApproval);
  return {
    capability_id: item.capability_id,
    definition_version: item.definition_version,
    ...(targetId ? { instance_id: targetId } : {}),
    params: { ...params },
    valid_for_seconds: validForSeconds,
    request_approval: approval,
    trigger_type: "manual",
  };
}
