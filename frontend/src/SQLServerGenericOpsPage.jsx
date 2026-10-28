import React, { useMemo } from "react";
import { Button, Card, Col, Row, Space, Spin, Statistic, Table, Tabs, Tag } from "antd";
import { FileTextOutlined, ReloadOutlined } from "@ant-design/icons";
import { OpsEmptyState } from "./OpsLayout";
import { stableRecordKey } from "./modules/stableKeys";
import { DEFAULT_TABLE_PAGINATION } from "./tablePagination";

const SECTION_PROFILES = {
  logs: {
    panelTitle: "日志健康概览",
    emptyTitle: "暂无日志采集明细",
    emptyDescription: "当前 SQL Server 日志管理接口没有返回事务日志或日志文件明细。",
    tabs: [
      { key: "logspace", label: "事务日志使用率", dataKey: "items", columns: ["Database Name", "Log Size (MB)", "Log Space Used (%)", "Status"] },
      { key: "files", label: "日志文件", dataKey: "files", columns: ["database_name", "name", "type_desc", "size_mb", "growth", "is_percent_growth"] },
    ],
    metrics: [
      { title: "数据库数", value: ({ summary, rows }) => summary.database_count ?? rows.items.length },
      { title: "日志文件", value: ({ summary, rows }) => summary.log_file_count ?? rows.files.length },
      { title: "高使用率", value: ({ rows }) => rows.items.filter((row) => readNumber(row, ["Log Space Used (%)", "log_space_used_pct"]) >= 80).length, danger: true },
      { title: "明细行数", value: ({ allRows }) => allRows.length },
    ],
  },
  sessionTransaction: {
    panelTitle: "会话事务概览",
    emptyTitle: "暂无会话事务明细",
    emptyDescription: "当前没有活动请求或用户会话明细。",
    tabs: [
      { key: "requests", label: "活动请求", dataKey: "requests", columns: ["session_id", "database_name", "command", "wait_type", "blocking_session_id", "cpu_time", "total_elapsed_time", "sql_text"] },
      { key: "sessions", label: "用户会话", dataKey: "sessions", columns: ["session_id", "login_name", "host_name", "program_name", "database_name", "open_transaction_count"] },
    ],
    metrics: [
      { title: "活动请求", value: ({ summary, rows }) => summary.request_count ?? rows.requests.length },
      { title: "用户会话", value: ({ summary, rows }) => summary.session_count ?? rows.sessions.length },
      { title: "阻塞请求", value: ({ rows }) => rows.requests.filter((row) => Number(row.blocking_session_id || 0) > 0).length, danger: true },
      { title: "打开事务", value: ({ rows }) => rows.sessions.filter((row) => Number(row.open_transaction_count || 0) > 0).length, danger: true },
    ],
  },
  objectStats: {
    panelTitle: "对象统计概览",
    emptyTitle: "暂无对象统计明细",
    emptyDescription: "当前接口没有返回表容量或对象统计数据。",
    tabs: [
      { key: "tables", label: "表容量排行", dataKey: "items", columns: ["schema_name", "table_name", "row_count", "total_mb"] },
    ],
    metrics: [
      { title: "表数量", value: ({ summary, rows }) => summary.table_count ?? rows.items.length },
      { title: "总容量(MB)", value: ({ rows }) => formatNumber(sumRows(rows.items, "total_mb")) },
      { title: "总行数", value: ({ rows }) => formatNumber(sumRows(rows.items, "row_count")) },
      { title: "TOP 明细", value: ({ rows }) => rows.items.length },
    ],
  },
  ha: {
    panelTitle: "高可用概览",
    emptyTitle: "暂无高可用明细",
    emptyDescription: "当前接口没有返回 Always On 或数据库副本状态。",
    tabs: [
      { key: "ha", label: "副本与数据库状态", dataKey: "items", columns: ["database_name", "state_desc", "synchronization_state_desc", "synchronization_health_desc", "recovery_model_desc"] },
    ],
    metrics: [
      { title: "数据库数", value: ({ summary, rows }) => summary.database_count ?? rows.items.length },
      { title: "ONLINE", value: ({ rows }) => rows.items.filter((row) => row.state_desc === "ONLINE").length },
      { title: "异常状态", value: ({ rows }) => rows.items.filter((row) => row.state_desc && row.state_desc !== "ONLINE").length, danger: true },
      { title: "同步异常", value: ({ rows }) => rows.items.filter((row) => row.synchronization_health_desc && row.synchronization_health_desc !== "HEALTHY").length, danger: true },
    ],
  },
  governance: {
    panelTitle: "数据治理概览",
    emptyTitle: "暂无治理候选对象",
    emptyDescription: "当前没有超过阈值的 SQL Server 治理候选表。",
    tabs: [
      { key: "candidates", label: "治理候选表", dataKey: "items", columns: ["schema_name", "table_name", "row_count", "total_mb"] },
    ],
    metrics: [
      { title: "候选表", value: ({ summary, rows }) => summary.candidate_count ?? rows.items.length, danger: true },
      { title: "阈值行数", value: ({ summary }) => formatNumber(summary.threshold_rows) },
      { title: "总行数", value: ({ rows }) => formatNumber(sumRows(rows.items, "row_count")) },
      { title: "明细行数", value: ({ rows }) => rows.items.length },
    ],
  },
};

