import React, { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  message,
} from "antd";
import { CodeOutlined, ReloadOutlined } from "@ant-design/icons";
import { oracleOpsApi } from "./apiClients";
import { CurrentInstanceSummary, OpsEmptyState, PlanOnlyScriptModal, useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";
import { normalizeDgMessage, oracleDgRiskColor, oracleDgStatusTag } from "./modules/oracleOpsHelpers";
import { stableRecordKey } from "./modules/stableKeys";

export function OracleDGPage({ instances, selectedInstanceId }) {
  const oracleInstances = useMemo(() => instances.filter((item) => item.db_type === "oracle"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const selectedInstance = oracleInstances.find((item) => item.id === instanceId);
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState(null);
  const [standbyId, setStandbyId] = useState(null);
  const [planModal, setPlanModal] = useState({ open: false, title: "", content: "", warnings: [] });

  const loadDataGuard = async () => {
    if (!instanceId) {
      message.warning("请先选择 Oracle 实例");
      return;
    }
    setLoading(true);
    try {
      const result = await oracleOpsApi.dataGuardOverview(instanceId, standbyId);
      setData(result);
    } catch (e) {
      message.error(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (instanceId) loadDataGuard();
  }, [instanceId, standbyId]);

  const generateSwitchoverPlan = async () => {
    if (!instanceId) return;
    try {
      const result = await oracleOpsApi.dataGuardSwitchoverCheckPlan(instanceId);
      setPlanModal({
        open: true,
        title: "Data Guard 切换前检查方案",
        content: result.sql,
        warnings: result.warnings || [result.message].filter(Boolean),
      });
    } catch (e) {
      message.error(e.message);
    }
  };

  const current = data?.current || {};
  const primary = data?.primary || {};
  const summary = data?.summary || {};

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          <CurrentInstanceSummary instance={selectedInstance} emptyText="请在右上角选择 Oracle 实例" />
          <Select
            className="instance-select"
            placeholder="选择备库实例（查询延迟）"
            allowClear
            value={standbyId}
            onChange={(v) => setStandbyId(v)}
            options={oracleInstances.filter(i => i.id !== instanceId).map(i => ({ value: i.id, label: `${i.instance_name} (${i.host}:${i.port})` }))}
          />
          <Button type="primary" icon={<ReloadOutlined />} loading={loading} disabled={!instanceId} onClick={loadDataGuard}>查询 DG 状态</Button>
          <Button icon={<CodeOutlined />} disabled={!instanceId} onClick={generateSwitchoverPlan}>切换检查方案</Button>
          <Tag color="blue">Plan Only</Tag>
          {data && <Tag color={summary.configured ? "blue" : "default"}>{summary.configured ? "已检测到 DG" : "未配置 DG"}</Tag>}
        </Space>
      </Card>

      <Spin spinning={loading}>
        {data ? (
          <Space direction="vertical" size={16} className="full-width">
            <Alert
              type={summary.risk_level === "critical" ? "error" : summary.risk_level === "warning" ? "warning" : "success"}
              showIcon
              message={normalizeDgMessage(summary.message) || "Data Guard 查询完成"}
              description={data.errors?.length ? <ul style={{ margin: 0, paddingLeft: 20 }}>{data.errors.map((item) => <li key={item}>{item}</li>)}</ul> : null}
            />

            <Row gutter={[16, 16]}>
              <Col xs={12} md={6}>
                <Card size="small">
                  <Statistic title="风险等级" value={summary.risk_level || "normal"} valueStyle={{ color: oracleDgRiskColor(summary.risk_level) }} />
                </Card>
              </Col>
              <Col xs={12} md={6}>
                <Card size="small">
                  <Statistic title="备库数量" value={data.standbys?.length || 0} />
                </Card>
              </Col>
              <Col xs={12} md={6}>
                <Card size="small">
                  <Statistic title="传输延迟" value={summary.transport_lag || "-"} />
                </Card>
              </Col>
              <Col xs={12} md={6}>
                <Card size="small">
                  <Statistic title="应用延迟" value={summary.apply_lag || "-"} />
                </Card>
              </Col>
            </Row>

            <Card title="主库信息" size="small">
              <Descriptions column={2} bordered size="small">
                <Descriptions.Item label="主库实例名">{primary.primary_instance_name || (current.database_role === "PRIMARY" ? current.instance_name : "-")}</Descriptions.Item>
                <Descriptions.Item label="主库状态">{oracleDgStatusTag(primary.status || (current.database_role === "PRIMARY" ? current.instance_status : ""))}</Descriptions.Item>
                <Descriptions.Item label="数据库名">{primary.database_name || current.database_name || "-"}</Descriptions.Item>
                <Descriptions.Item label="DB_UNIQUE_NAME">{primary.db_unique_name || current.db_unique_name || "-"}</Descriptions.Item>
                <Descriptions.Item label="当前连接角色">{current.database_role || "-"}</Descriptions.Item>
                <Descriptions.Item label="打开模式">{current.open_mode || "-"}</Descriptions.Item>
                <Descriptions.Item label="保护模式">{current.protection_mode || "-"}</Descriptions.Item>
                <Descriptions.Item label="切换状态">{current.switchover_status || "-"}</Descriptions.Item>
              </Descriptions>
            </Card>

            <Card title="备库状态" size="small">
              <Table
                size="small"
                rowKey={(row) => stableRecordKey("oracle-dg-standby", row, ["dest_id", "standby_instance_name", "destination", "database_mode", "recovery_mode"])}
                dataSource={data.standbys || []}
                pagination={false}
                scroll={{ x: 1200 }}
                locale={{ emptyText: <Empty description="暂无备库信息" /> }}
                columns={[
                  { title: "备库实例名", dataIndex: "standby_instance_name", width: 180 },
                  { title: "状态", dataIndex: "status", width: 120, render: oracleDgStatusTag },
                  { title: "数据库模式", dataIndex: "database_mode", width: 150 },
                  { title: "恢复模式", dataIndex: "recovery_mode", width: 180 },
                  { title: "延迟时间", width: 220, render: (_, row) => (
                    <Space direction="vertical" size={0}>
                      <span>传输: {row.transport_lag || "-"}</span>
                      <span>应用: {row.apply_lag || "-"}</span>
                    </Space>
                  ) },
                  { title: "应用归档", width: 220, render: (_, row) => (
                    <Space direction="vertical" size={0}>
                      <span>已归档: {row.archived_sequence ?? "-"}</span>
                      <span>已应用: {row.applied_sequence ?? "-"}</span>
                    </Space>
                  ) },
                  { title: "GAP", dataIndex: "gap_status", width: 120, render: oracleDgStatusTag },
                  { title: "错误信息", dataIndex: "error", ellipsis: true, render: (v) => v || "-" },
                ]}
              />
            </Card>

            <Row gutter={[16, 16]}>
              <Col xs={24} xl={12}>
                <Card title="DG 进程" size="small">
                  <Table
                    size="small"
                    rowKey={(row) => stableRecordKey("oracle-dg-process", row, ["process", "thread_no", "sequence_no", "status", "client_process"])}
                    dataSource={data.managed_processes || []}
                    pagination={false}
                    columns={[
                      { title: "进程", dataIndex: "process", width: 90 },
                      { title: "状态", dataIndex: "status", render: oracleDgStatusTag },
                      { title: "线程", dataIndex: "thread_no", width: 80 },
                      { title: "序列", dataIndex: "sequence_no", width: 100 },
                      { title: "客户端", dataIndex: "client_process", width: 110 },
                    ]}
                    locale={{ emptyText: <Empty description="暂无 MRP/RFS 进程信息" /> }}
                  />
                </Card>
              </Col>
              <Col xs={24} xl={12}>
                <Card title="归档接收与应用进度" size="small">
                  <Table
                    size="small"
                    rowKey="thread_no"
                    dataSource={data.archive_apply || []}
                    pagination={false}
                    columns={[
                      { title: "线程", dataIndex: "thread_no", width: 80 },
                      { title: "接收序列", dataIndex: "last_received_sequence" },
                      { title: "应用序列", dataIndex: "last_applied_sequence" },
                      { title: "最后接收时间", dataIndex: "last_received_time", width: 170 },
                      { title: "最后应用时间", dataIndex: "last_applied_time", width: 170 },
                    ]}
                    locale={{ emptyText: <Empty description="暂无归档应用进度" /> }}
                  />
                </Card>
              </Col>
            </Row>

            <Card title="Standby Redo Log" size="small">
              <Table
                size="small"
                rowKey={(row) => stableRecordKey("oracle-dg-standby-redo", row, ["thread_no", "group_no", "sequence_no", "status", "member"])}
                dataSource={data.standby_redo_logs || []}
                pagination={false}
                locale={{ emptyText: <Empty description="暂无 Standby Redo Log 信息" /> }}
                columns={[
                  { title: "组号", dataIndex: "group_no", width: 80 },
                  { title: "线程", dataIndex: "thread_no", width: 80 },
                  { title: "序列", dataIndex: "sequence_no", width: 100 },
                  { title: "大小(MB)", dataIndex: "size_mb", width: 120 },
                  { title: "已归档", dataIndex: "archived", width: 100, render: oracleDgStatusTag },
                  { title: "状态", dataIndex: "status", render: oracleDgStatusTag },
                ]}
              />
            </Card>

            {data.archive_gap?.length > 0 && (
              <Card title="归档缺口" size="small">
                <Table
                  size="small"
                  rowKey={(row) => `${row.thread_no}-${row.low_sequence}-${row.high_sequence}`}
                  dataSource={data.archive_gap}
                  pagination={false}
                  columns={[
                    { title: "线程", dataIndex: "thread_no" },
                    { title: "缺失起始序列", dataIndex: "low_sequence" },
                    { title: "缺失结束序列", dataIndex: "high_sequence" },
                  ]}
                />
              </Card>
            )}
          </Space>
        ) : (
          <OpsEmptyState title="请选择 Oracle 实例" description="选择实例后可查询 Data Guard 主备状态、延迟和归档缺口。" />
        )}
      </Spin>
      <PlanOnlyScriptModal
        title={planModal.title}
        open={planModal.open}
        script={planModal.content}
        warnings={planModal.warnings}
        width={900}
        onClose={() => setPlanModal({ open: false, title: "", content: "", warnings: [] })}
      />
    </Space>
  );
}
