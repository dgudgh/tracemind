import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { PlayCircleOutlined, ReloadOutlined, SwapOutlined } from "@ant-design/icons";
import {
  collectAwrNow,
  compareAwrReports,
  getAnalyzedAwrReportDetail,
  getAwrCollectPolicies,
  getSuccessfulAwrReports,
  updateAwrCollectPolicy,
} from "./modules/performanceAwrApi";
import { CurrentInstanceSummary, OpsEmptyState } from "./OpsLayout";
import { stableRecordKey, stableValueKey } from "./modules/stableKeys";

const { Text } = Typography;

function awrNum(v) {
  if (v == null || v === "") return "-";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2);
  return String(v);
}
function awrScoreColor(v) { return v >= 85 ? "#3f8600" : v >= 70 ? "#1677ff" : "#cf1322"; }

function AwrMetricCard({ title, score, pairs }) {
  return <Col xs={24} md={12}><Card size="small" title={<span>{title} {score != null && <Tag color={score >= 85 ? "green" : score >= 70 ? "blue" : "red"}>评分 {awrNum(score)}</Tag>}</span>}>
    <Descriptions size="small" column={2} bordered>
      {pairs.filter(([, v]) => v != null && v !== "").map(([k, v]) => <Descriptions.Item key={k} label={k}>{awrNum(v)}</Descriptions.Item>)}
    </Descriptions>
  </Card></Col>;
}

