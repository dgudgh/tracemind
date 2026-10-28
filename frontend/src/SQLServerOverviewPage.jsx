import React, { useMemo } from "react";
import { Alert, Button, Card, Col, Descriptions, Row, Space, Spin, Statistic, Table, Tabs, Tag, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { OpsEmptyState } from "./OpsLayout";
import { stableRecordKey } from "./modules/stableKeys";
import { DEFAULT_TABLE_PAGINATION } from "./tablePagination";

const { Text } = Typography;

export function SQLServerOverviewPanel({ data, loading, onRefresh }) {
  const summary = data?.summary || {};
  const server = data?.server || summary;
  const databases = Array.isArray(data?.databases) ? data.databases : [];
  const configurations = Array.isArray(data?.configurations) ? data.configurations : [];
  const risks = Array.isArray(data?.risk_items) ? data.risk_items : [];

  const tabItems = useMemo(() => [
    {
      key: "databases",
      label: `数据库状态(${databases.length})`,
      children: databases.length ? (
        <Table
          rowKey={(row) => stableRecordKey("sqlserver-overview-db", row, ["database_id", "database_name"])}
          size="small"
          dataSource={databases}
          columns={databaseColumns}
          scroll={{ x: true }}
          pagination={DEFAULT_TABLE_PAGINATION}
        />
      ) : (
        <OpsEmptyState title="暂无数据库状态" description="当前概览接口没有返回 sys.databases 明细。" />
      ),
    },
    {
      key: "configurations",
      label: `核心参数(${configurations.length})`,
      children: configurations.length ? (
        <Table
          rowKey={(row) => row.name}
          size="small"
          dataSource={configurations}
          columns={configurationColumns}
          scroll={{ x: true }}
          pagination={DEFAULT_TABLE_PAGINATION}
        />
      ) : (
        <OpsEmptyState title="暂无核心参数" description="当前账号未返回 sys.configurations 中的核心参数。" />
      ),
    },
    {
      key: "risks",
      label: `风险建议(${risks.length})`,
      children: risks.length ? (
        <Table
          rowKey={(row) => stableRecordKey("sqlserver-overview-risk", row, ["category", "object_name", "message"])}
          size="small"
          dataSource={risks}
          columns={riskColumns}
          scroll={{ x: true }}
          pagination={DEFAULT_TABLE_PAGINATION}
        />
      ) : (
        <OpsEmptyState title="暂无运行风险" description="数据库状态、核心参数和常见运行选项未发现明显风险。" />
      ),
    },
  ], [databases, configurations, risks]);

  if (loading) {
    return <Spin />;
  }

  if (!Object.keys(summary).length && !databases.length) {
    return (
      <OpsEmptyState
        title="暂无运行概览"
        description={data?.empty_reason || "当前 SQL Server 运行概览没有返回数据，请检查实例连接或账号权限。"}
        action={<Button icon={<ReloadOutlined />} onClick={onRefresh}>刷新</Button>}
      />
    );
  }

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Row gutter={[16, 16]}>
        <MetricCard title="数据库数" value={summary.database_count || databases.length} />
        <MetricCard title="ONLINE" value={summary.online_database_count || 0} valueStyle={{ color: "#389e0d" }} />
        <MetricCard title="会话总数" value={summary.total_sessions || 0} />
        <MetricCard title="运行会话" value={summary.running_sessions || 0} />
        <MetricCard title="CPU" value={server.cpu_count || "-"} suffix={server.cpu_count ? "核" : undefined} />
        <MetricCard title="运行风险" value={summary.risk_count || risks.length} valueStyle={{ color: risks.length ? "#cf1322" : "#389e0d" }} />
      </Row>

      <Card
        size="small"
        title="实例与运行状态"
        extra={<Button icon={<ReloadOutlined />} onClick={onRefresh}>刷新</Button>}
      >
        <Descriptions bordered size="small" column={2}>
          <Descriptions.Item label="Server Name">{server.server_name || "-"}</Descriptions.Item>
          <Descriptions.Item label="服务名">{server.service_name || "-"}</Descriptions.Item>
          <Descriptions.Item label="机器名">{server.machine_name || "-"}</Descriptions.Item>
          <Descriptions.Item label="实例名">{server.instance_name || "默认实例"}</Descriptions.Item>
          <Descriptions.Item label="版本">{server.product_version || "-"}</Descriptions.Item>
          <Descriptions.Item label="补丁级别">{server.product_level || "-"}</Descriptions.Item>
          <Descriptions.Item label="版本类型" span={2}>{server.edition || "-"}</Descriptions.Item>
          <Descriptions.Item label="启动时间">{server.sqlserver_start_time || "-"}</Descriptions.Item>
          <Descriptions.Item label="当前登录">{server.current_login || "-"}</Descriptions.Item>
          <Descriptions.Item label="当前数据库">{server.database_name || "-"}</Descriptions.Item>
          <Descriptions.Item label="物理内存">{formatMemory(server.physical_memory_kb)}</Descriptions.Item>
          <Descriptions.Item label="集群 / HADR">
            <Space wrap>
              <Tag color={truthy(server.is_clustered) ? "blue" : "default"}>{truthy(server.is_clustered) ? "Clustered" : "非集群"}</Tag>
              <Tag color={truthy(server.is_hadr_enabled) ? "green" : "default"}>{truthy(server.is_hadr_enabled) ? "HADR 启用" : "HADR 未启用"}</Tag>
            </Space>
          </Descriptions.Item>
          <Descriptions.Item label="完整版本" span={2}>
            <Text style={{ whiteSpace: "pre-wrap" }}>{server.version || "-"}</Text>
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {risks.length > 0 && (
        <Alert
          type={risks.some((item) => item.level === "critical") ? "error" : "warning"}
          showIcon
          message={`发现 ${risks.length} 个运行概览风险`}
          description="建议优先查看数据库状态、恢复模型、AUTO_CLOSE/AUTO_SHRINK、内存与并行参数。所有变更保持人工确认和 Plan Only。"
        />
      )}

      <Tabs type="card" items={tabItems} />
    </Space>
  );
}

