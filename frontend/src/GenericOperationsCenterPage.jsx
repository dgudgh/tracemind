import React, { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Col, Row, Space, Spin, Statistic, Table, Tag, message } from "antd";
import { FileTextOutlined, ReloadOutlined } from "@ant-design/icons";
import { genericOpsApi } from "./apiClients";
import { OpsCenterTabs, OpsEmptyState, PlanOnlyScriptModal, useFirstAvailableInstanceId } from "./OpsLayout";
import { SQLServerGenericOpsPanel } from "./SQLServerGenericOpsPage";
import { SQLServerOverviewPanel } from "./SQLServerOverviewPage";
import { SQLServerSpacePanel } from "./SQLServerSpacePage";
import { stableRecordKey } from "./modules/stableKeys";
import { DEFAULT_TABLE_PAGINATION } from "./tablePagination";

const POSTGRESQL_OPS_META = {
  overview: { title: "运行概览", description: "版本、数据库、连接、启动时间和核心参数。", planType: null },
  space: { title: "空间管理", description: "数据库大小、表空间、表/索引容量和膨胀线索。", planType: null },
  wal: { title: "WAL 日志", description: "WAL 参数、归档模式、复制槽积压和 PITR 风险。", planType: null },
  sessionTransaction: { title: "会话事务", description: "活动会话、锁等待、阻塞链和长事务。", planType: "kill_session" },
  objectStats: { title: "对象统计", description: "表/索引统计、Vacuum 状态、未使用索引和统计信息建议。", planType: "vacuum" },
  ha: { title: "高可用", description: "流复制、复制延迟、复制槽和高可用状态检查。", planType: "switchover_check" },
  governance: { title: "数据治理", description: "大表归档、分区治理、CSV 导出和历史库迁移方案。", planType: "archive" },
};

const SQLSERVER_OPS_META = {
  overview: { title: "运行概览", description: "版本、实例、服务、数据库和配置概览。", planType: null },
  space: { title: "空间管理", description: "数据库文件、TempDB、文件增长配置和容量风险。", planType: "growth" },
  logs: { title: "日志管理", description: "Error Log、事务日志使用率、VLF 和日志备份状态。", planType: "log_backup" },
  sessionTransaction: { title: "会话事务", description: "当前会话、活动请求、阻塞链、死锁和长事务。", planType: "kill_session" },
  objectStats: { title: "对象统计", description: "表大小、索引碎片、缺失索引、未使用索引和统计信息。", planType: "rebuild_index" },
  ha: { title: "高可用", description: "Always On、可用性组、副本状态、同步状态和 Failover 检查。", planType: "failover_check" },
  governance: { title: "数据治理", description: "历史表归档、分区治理、BCP 导出和归档库迁移方案。", planType: "archive" },
};

export function GenericOperationsCenterPage({ engine, activeKey, selectedInstanceId, instances }) {
  const section = activeKey.split(".").pop();
  const metaMap = engine === "postgresql" ? POSTGRESQL_OPS_META : SQLSERVER_OPS_META;
  if (!metaMap[section]) {
    const engineLabel = engine === "postgresql" ? "PostgreSQL" : "SQL Server";
    return (
      <OpsEmptyState
        title="未知运维模块"
        description={`${engineLabel} 不支持 ${section || "空"} 运维分区，请从左侧菜单重新选择。`}
      />
    );
  }
  return <GenericOpsPanel engine={engine} section={section} selectedInstanceId={selectedInstanceId} instances={instances} />;
}

export function PostgreSQLOperationsCenterPage({ activeKey, instanceId, instances }) {
  return <GenericOperationsCenterPage engine="postgresql" activeKey={activeKey} selectedInstanceId={instanceId} instances={instances} />;
}

export function SQLServerOperationsCenterPage({ activeKey, instanceId, instances }) {
  return <GenericOperationsCenterPage engine="sqlserver" activeKey={activeKey} selectedInstanceId={instanceId} instances={instances} />;
}