// 统一的格式化 AWR 分析报告视图（替代原始 JSON）
export function AwrReportView({ analysis, recommendations }) {
  if (!analysis) return <Empty description="暂无分析结果" />;
  const summary = analysis.summary || {};
  const cpu = analysis.cpu_analysis || {};
  const mem = analysis.memory_analysis || {};
  const io = analysis.io_analysis || {};
  const eff = analysis.instance_efficiency_analysis || {};
  const wait = analysis.wait_analysis || {};
  const sqlA = analysis.sql_analysis || {};
  const score = analysis.performance_score;
  const level = analysis.performance_level;
  const issues = Array.isArray(analysis.potential_issues) ? analysis.potential_issues : [];
  const recs = recommendations || analysis.recommendations || {};
  const prio = Array.isArray(recs.priority_recommendations) ? recs.priority_recommendations
    : (Array.isArray(recs) ? recs : []);
  const topSql = sqlA.top_elapsed_time_sql || sqlA.top_cpu_sql || [];
  const waitEvents = wait.top_events || [];
  const sev = s => { const v = String(s || "").toLowerCase(); return v.includes("high") || v.includes("critical") || v.includes("严重") ? "error" : v.includes("medium") || v.includes("中") ? "warning" : "info"; };

  return <Space direction="vertical" size={14} style={{ width: "100%" }}>
    <Row gutter={[16, 16]} align="middle">
      <Col xs={24} md={6}><Card size="small"><Statistic title="综合评分" value={score ?? "-"} valueStyle={{ color: awrScoreColor(score || 0) }} suffix={level ? ` ${level}` : ""} /></Card></Col>
      <Col xs={24} md={18}><Descriptions size="small" column={3} bordered>
        <Descriptions.Item label="数据库">{summary.database_name || "-"}</Descriptions.Item>
        <Descriptions.Item label="实例">{summary.instance_name || "-"}</Descriptions.Item>
        <Descriptions.Item label="版本">{summary.version || "-"}</Descriptions.Item>
        <Descriptions.Item label="主机">{summary.host || "-"}</Descriptions.Item>
        <Descriptions.Item label="快照区间" span={2}>{summary.snapshot_period || "-"}</Descriptions.Item>
        <Descriptions.Item label="DB Time(s)">{awrNum(summary.db_time)}</Descriptions.Item>
        <Descriptions.Item label="DB Load(AAS)">{awrNum(summary.db_load)}</Descriptions.Item>
        <Descriptions.Item label="Elapsed(s)">{awrNum(summary.elapsed_time)}</Descriptions.Item>
      </Descriptions></Col>
    </Row>
    <Row gutter={[16, 16]}>
      <AwrMetricCard title="CPU" score={cpu.score} pairs={[["利用率%", cpu.cpu_utilization], ["核数", cpu.cpu_cores], ["瓶颈", cpu.cpu_bottleneck]]} />
      <AwrMetricCard title="内存" score={mem.score} pairs={[["缓冲命中率%", mem.buffer_hit_ratio], ["库缓存命中%", mem.library_hit_ratio], ["内存排序%", mem.in_memory_sort_pct], ["逻辑读/秒", mem.logical_reads_per_sec], ["物理读/秒", mem.physical_reads_per_sec]]} />
      <AwrMetricCard title="IO" score={io.score} pairs={[["物理读/秒", io.physical_reads_per_sec], ["物理写/秒", io.physical_writes_per_sec], ["IO请求/秒", io.total_io_requests_per_sec], ["读写比", io.read_write_ratio], ["瓶颈", io.io_bottleneck]]} />
      <AwrMetricCard title="实例效率" score={eff.score} pairs={[["执行解析比%", eff.execute_to_parse], ["软解析%", eff.soft_parse], ["闩锁命中%", eff.latch_hit], ["缓冲命中%", eff.buffer_hit]]} />
    </Row>
    {waitEvents.length > 0 && <Card size="small" title="Top 等待事件">
      <Table dataSource={waitEvents.map((e, i) => ({ key: i, ...e }))} size="small" pagination={false} scroll={{ x: 600 }}
        columns={[
          { title: "事件", dataIndex: "event_name", ellipsis: true },
          { title: "等待类", dataIndex: "wait_class", width: 120, render: v => v || "-" },
          { title: "占DB Time%", dataIndex: "db_time_pct", width: 110, render: v => awrNum(v) },
          { title: "等待时间", dataIndex: "wait_time", width: 110, render: v => awrNum(v) },
        ]} />
    </Card>}
    {topSql.length > 0 && <Card size="small" title="Top SQL（按耗时）">
      <Table dataSource={topSql.map((s, i) => ({ key: i, ...s }))} size="small" pagination={false} scroll={{ x: 700 }}
        expandable={{ expandedRowRender: r => <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, maxHeight: 180, overflow: "auto" }}>{r.sql_text || "(无文本)"}</pre> }}
        columns={[
          { title: "SQL_ID", dataIndex: "sql_id", width: 150 },
          { title: "耗时(s)", dataIndex: "elapsed_time", width: 110, render: v => awrNum(v) },
          { title: "CPU(s)", dataIndex: "cpu_time", width: 100, render: v => awrNum(v) },
          { title: "执行次数", dataIndex: "executions", width: 100, render: v => awrNum(v) },
          { title: "Buffer Gets", dataIndex: "buffer_gets", width: 120, render: v => awrNum(v) },
        ]} />
    </Card>}
    {issues.length > 0 && <Card size="small" title={`潜在问题（${issues.length}）`}>
      {issues.map((it) => <Alert key={stableRecordKey("awr-issue", it, ["category", "issue", "title", "severity", "recommendation"])} style={{ marginBottom: 8 }} type={sev(it.severity)} showIcon
        message={`[${it.category || ""}] ${it.issue || it.title || ""}`} description={it.recommendation} />)}
    </Card>}
    {prio.length > 0 && <Card size="small" title={`优化建议（${prio.length}）`}>
      {prio.slice(0, 15).map((p, i) => typeof p === "string"
        ? <div key={stableValueKey("awr-priority", p)} style={{ marginBottom: 4 }}>• {p}</div>
        : <Alert key={stableRecordKey("awr-priority", p, ["title", "category", "finding", "impact", "severity"])} style={{ marginBottom: 8 }} type={sev(p.severity)} showIcon
          message={p.title || p.category || `建议 ${i + 1}`}
          description={<div>{p.finding && <div>发现：{p.finding}</div>}{p.impact && <div>影响：{p.impact}</div>}{Array.isArray(p.recommendations) && p.recommendations.map((x) => <div key={stableValueKey("awr-priority-recommendation", x)}>• {x}</div>)}</div>} />)}
    </Card>}
  </Space>;
}



// 通用：按首行键自动生成列的表格（用于字段不固定的 table-usage 结果）