const LABEL_MAP = {
  database_name: "数据库",
  "Database Name": "数据库",
  "Log Size (MB)": "日志大小(MB)",
  "Log Space Used (%)": "日志使用率",
  Status: "状态",
  name: "名称",
  type_desc: "类型",
  size_mb: "大小(MB)",
  growth: "增长",
  is_percent_growth: "百分比增长",
  session_id: "会话ID",
  login_name: "登录名",
  host_name: "主机",
  program_name: "程序",
  command: "命令",
  wait_type: "等待类型",
  blocking_session_id: "阻塞会话",
  cpu_time: "CPU 时间",
  total_elapsed_time: "执行耗时",
  sql_text: "SQL 文本",
  open_transaction_count: "打开事务",
  schema_name: "Schema",
  table_name: "表名",
  row_count: "行数",
  total_mb: "容量(MB)",
  recovery_model_desc: "恢复模型",
  state_desc: "状态",
  synchronization_state_desc: "同步状态",
  synchronization_health_desc: "同步健康",
};

export function SQLServerGenericOpsPanel({ section, data, loading, meta, onRefresh, onBuildPlan }) {
  const summary = data?.summary || {};
  const profile = SECTION_PROFILES[section] || buildFallbackProfile(meta);
  const rows = useMemo(() => collectRows(data), [data]);
  const allRows = useMemo(() => Object.values(rows).flat(), [rows]);
  const metrics = profile.metrics.map((item) => ({
    ...item,
    value: item.value({ summary, rows, allRows }),
  }));

  const tabItems = profile.tabs.map((tab) => {
    const dataSource = rows[tab.dataKey] || [];
    return {
      key: tab.key,
      label: `${tab.label}(${dataSource.length})`,
      children: (
        <Table
          rowKey={(row) => stableRecordKey(`sqlserver-${section}-${tab.key}`, row, [
            "id",
            "database_name",
            "Database Name",
            "session_id",
            "schema_name",
            "table_name",
            "name",
            "host_name",
            "login_name",
          ])}
          size="small"
          dataSource={dataSource}
          columns={buildDynamicColumns(dataSource, tab.columns)}
          scroll={{ x: true }}
          pagination={DEFAULT_TABLE_PAGINATION}
          locale={{
            emptyText: <OpsEmptyState title={profile.emptyTitle || `暂无${tab.label}`} description={data?.empty_reason || profile.emptyDescription} />,
          }}
        />
      ),
    };
  });

  if (loading) {
    return <Spin />;
  }

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Row gutter={[16, 16]}>
        {metrics.map((metric) => (
          <MetricCard
            key={metric.title}
            title={metric.title}
            value={metric.value}
            valueStyle={{ color: metric.danger && numericValue(metric.value) > 0 ? "#cf1322" : undefined }}
          />
        ))}
      </Row>

      <Card
        size="small"
        title={profile.panelTitle}
        extra={
          <Space>
            {meta?.planType && <Button icon={<FileTextOutlined />} onClick={onBuildPlan}>生成方案</Button>}
            <Button icon={<ReloadOutlined />} onClick={onRefresh}>刷新</Button>
          </Space>
        }
      >
        <Tabs type="card" items={tabItems} />
      </Card>
    </Space>
  );
}

