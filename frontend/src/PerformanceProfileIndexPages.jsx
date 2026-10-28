import React, { useEffect, useState } from "react";
import {
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Input,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { CodeOutlined, ReloadOutlined, RocketOutlined, ThunderboltOutlined } from "@ant-design/icons";
import {
  collectIndexFindings,
  getDatabaseProfile,
  getIndexFindings,
  refreshPerformanceSuggestions,
} from "./modules/performanceApi";
import { CurrentInstanceSummary, OpsEmptyState, PlanOnlyScriptModal } from "./OpsLayout";
import { GRADE_COLOR, SEV_COLOR, SEV_LABEL, SOURCE_LABEL, IDX_TYPE_LABEL, IDX_TYPE_COLOR } from "./PerformancePageConstants";

const { Text } = Typography;

export function DatabaseProfilePage({ instances, selectedInstanceId }) {
  const instanceId = selectedInstanceId;
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(false);
  const selectedInstance = (instances || []).find((item) => item.id === instanceId);

  const load = async () => {
    if (!instanceId) return;
    setLoading(true);
    try { setProfile(await getDatabaseProfile(instanceId)); }
    catch (e) { message.error(e.message); }
    setLoading(false);
  };
  const refresh = async () => {
    if (!instanceId) return;
    setLoading(true);
    try { const r = await refreshPerformanceSuggestions(instanceId); message.success(`已重建 ${r.written} 条`); await load(); }
    catch (e) { message.error(e.message); }
    setLoading(false);
  };
  useEffect(() => { load(); }, [instanceId]);

  const p = profile || {};
  const bySev = p.suggestions_by_severity || {};

  return (<Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Card>
      <Space wrap>
        <CurrentInstanceSummary instance={selectedInstance} />
        <Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={load}>加载画像</Button>
        <Button icon={<RocketOutlined />} loading={loading} onClick={refresh}>重建该实例建议</Button>
      </Space>
    </Card>
    {!instanceId ? <OpsEmptyState title="请选择实例" description="选择实例后可查看数据库评分、风险等级和待处理建议。" /> : <>
      <Card size="small" title={`数据库画像 - ${p.instance_name || instanceId}`}>
        <Descriptions bordered size="small" column={4}>
          <Descriptions.Item label="综合评分">{p.score ?? "-"}</Descriptions.Item>
          <Descriptions.Item label="等级"><Tag color={GRADE_COLOR[p.grade]}>{p.grade || "-"}</Tag></Descriptions.Item>
          <Descriptions.Item label="风险"><Tag color={SEV_COLOR[p.risk_level]}>{SEV_LABEL[p.risk_level] || p.risk_level || "-"}</Tag></Descriptions.Item>
          <Descriptions.Item label="类型">{(p.db_type || "").toUpperCase()}</Descriptions.Item>
          <Descriptions.Item label="待处理建议">{p.open_suggestions ?? 0}</Descriptions.Item>
          <Descriptions.Item label="高/中/低">{`${bySev.high || 0} / ${bySev.medium || 0} / ${bySev.low || 0}`}</Descriptions.Item>
          <Descriptions.Item label="慢SQL">{p.slow_sql_cnt ?? 0}</Descriptions.Item>
          <Descriptions.Item label="容量严重">{p.capacity_critical ?? 0}</Descriptions.Item>
        </Descriptions>
      </Card>
      <Card size="small" title="该实例待处理建议（钻取）">
        <Table dataSource={p.suggestions || []} rowKey="id" size="small" pagination={{ pageSize: 10 }} scroll={{ x: 900 }}
          expandable={{ expandedRowRender: r => <div style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{r.suggestion || "-"}</div> }}
          columns={[
            { title: "来源", dataIndex: "source", width: 80, render: v => <Tag>{SOURCE_LABEL[v] || v}</Tag> },
            { title: "问题类型", dataIndex: "problem_type", width: 180, ellipsis: true },
            { title: "对象", dataIndex: "object_name", width: 140, ellipsis: true, render: v => v || "-" },
            { title: "严重度", dataIndex: "severity", width: 80, render: v => <Tag color={SEV_COLOR[v]}>{SEV_LABEL[v] || v}</Tag> },
            { title: "建议", dataIndex: "suggestion", ellipsis: true, render: v => v || "-" },
          ]} />
      </Card>
    </>}
  </Space>);
}

