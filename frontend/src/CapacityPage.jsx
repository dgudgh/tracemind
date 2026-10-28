import React, { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Progress,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  message,
} from "antd";
import {
  PlayCircleOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { capacityApi } from "./apiClients";
import { useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";
import {
  dataOpsLevelTag,
  dataOpsUsageColor,
  mbToGb,
} from "./modules/dataOperationsHelpers";

export function CapacityPage({ instances, selectedInstanceId }) {
  const capacityInstances = useMemo(() => instances.filter((item) => ["oracle", "mysql", "postgresql", "sqlserver"].includes(String(item.db_type || "").toLowerCase())), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, capacityInstances);
  const [loading, setLoading] = useState(false);
  const [summary, setSummary] = useState(null);
  const [analysis, setAnalysis] = useState(null);
  const [snapshots, setSnapshots] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [policy, setPolicy] = useState(null);

  const loadData = async () => {
    if (!instanceId) return;
    setLoading(true);
    try {
      const [policy, summaryData, analysisData, snapshotData, alertData] = await Promise.all([
        capacityApi.policy(instanceId),
        capacityApi.summary(instanceId),
        capacityApi.analysis(instanceId, { days: 30 }),
        capacityApi.snapshots(instanceId, { days: 30 }),
        capacityApi.alerts({ instance_id: instanceId, limit: 30 }),
      ]);
      setPolicy(policy);
      setSummary(summaryData);
      setAnalysis(analysisData);
      setSnapshots(snapshotData.items || []);
      setAlerts(alertData.items || []);
    } catch (e) {
      message.error(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { if (instanceId) loadData(); }, [instanceId]);

  const collectNow = async () => {
    if (!instanceId) return;
    setLoading(true);
    try {
      const data = await capacityApi.collect(instanceId);
      if (data.status === "no_data") message.warning("容量查询未返回数据，已记录缺数告警");
      else if (data.status === "skipped") message.info("该数据库类型暂不支持实时容量采集");
      else message.success(`已完成容量巡检，生成 ${data.alerts?.length || 0} 条预警`);
      await loadData();
    } catch (e) {
      message.error(e.message);
    } finally {
      setLoading(false);
    }
  };

  const usageColor = dataOpsUsageColor;
  const levelTag = dataOpsLevelTag;
  const knownValue = (value, formatter = (item) => item) => value == null ? "未知" : formatter(value);
  const sizeValue = (value) => value == null ? "-" : Number(value).toFixed(2);
  const collectionStatus = summary?.collection_status || (summary?.source === "snapshot" ? "success" : "unknown");
  const statusMeta = {
    success: { type: "success", label: summary?.source === "snapshot" ? "快照数据可用" : "实时采集成功" },
    no_data: { type: "warning", label: "查询成功但没有返回容量对象" },
    unsupported: { type: "info", label: "该类型暂不支持实时容量采集" },
    unknown: { type: "info", label: "容量状态尚未确认" },
  }[collectionStatus] || { type: "warning", label: collectionStatus };

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          <Button type="primary" icon={<ReloadOutlined />} loading={loading} disabled={!instanceId} onClick={loadData}>加载容量数据</Button>
          <Button icon={<PlayCircleOutlined />} loading={loading} disabled={!instanceId} onClick={collectNow}>立即巡检</Button>
        </Space>
      </Card>
      {summary && (
        <>
          <Alert
            type={summary.data_freshness_status === "stale" ? "warning" : statusMeta.type}
            showIcon
            message={summary.data_freshness_status === "stale" ? "容量快照已过期" : statusMeta.label}
            description={[
              summary.source && `数据来源：${summary.source === "snapshot" ? "历史快照" : "实时查询"}`,
              summary.snapshot_time && `快照时间：${summary.snapshot_time}`,
              summary.data_age_minutes != null && `数据年龄：${summary.data_age_minutes} 分钟`,
              policy?.last_message && `最近任务：${policy.last_message}`,
              ...(summary.warnings || []),
            ].filter(Boolean).join("；")}
          />
          <Row gutter={[16, 16]}>
            <Col xs={12} md={6}><Card size="small"><Statistic title={summary.capacity_limit_known ? "总容量(GB)" : "逻辑大小(GB)"} value={knownValue(summary.total_mb, mbToGb)} /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="已用(GB)" value={knownValue(summary.used_mb, mbToGb)} valueStyle={{ color: summary.used_pct == null ? undefined : usageColor(summary.used_pct) }} /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="使用率" value={summary.used_pct == null ? "不适用" : Number(summary.used_pct).toFixed(1)} suffix={summary.used_pct == null ? undefined : "%"} valueStyle={{ color: summary.used_pct == null ? undefined : usageColor(summary.used_pct) }} /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="监控对象" value={summary.objects?.length || 0} /></Card></Col>
          </Row>
          {alerts.length > 0 && (
            <Alert type={alerts.some((item) => item.level === "critical") ? "error" : "warning"} showIcon message={`当前容量预警 ${alerts.length} 条`} description={alerts.slice(0, 3).map((item) => item.message).join("；")} />
          )}
          <Card title="容量增长分析" size="small">
            <Table
              dataSource={analysis?.items || []}
              rowKey="object_name"
              size="small"
              pagination={{ defaultPageSize: 12 }}
              scroll={{ x: 1400 }}
              columns={[
                { title: "容量对象", dataIndex: "object_name", width: 200, ellipsis: true },
                { title: "类型", dataIndex: "object_type", width: 120, render: (value) => <Tag color={value === "asm_diskgroup" ? "purple" : "blue"}>{({ asm_diskgroup: "磁盘组", tablespace: "表空间", schema: "Schema", database: "数据库" })[value] || value || "对象"}</Tag> },
                { title: "风险", dataIndex: "level", width: 90, render: levelTag },
                { title: "总容量(GB)", dataIndex: "total_mb", width: 110, render: mbToGb },
                { title: "已用(GB)", dataIndex: "used_mb", width: 110, render: mbToGb },
                { title: "剩余(GB)", dataIndex: "free_mb", width: 110, render: (value) => value == null ? "不适用" : mbToGb(value) },
                { title: "本次增长(MB)", dataIndex: "interval_growth_mb", width: 120 },
                { title: "本次增长率", dataIndex: "interval_growth_pct", width: 110, render: (value) => `${Number(value || 0).toFixed(2)}%` },
                { title: "日均增长(MB)", dataIndex: "daily_growth_mb", width: 120 },
                { title: "预计可用天数", dataIndex: "forecast_days", width: 120, render: (value, row) => !row.capacity_limit_known ? "无容量上限" : value == null ? "增长不足" : `${value} 天` },
                { title: "说明", dataIndex: "reason", ellipsis: true },
              ]}
            />
          </Card>
          <Card title="当前容量明细" size="small">
            <Table
              dataSource={summary.objects}
              rowKey={(row) => `${row.object_type || "tablespace"}-${row.object_name || row.tablespace_name}`}
              size="small"
              pagination={{ defaultPageSize: 15 }}
              scroll={{ x: 900 }}
              columns={[
                { title: "容量对象", dataIndex: "object_name", width: 200, ellipsis: true, render: (value, row) => value || row.tablespace_name },
                { title: "类型", dataIndex: "object_type", width: 110, render: (value) => <Tag color={value === "asm_diskgroup" ? "purple" : "blue"}>{({ asm_diskgroup: "磁盘组", tablespace: "表空间", schema: "Schema", database: "数据库" })[value] || value || "对象"}</Tag> },
                { title: "总大小(MB)", dataIndex: "total_mb", render: sizeValue },
                { title: "已用(MB)", dataIndex: "used_mb", render: sizeValue },
                { title: "空闲(MB)", dataIndex: "free_mb", render: (value) => value == null ? "不适用" : sizeValue(value) },
                { title: "使用率", dataIndex: "used_pct", render: (value) => value == null ? <Tag>不适用</Tag> : <Space><Progress percent={Number(value)} size="small" strokeColor={usageColor(value)} style={{ width: 80 }} /><span style={{ color: usageColor(value) }}>{Number(value).toFixed(1)}%</span></Space> },
              ]}
            />
          </Card>
          <Card title="最近巡检快照" size="small">
            <Table
              dataSource={snapshots}
              rowKey="id"
              size="small"
              pagination={{ defaultPageSize: 10 }}
              columns={[
                { title: "巡检时间", dataIndex: "create_time", width: 170 },
                { title: "对象", dataIndex: "object_name" },
                { title: "已用(MB)", dataIndex: "used_mb", render: sizeValue },
                { title: "剩余(MB)", dataIndex: "free_mb", render: (value) => value == null ? "不适用" : sizeValue(value) },
                { title: "总容量(MB)", dataIndex: "total_mb", render: sizeValue },
              ]}
            />
          </Card>
          <Card title="容量告警" size="small">
            <Table
              dataSource={alerts}
              rowKey="id"
              size="small"
              pagination={{ defaultPageSize: 10 }}
              columns={[
                { title: "级别", dataIndex: "level", width: 90, render: levelTag },
                { title: "对象", dataIndex: "object_name", width: 180, ellipsis: true },
                { title: "类型", dataIndex: "alert_type", width: 160, render: (value) => <Tag>{value}</Tag> },
                { title: "状态", dataIndex: "status", width: 90, render: (value) => <Tag color={value === "open" ? "orange" : value === "closed" ? "green" : "blue"}>{value}</Tag> },
                { title: "发生次数", dataIndex: "occurrence_count", width: 100 },
                { title: "说明", dataIndex: "message", ellipsis: true },
                { title: "最近发生", dataIndex: "last_seen", width: 180 },
              ]}
            />
          </Card>
        </>
      )}
    </Space>
  );
}
