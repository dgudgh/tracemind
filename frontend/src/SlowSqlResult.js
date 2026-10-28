export const SLOW_SQL_CATEGORIES = [
  { value: "active", label: "当前执行" },
  { value: "top_elapsed", label: "耗时最长" },
  { value: "top_avg", label: "平均最慢" },
  { value: "top_buffer", label: "逻辑读最高" },
  { value: "top_disk", label: "物理读最高" },
  { value: "waiting", label: "正在等待" },
];

const LABELS = {
  sql_id: "SQL ID", child_number: "子游标", con_id: "容器 ID", plan_hash_value: "Plan Hash",
  executions: "累计执行次数", elapsed_seconds: "累计耗时(s)", avg_elapsed_seconds: "平均耗时(s)",
  cpu_seconds: "累计 CPU(s)", avg_cpu_seconds: "平均 CPU(s)", buffer_gets: "累计逻辑读",
  avg_buffer_gets: "平均逻辑读", disk_reads: "累计物理读", avg_disk_reads: "平均物理读",
  rows_processed: "累计处理行数", sid: "SID", "serial#": "Serial#", serial_num: "Serial#",
  username: "用户", machine: "机器", program: "程序", module: "模块", status: "会话状态",
  event: "等待事件", wait_class: "等待类", seconds_in_wait: "等待(s)", running_seconds: "运行(s)",
  blocking_session: "阻塞会话", sql_text: "SQL 文本", last_active_time: "最后活跃时间",
};

export function slowSqlFields(rows = []) {
  const present = new Set(rows.flatMap((row) => Object.keys(row)));
  return Object.entries(LABELS).filter(([key]) => present.has(key)).map(([key, title]) => ({ key, title }));
}

export function validateSlowSqlResult(data) {
  if (!data || ["failed", "degraded", "error", "unsupported"].includes(data.status)) {
    throw new Error(data?.message || data?.error || "慢 SQL 查询失败，未取得有效结果");
  }
  if (!Array.isArray(data.rows)) throw new Error("慢 SQL 接口未返回有效的记录列表");
  return data;
}

export function slowSqlSnapshot(data, instanceId, params = {}, receivedAt = new Date().toISOString()) {
  validateSlowSqlResult(data);
  return { instanceId, category: data.category || params.category || "top_avg", hours: params.hours || 1, receivedAt, data };
}

export function matchingSlowSqlSnapshot(snapshot, instanceId) {
  return snapshot && String(snapshot.instanceId) === String(instanceId) && Array.isArray(snapshot.data?.rows)
    && !["failed", "degraded", "error", "unsupported"].includes(snapshot.data.status) ? snapshot : null;
}

export function slowSqlConclusion(snapshot) {
  const rows = snapshot?.data?.rows || [];
  const session = ["active", "waiting"].includes(snapshot?.category);
  const numeric = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
  const ranked = rows.filter((row) => numeric(row.avg_elapsed_seconds))
    .slice().sort((a, b) => Number(b.avg_elapsed_seconds) - Number(a.avg_elapsed_seconds));
  const lines = [`本次返回 ${rows.length} 条${session ? "会话记录" : "SQL 游标记录"}，涉及 ${new Set(rows.map((row) => row.sql_id).filter(Boolean)).size} 个 SQL ID。`];
  if (ranked.length) lines.push(`样本中平均耗时最高的是 ${ranked[0].sql_id || "未知 SQL"}：${Number(ranked[0].avg_elapsed_seconds).toFixed(2)} 秒，累计执行 ${ranked[0].executions ?? "未知"} 次。`);
  if (!rows.length) lines.push("当前条件未返回记录，不能据此判断数据库无慢 SQL 或运行健康。");
  const scope = session
    ? "来源：V$SESSION / V$SQL 当前会话快照；不是历史时间窗口。"
    : `来源：V$SQL 中最近 ${snapshot?.hours || 1} 小时活跃的游标；耗时、CPU、读写和次数为游标累计值，不是该时间段的增量。最多返回 50 条。`;
  return { lines, scope, limitation: "本报告为查询证据汇总，未执行 AI 根因分析；执行计划需单独查询，不能仅凭排名确认根因。" };
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

export function slowSqlReportHtml(snapshot, instanceName = "Oracle") {
  const conclusion = slowSqlConclusion(snapshot);
  const rows = snapshot.data.rows;
  const fields = slowSqlFields(rows);
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>慢 SQL 查询报告</title>
<style>body{font:14px/1.6 system-ui,sans-serif;color:#20252b;margin:32px auto;padding:0 20px;max-width:1280px}h1{font-size:24px}header{border-bottom:2px solid #1677ff;padding-bottom:16px}section{margin:24px 0}p{overflow-wrap:anywhere}.table{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border:1px solid #dfe3e8;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#f4f6f8}td:last-child{min-width:320px;white-space:pre-wrap}.muted{color:#5d6773}@media print{body{margin:0;max-width:none}.table{overflow:visible}table{table-layout:fixed}td:last-child{min-width:0}tr{break-inside:avoid}}</style>
<header><h1>慢 SQL 查询报告</h1><p>${escapeHtml(instanceName)} · 实例 ${escapeHtml(snapshot.instanceId)}</p><p class="muted">查询结果接收时间：${escapeHtml(snapshot.receivedAt)} · 本次结果导出，未归档到巡检报告</p></header>
<section><h2>查询结论</h2>${conclusion.lines.map((line) => `<p>${escapeHtml(line)}</p>`).join("")}<p>${escapeHtml(conclusion.scope)}</p><p class="muted">${escapeHtml(conclusion.limitation)}</p></section>
<section><h2>原始查询记录（${rows.length} 条）</h2><div class="table"><table><thead><tr>${fields.map((field) => `<th>${escapeHtml(field.title)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${fields.map((field) => `<td>${escapeHtml(row[field.key] ?? "-")}</td>`).join("")}</tr>`).join("")}</tbody></table></div></section></html>`;
}
