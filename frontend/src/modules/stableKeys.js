const DEFAULT_RECORD_KEY_FIELDS = [
  "id",
  "uid",
  "event_uid",
  "sql_id",
  "plan_hash",
  "rule_code",
  "rule_name",
  "check_category",
  "check_item",
  "metric_name",
  "sample_time",
  "capture_time",
  "create_time",
  "type",
  "path",
  "title",
  "name",
  "schema_name",
  "table_schema",
  "table_name",
  "index_name",
  "user",
  "host",
  "MEMBER_ID",
  "Log_name",
  "trx_id",
  "waiting_trx_id",
];

export function hashString(value) {
  let hash = 0;
  const text = String(value ?? "");
  for (let index = 0; index < text.length; index += 1) {
    hash = ((hash << 5) - hash + text.charCodeAt(index)) | 0;
  }
  return Math.abs(hash).toString(36);
}

export function stableValueKey(prefix, value) {
  return `${prefix}-${hashString(stableStringify(value))}`;
}

export function stableRecordKey(prefix, record = {}, preferredFields = []) {
  const fields = [...new Set([...preferredFields, ...DEFAULT_RECORD_KEY_FIELDS])];
  const parts = fields
    .map((field) => record?.[field])
    .filter((value) => value !== null && value !== undefined && value !== "");
  return stableValueKey(prefix, parts.length ? parts : record);
}

function stableStringify(value) {
  if (value === null || typeof value !== "object") return String(value ?? "");
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  return `{${Object.keys(value).sort().map((key) => `${key}:${stableStringify(value[key])}`).join(",")}}`;
}