function MetricCard({ title, value, valueStyle }) {
  return (
    <Col xs={24} sm={12} xl={6}>
      <Card size="small">
        <Statistic title={title} value={value ?? "-"} valueStyle={valueStyle} />
      </Card>
    </Col>
  );
}

function buildFallbackProfile(meta) {
  return {
    panelTitle: `${meta?.title || "采集"}概览`,
    emptyTitle: "暂无采集明细",
    emptyDescription: "当前接口没有返回明细行。",
    tabs: [{ key: "items", label: "采集明细", dataKey: "items", columns: ["name", "status", "message"] }],
    metrics: [
      { title: "汇总项", value: ({ summary }) => Object.keys(summary).length },
      { title: "明细行数", value: ({ allRows }) => allRows.length },
    ],
  };
}

function collectRows(data) {
  const arrays = {
    items: Array.isArray(data?.items) ? data.items : [],
    files: Array.isArray(data?.files) ? data.files : [],
    requests: Array.isArray(data?.requests) ? data.requests : [],
    sessions: Array.isArray(data?.sessions) ? data.sessions : [],
    databases: Array.isArray(data?.databases) ? data.databases : [],
    risk_items: Array.isArray(data?.risk_items) ? data.risk_items : [],
    growth_risks: Array.isArray(data?.growth_risks) ? data.growth_risks : [],
    tables: Array.isArray(data?.tables) ? data.tables : [],
  };
  if (!arrays.items.length && arrays.tables.length) {
    arrays.items = arrays.tables;
  }
  return arrays;
}

function buildDynamicColumns(rows, fallbackKeys = []) {
  const first = rows[0] || {};
  const keys = Object.keys(first).length ? Object.keys(first).slice(0, 12) : fallbackKeys;
  return keys.map((key) => ({
    title: LABEL_MAP[key] || key,
    dataIndex: key,
    key,
    ellipsis: true,
    render: (value) => renderValue(key, value),
  }));
}

function renderValue(key, value) {
  if (value === null || value === undefined || value === "") return "-";
  if (key === "state_desc") return <Tag color={value === "ONLINE" ? "green" : "red"}>{String(value)}</Tag>;
  if (key === "Status" || key === "status") return <Tag color={String(value).toLowerCase().includes("run") ? "green" : "blue"}>{String(value)}</Tag>;
  if (key === "recovery_model_desc") return <Tag color={value === "FULL" ? "green" : "orange"}>{String(value)}</Tag>;
  if (key === "synchronization_health_desc") return <Tag color={value === "HEALTHY" ? "green" : "orange"}>{String(value)}</Tag>;
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function readNumber(row, keys) {
  for (const key of keys) {
    const num = Number(row?.[key]);
    if (Number.isFinite(num)) return num;
  }
  return 0;
}

function sumRows(rows, key) {
  return rows.reduce((total, row) => total + readNumber(row, [key]), 0);
}

function formatNumber(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return num.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function numericValue(value) {
  const num = Number(String(value ?? "").replace(/,/g, ""));
  return Number.isFinite(num) ? num : 0;
}
