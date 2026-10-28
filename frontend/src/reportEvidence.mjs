const objectValue = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : null;

export function reportEvidence(detail) {
  const analysis = objectValue(detail?.content_json?.analysis_detail);
  const source = analysis?.kind === "slow_sql_ai" ? objectValue(analysis.evidence) : null;
  const manifest = Array.isArray(detail?.evidence_manifest) ? detail.evidence_manifest : [];
  const items = [];
  if (typeof source?.sql_text === "string" && source.sql_text.trim()) {
    items.push({ key: "sql", label: "SQL 原文", text: source.sql_text });
  }
  if (source?.plan_status === "collected" && typeof source.plan_text === "string" && source.plan_text.trim()) {
    items.push({ key: "plan", label: "执行计划", text: source.plan_text });
  }
  const metrics = objectValue(source?.metrics);
  if (metrics && Object.values(metrics).some((value) => value !== null && value !== undefined)) {
    items.push({ key: "metrics", label: "采样指标", text: JSON.stringify(metrics, null, 2) });
  }
  return { source, items, manifest, count: items.length + manifest.length };
}