function MetricCard({ title, value, suffix, valueStyle }) {
  return (
    <Col xs={24} sm={12} xl={4}>
      <Card size="small">
        <Statistic title={title} value={value} suffix={suffix} valueStyle={valueStyle} />
      </Card>
    </Col>
  );
}

const databaseColumns = [
  { title: "数据库", dataIndex: "database_name", width: 220, ellipsis: true },
  { title: "状态", dataIndex: "state_desc", width: 120, render: databaseStatusTag },
  { title: "访问模式", dataIndex: "user_access_desc", width: 120 },
  { title: "恢复模型", dataIndex: "recovery_model_desc", width: 120, render: recoveryTag },
  { title: "兼容级别", dataIndex: "compatibility_level", width: 100 },
  { title: "只读", dataIndex: "is_read_only", width: 90, render: boolTag },
  { title: "AUTO_CLOSE", dataIndex: "is_auto_close_on", width: 110, render: boolRiskTag },
  { title: "AUTO_SHRINK", dataIndex: "is_auto_shrink_on", width: 120, render: boolRiskTag },
  { title: "Page Verify", dataIndex: "page_verify_option_desc", width: 130 },
  { title: "创建时间", dataIndex: "create_date", width: 180 },
];

const configurationColumns = [
  { title: "参数", dataIndex: "name", width: 260 },
  { title: "运行值", dataIndex: "value_in_use", width: 120, render: valueText },
  { title: "配置值", dataIndex: "value", width: 120, render: valueText },
  { title: "动态", dataIndex: "is_dynamic", width: 90, render: boolTag },
  { title: "高级", dataIndex: "is_advanced", width: 90, render: boolTag },
  { title: "说明", dataIndex: "description", ellipsis: true },
];

const riskColumns = [
  { title: "级别", dataIndex: "level", width: 90, render: riskTag },
  { title: "类别", dataIndex: "category", width: 120 },
  { title: "对象", dataIndex: "object_name", width: 260, ellipsis: true },
  { title: "风险说明", dataIndex: "message", width: 380 },
  { title: "建议", dataIndex: "suggestion", width: 420 },
];

function databaseStatusTag(value) {
  return <Tag color={value === "ONLINE" ? "green" : "red"}>{value || "-"}</Tag>;
}

function recoveryTag(value) {
  const color = value === "FULL" ? "green" : value === "SIMPLE" ? "orange" : "blue";
  return <Tag color={color}>{value || "-"}</Tag>;
}

function riskTag(value) {
  const colors = { critical: "red", warning: "orange", info: "blue" };
  const labels = { critical: "严重", warning: "预警", info: "提示" };
  return <Tag color={colors[value] || "default"}>{labels[value] || value || "-"}</Tag>;
}

function boolTag(value) {
  return <Tag color={truthy(value) ? "blue" : "default"}>{truthy(value) ? "是" : "否"}</Tag>;
}

function boolRiskTag(value) {
  return <Tag color={truthy(value) ? "orange" : "green"}>{truthy(value) ? "开启" : "关闭"}</Tag>;
}

function truthy(value) {
  return value === true || value === 1 || value === "1" || value === "true" || value === "True";
}

function valueText(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

function formatMemory(value) {
  const kb = Number(value);
  if (!Number.isFinite(kb) || kb <= 0) return "-";
  return `${(kb / 1024 / 1024).toLocaleString(undefined, { maximumFractionDigits: 2 })} GB`;
}