function GenericOpsPanel({ engine, section, selectedInstanceId, instances }) {
  const dbInstances = useMemo(() => instances.filter((item) => item.db_type === engine), [engine, instances]);
  const [instanceId] = useFirstAvailableInstanceId(selectedInstanceId, dbInstances);
  const selectedInstance = dbInstances.find((item) => item.id === instanceId);
  const meta = engine === "postgresql" ? POSTGRESQL_OPS_META[section] : SQLSERVER_OPS_META[section];
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [plan, setPlan] = useState(null);

  const loadData = async () => {
    if (!instanceId) return;
    setLoading(true);
    try {
      setData(await genericOpsApi.section(engine, instanceId, section));
    } catch (e) {
      setData({ status: "degraded", warnings: [e.message], items: [] });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, [instanceId, section, engine]);

  const buildPlan = async () => {
    if (!instanceId || !meta?.planType) return;
    try {
      const result = await genericOpsApi.plan(engine, instanceId, meta.planType);
      setPlan(result);
    } catch (e) {
      message.error(e.message || "生成方案失败");
    }
  };

  const engineLabel = engine === "postgresql" ? "PostgreSQL" : "SQL Server";
  const emptyText = `请在右上角选择 ${engineLabel} 实例`;
  const warnings = data?.warnings || [];
  const summary = data?.summary || {};
  const items = Array.isArray(data?.items) ? data.items : [];
  const extraRows = [
    ...(Array.isArray(data?.databases) ? data.databases : []),
    ...(Array.isArray(data?.replication_slots) ? data.replication_slots : []),
    ...(Array.isArray(data?.files) ? data.files : []),
    ...(Array.isArray(data?.sessions) ? data.sessions : []),
  ];
  const tableRows = items.length ? items : extraRows;
  const columns = buildDynamicColumns(tableRows);
  const emptyDescription = data?.empty_reason
    || (warnings.length
      ? "采集失败或权限不足，请查看上方采集提示。"
      : Object.keys(summary).length
        ? "采集已成功，当前模块没有需要展示的明细行；可查看上方汇总指标，或切换其他模块。"
        : "当前接口未返回明细行，请检查实例连接、账号权限或采集配置。");
  const panelContent = !instanceId ? (
    <OpsEmptyState
      title={`请选择 ${engineLabel} 实例`}
      description={`在右上角统一实例选择器中选择 ${engineLabel} 实例后查看${meta?.title || "运维"}。`}
    />
  ) : (
    <Space direction="vertical" size={16} className="full-width">
      <Space wrap>
        {data?.status === "degraded" && <Tag color="orange">降级采集</Tag>}
        {data?.status === "plan_only" && <Tag color="purple">plan_only</Tag>}
      </Space>
      {warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message="采集提示"
          description={<ul style={{ margin: 0, paddingLeft: 20 }}>{warnings.map((item) => <li key={item}>{item}</li>)}</ul>}
        />
      )}
      {engine === "sqlserver" && section === "overview" ? (
        <SQLServerOverviewPanel data={data} loading={loading} onRefresh={loadData} />
      ) : engine === "sqlserver" && section === "space" ? (
        <SQLServerSpacePanel data={data} loading={loading} onRefresh={loadData} onBuildPlan={buildPlan} />
      ) : engine === "sqlserver" ? (
        <SQLServerGenericOpsPanel section={section} data={data} loading={loading} meta={meta} onRefresh={loadData} onBuildPlan={buildPlan} />
      ) : (
        <>
          <Row gutter={[16, 16]}>
            {Object.entries(summary).slice(0, 8).map(([key, value]) => (
              <Col xs={24} md={12} xl={6} key={key}>
                <Card size="small" className="generic-ops-summary-card">
                  <Statistic title={key} value={String(value ?? "-")} />
                </Card>
              </Col>
            ))}
          </Row>
          <Card
            title="采集明细"
            extra={
              <Space>
                {meta?.planType && <Button icon={<FileTextOutlined />} onClick={buildPlan}>生成方案</Button>}
                <Button icon={<ReloadOutlined />} onClick={loadData} loading={loading}>刷新</Button>
              </Space>
            }
          >
            {loading ? (
              <Spin />
            ) : tableRows.length ? (
              <Table rowKey={(record) => buildDynamicRowKey(section, record)} columns={columns} dataSource={tableRows} size="small" scroll={{ x: true }} pagination={DEFAULT_TABLE_PAGINATION} />
            ) : (
              <OpsEmptyState title="暂无采集明细" description={emptyDescription} />
            )}
          </Card>
        </>
      )}
    </Space>
  );

  return (
    <>
      <OpsCenterTabs
        title={`${engineLabel} ${meta?.title || "运维"}`}
        description={meta?.description}
        instance={selectedInstance}
        emptyText={emptyText}
        activeKey={section}
        onChange={() => {}}
        items={[{
          key: section,
          label: meta?.title || section,
          riskLevel: data?.status === "degraded" ? "warning" : "normal",
          children: panelContent,
        }]}
      />
      <PlanOnlyScriptModal
        open={!!plan}
        title={plan?.title || "运维方案"}
        script={plan?.script}
        warnings={plan?.warnings || []}
        onClose={() => setPlan(null)}
      />
    </>
  );
}

function buildDynamicColumns(rows) {
  const first = rows[0] || {};
  return Object.keys(first).slice(0, 10).map((key) => ({
    title: key,
    dataIndex: key,
    key,
    ellipsis: true,
    render: (value) => {
      if (value === null || value === undefined || value === "") return "-";
      if (typeof value === "object") return JSON.stringify(value);
      return String(value);
    },
  }));
}

function buildDynamicRowKey(section, record = {}) {
  return stableRecordKey(section, record, [
    "id",
    "pid",
    "session_id",
    "database_name",
    "datname",
    "schema_name",
    "schemaname",
    "table_name",
    "relname",
    "index_name",
    "name",
    "slot_name",
    "file_id",
    "file_name",
    "logical_name",
    "rolname",
    "login_name",
    "host_name",
  ]);
}

