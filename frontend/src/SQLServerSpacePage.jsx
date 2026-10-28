import React, { useMemo } from "react";
import { Alert, Button, Card, Col, Progress, Row, Space, Spin, Statistic, Table, Tabs, Tag, Typography } from "antd";
import { FileTextOutlined, ReloadOutlined } from "@ant-design/icons";
import { OpsEmptyState } from "./OpsLayout";
import { stableRecordKey } from "./modules/stableKeys";
import { DEFAULT_TABLE_PAGINATION } from "./tablePagination";

const { Text } = Typography;

export function SQLServerSpacePanel({ data, loading, onRefresh, onBuildPlan }) {
  const summary = data?.summary || {};
  const databases = Array.isArray(data?.databases) ? data.databases : [];
  const files = Array.isArray(data?.files) ? data.files : [];
  const tempdbFiles = Array.isArray(data?.tempdb_files) ? data.tempdb_files : [];
  const risks = Array.isArray(data?.growth_risks) ? data.growth_risks : [];

  const totalMb = numberValue(summary.total_size_mb);
  const dataPct = totalMb ? Math.round((numberValue(summary.data_size_mb) / totalMb) * 100) : 0;
  const logPct = totalMb ? Math.round((numberValue(summary.log_size_mb) / totalMb) * 100) : 0;
  const tempdbPct = totalMb ? Math.max(1, Math.round((numberValue(summary.tempdb_size_mb) / totalMb) * 100)) : 0;

  const tabItems = useMemo(() => [
    {
      key: "databases",
      label: `数据库容量(${databases.length})`,
      children: (
        <Table
          rowKey={(row) => row.database_name}
          size="small"
          dataSource={databases}
          columns={databaseColumns}
          scroll={{ x: true }}
          pagination={DEFAULT_TABLE_PAGINATION}
        />
      ),
    },
    {
      key: "files",
      label: `文件明细(${files.length})`,
      children: (
        <Table
          rowKey={(row) => stableRecordKey("sqlserver-space-file", row, ["database_name", "file_id", "logical_name", "physical_name"])}
          size="small"
          dataSource={files}
          columns={fileColumns}
          scroll={{ x: 1500 }}
          pagination={DEFAULT_TABLE_PAGINATION}
        />
      ),
    },
    {
      key: "tempdb",
      label: `TempDB(${tempdbFiles.length})`,
      children: (
        <Table
          rowKey={(row) => stableRecordKey("sqlserver-tempdb-file", row, ["file_id", "logical_name", "physical_name"])}
          size="small"
          dataSource={tempdbFiles}
          columns={tempdbColumns}
          scroll={{ x: true }}
          pagination={DEFAULT_TABLE_PAGINATION}
        />
      ),
    },
    {
      key: "risks",
      label: `容量风险(${risks.length})`,
      children: risks.length ? (
        <Table
          rowKey={(row) => stableRecordKey("sqlserver-space-risk", row, ["category", "object_name", "message"])}
          size="small"
          dataSource={risks}
          columns={riskColumns}
          scroll={{ x: true }}
          pagination={DEFAULT_TABLE_PAGINATION}
        />
      ) : (
        <OpsEmptyState title="暂无容量风险" description="当前未发现百分比增长、增长步长过小、MAXSIZE 接近上限或磁盘余量不足风险。" />
      ),
    },
  ], [databases, files, tempdbFiles, risks]);

  if (loading) {
    return <Spin />;
  }

  if (!databases.length && !files.length && !tempdbFiles.length) {
    return (
      <OpsEmptyState
        title="暂无空间数据"
        description={data?.empty_reason || "当前 SQL Server 空间接口没有返回数据库文件信息，请检查账号权限或实例连接。"}
        action={<Button icon={<ReloadOutlined />} onClick={onRefresh}>刷新</Button>}
      />
    );
  }

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Row gutter={[16, 16]}>
        <MetricCard title="数据库数" value={summary.database_count || databases.length} />
        <MetricCard title="文件数" value={summary.file_count || files.length} />
        <MetricCard title="总容量" value={formatGb(summary.total_size_mb)} suffix="GB" />
        <MetricCard title="TempDB" value={formatGb(summary.tempdb_size_mb)} suffix="GB" />
        <MetricCard title="百分比增长文件" value={summary.percent_growth_count || 0} />
        <MetricCard title="容量风险" value={summary.risk_count || risks.length} valueStyle={{ color: risks.length ? "#cf1322" : "#389e0d" }} />
      </Row>

      <Card
        size="small"
        title="容量结构"
        extra={
          <Space>
            <Button icon={<FileTextOutlined />} onClick={onBuildPlan}>生成增长方案</Button>
            <Button icon={<ReloadOutlined />} onClick={onRefresh}>刷新</Button>
          </Space>
        }
      >
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={8}>
            <Text type="secondary">数据文件占比</Text>
            <Progress percent={dataPct} strokeColor="#1677ff" />
          </Col>
          <Col xs={24} lg={8}>
            <Text type="secondary">日志文件占比</Text>
            <Progress percent={logPct} strokeColor="#fa8c16" />
          </Col>
          <Col xs={24} lg={8}>
            <Text type="secondary">TempDB 占比</Text>
            <Progress percent={tempdbPct} strokeColor="#722ed1" />
          </Col>
        </Row>
      </Card>

      {risks.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`发现 ${risks.length} 个空间配置风险`}
          description="重点检查百分比增长、增长步长过小、MAXSIZE 上限和底层卷剩余空间。所有变更仅生成 Plan Only 脚本。"
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
  { title: "文件数", dataIndex: "file_count", width: 90 },
  { title: "数据文件", dataIndex: "data_file_count", width: 90 },
  { title: "日志文件", dataIndex: "log_file_count", width: 90 },
  { title: "数据容量(GB)", dataIndex: "data_size_mb", width: 130, render: formatGb },
  { title: "日志容量(GB)", dataIndex: "log_size_mb", width: 130, render: formatGb },
  { title: "总容量(GB)", dataIndex: "total_size_mb", width: 130, render: formatGb },
];

