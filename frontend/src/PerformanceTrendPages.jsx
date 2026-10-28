import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Input,
  Row,
  Segmented,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { LineChartOutlined, ReloadOutlined, TableOutlined } from "@ant-design/icons";
import { CurrentInstanceSummary, OpsEmptyState } from "./OpsLayout";
import { SEV_COLOR } from "./PerformancePageConstants";
import { stableRecordKey } from "./modules/stableKeys";
import {
  collectPerformanceBaseline,
  collectTableProfile,
  getOracleTableUsage,
  getPerformanceBaseline,
  getProblemTrend,
  getTableProfile,
} from "./modules/performanceApi";

const { Text } = Typography;

function AutoTable({ items, loading }) {
  if (!items || items.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据" />;
  const keys = Object.keys(items[0]).filter(k => typeof items[0][k] !== "object");
  const columns = keys.slice(0, 10).map(k => ({
    title: k, dataIndex: k, ellipsis: true,
    render: v => (v == null ? "-" : (typeof v === "number" ? v.toLocaleString() : String(v))),
  }));
  return <Table dataSource={items.map((r, i) => ({ _k: i, ...r }))} rowKey="_k" size="small" loading={loading} pagination={{ pageSize: 15 }} scroll={{ x: 1200 }} columns={columns} />;
}



export function PerfBaselinePage({ instances, selectedInstanceId }) {
  const instanceId = selectedInstanceId;
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const selectedInstance = (instances || []).find((item) => item.id === instanceId);

  const load = async () => {
    if (!instanceId) return;
    setLoading(true);
    try { setData(await getPerformanceBaseline(instanceId)); }
    catch (e) { message.error(e.message); }
    setLoading(false);
  };
  const collect = async () => {
    if (!instanceId) return;
    setLoading(true);
    try { const r = await collectPerformanceBaseline(instanceId); message.success(`已采集 ${r.count} 项指标${r.errors && r.errors.length ? "（部分失败）" : ""}`); await load(); }
    catch (e) { message.error(e.message); }
    setLoading(false);
  };
  useEffect(() => { load(); }, [instanceId]);

  const devTag = v => v == null ? "-" : <Tag color={Math.abs(v) >= 50 ? "red" : Math.abs(v) >= 20 ? "orange" : "green"}>{v > 0 ? "+" : ""}{v}%</Tag>;
  return (<Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Alert type="info" showIcon message="采集关键指标（CPU/AAS/TPS/QPS/IOPS/Redo），建立日/周/月基线并判断当前偏离。自动采集由「定时任务」统一调度（可在那里开关/调整间隔），也可点「立即采集」手动打点。" />
    <Card>
      <Space wrap>
        <CurrentInstanceSummary instance={selectedInstance} />
        <Button type="primary" icon={<LineChartOutlined />} loading={loading} onClick={collect}>立即采集</Button>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
        {data && <Text type="secondary">累计样本 {data.total_samples} 条</Text>}
      </Space>
    </Card>
    {!instanceId ? <OpsEmptyState title="请选择实例" description="选择实例后可采集性能指标，建立日/周/月基线。" /> :
      <Card size="small">
        <Table dataSource={data?.items || []} rowKey="metric" size="small" loading={loading} pagination={false} scroll={{ x: 800 }}
          locale={{ emptyText: "暂无样本，点「立即采集」开始打点（基线需多次采集积累）" }}
          columns={[
            { title: "指标", dataIndex: "label", width: 180 },
            { title: "当前值", dataIndex: "current", width: 120, render: v => v == null ? "-" : Number(v).toLocaleString() },
            { title: "日基线", dataIndex: "day_avg", width: 120, render: v => v == null ? "-" : Number(v).toLocaleString() },
            { title: "周基线", dataIndex: "week_avg", width: 120, render: v => v == null ? "-" : Number(v).toLocaleString() },
            { title: "月基线", dataIndex: "month_avg", width: 120, render: v => v == null ? "-" : Number(v).toLocaleString() },
            { title: "偏离(当前vs周)", dataIndex: "deviation_pct", width: 130, render: devTag },
            { title: "样本", dataIndex: "samples", width: 80 },
          ]} />
      </Card>}
  </Space>);
}