const fileColumns = [
  { title: "数据库", dataIndex: "database_name", width: 180, ellipsis: true },
  { title: "逻辑文件", dataIndex: "logical_name", width: 220, ellipsis: true },
  { title: "类型", dataIndex: "type_desc", width: 100, render: fileTypeTag },
  { title: "状态", dataIndex: "state_desc", width: 100 },
  { title: "大小(MB)", dataIndex: "size_mb", width: 120, render: formatMb },
  { title: "增长配置", dataIndex: "growth_desc", width: 120 },
  { title: "最大容量", dataIndex: "max_size_desc", width: 130 },
  { title: "卷剩余(GB)", dataIndex: "volume_free_mb", width: 130, render: formatGb },
  { title: "卷剩余率", dataIndex: "volume_free_pct", width: 110, render: formatPct },
  { title: "卷", dataIndex: "volume_mount_point", width: 120, ellipsis: true },
  { title: "物理路径", dataIndex: "physical_name", width: 360, ellipsis: true },
];

const tempdbColumns = [
  { title: "逻辑文件", dataIndex: "logical_name", width: 220, ellipsis: true },
  { title: "类型", dataIndex: "type_desc", width: 100, render: fileTypeTag },
  { title: "状态", dataIndex: "state_desc", width: 100 },
  { title: "大小(MB)", dataIndex: "size_mb", width: 120, render: formatMb },
  { title: "增长配置", dataIndex: "growth_desc", width: 120 },
  { title: "最大容量", dataIndex: "max_size_desc", width: 130 },
  { title: "卷剩余(GB)", dataIndex: "volume_free_mb", width: 130, render: formatGb },
  { title: "物理路径", dataIndex: "physical_name", ellipsis: true },
];

const riskColumns = [
  { title: "级别", dataIndex: "level", width: 90, render: riskTag },
  { title: "类别", dataIndex: "category", width: 110 },
  { title: "对象", dataIndex: "object_name", width: 240, ellipsis: true },
  { title: "风险说明", dataIndex: "message", width: 360 },
  { title: "建议", dataIndex: "suggestion", width: 360 },
];

function fileTypeTag(value) {
  return <Tag color={value === "LOG" ? "orange" : "blue"}>{value || "-"}</Tag>;
}

function riskTag(value) {
  const color = value === "critical" ? "red" : "orange";
  return <Tag color={color}>{value === "critical" ? "严重" : "预警"}</Tag>;
}

function numberValue(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num : 0;
}

function formatMb(value) {
  return numberValue(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatGb(value) {
  return (numberValue(value) / 1024).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatPct(value) {
  if (value === null || value === undefined || value === "") return "-";
  return `${numberValue(value).toFixed(2)}%`;
}
