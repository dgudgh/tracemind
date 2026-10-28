import React, { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Col, InputNumber, Row, Space, Statistic, Table, Tag, message } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { oracleOpsApi } from "./apiClients";
import { CurrentInstanceSummary, OpsEmptyState, PlanOnlyScriptModal, useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";

export function RedoLogPage({ instances, selectedInstanceId }) {
  const oracleInstances = useMemo(() => instances.filter((item) => item.db_type === "oracle"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const selectedInstance = oracleInstances.find((item) => item.id === instanceId);
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState(null);
  const [scriptModal, setScriptModal] = useState({ open: false, title: "", content: "", warnings: [] });
  const [switchFreqData, setSwitchFreqData] = useState(null);
  const [switchFreqLoading, setSwitchFreqLoading] = useState(false);
  const [switchFreqThread, setSwitchFreqThread] = useState(null);

  const loadData = async () => {
    if (!instanceId) return;
    setLoading(true);
    try { setData(await oracleOpsApi.redoLog(instanceId)); } catch (e) { message.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { if (instanceId) loadData(); }, [instanceId]);

  const loadSwitchFrequency = async () => {
    if (!instanceId) return;
    setSwitchFreqLoading(true);
    try {
      const res = await oracleOpsApi.redoLogSwitchFrequency(instanceId, switchFreqThread);
      setSwitchFreqData(res);
    } catch (e) { message.error(e.message); }
    finally { setSwitchFreqLoading(false); }
  };

  const fetchScript = async (request, title) => {
    if (!instanceId) return;
    try {
      const data = await request();
      setScriptModal({ open: true, title, content: data.sql || data.script, warnings: data.warnings || [] });
    } catch (e) { message.error(e.message); }
  };

  const getIntervalColor = (minutes) => {
    if (minutes == null) return "default";
    if (minutes <= 5) return "red";
    if (minutes <= 15) return "orange";
    if (minutes <= 30) return "blue";
    return "green";
  };

  const getIntervalTag = (minutes) => {
    if (minutes == null) return <Tag>-</Tag>;
    return <Tag color={getIntervalColor(minutes)}>{minutes} min</Tag>;
  };

  if (!instanceId) {
    return <OpsEmptyState title="请选择 Oracle 实例" description="选择实例后可查看 Redo 组、成员、切换频率、Redo 等待和扩容方案。" />;
  }

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          <CurrentInstanceSummary instance={selectedInstance} emptyText="请在右上角选择 Oracle 实例" />
          <Button icon={<ReloadOutlined />} loading={loading} onClick={loadData}>刷新</Button>
          <Tag color="blue">Plan Only</Tag>
          {data && <>
            <Button onClick={() => fetchScript(() => oracleOpsApi.redoLogAddGroupScript(instanceId, { size_mb: 1024 }), "添加 Redo Log 组")}>添加组</Button>
            <Button onClick={() => fetchScript(() => oracleOpsApi.redoLogSwitchScript(instanceId), "日志切换")}>切换日志</Button>
            <Button loading={switchFreqLoading} onClick={loadSwitchFrequency}>日志切换频率</Button>
            <InputNumber min={1} max={10} placeholder="Thread#" value={switchFreqThread}
              onChange={(v) => setSwitchFreqThread(v)} style={{ width: 110 }} />
          </>}
        </Space>
      </Card>

      {switchFreqData && (
        <>
          <Row gutter={[16, 16]}>
            <Col xs={6}><Card size="small"><Statistic title="平均切换间隔" value={switchFreqData.summary.avg_interval_minutes} suffix="分钟" valueStyle={{ color: switchFreqData.summary.avg_interval_minutes < 10 ? "#cf1322" : "#52c41a" }} /></Card></Col>
            <Col xs={6}><Card size="small"><Statistic title="最小切换间隔" value={switchFreqData.summary.min_interval_minutes} suffix="分钟" valueStyle={{ color: "#fa8c16" }} /></Card></Col>
            <Col xs={6}><Card size="small"><Statistic title="最大切换间隔" value={switchFreqData.summary.max_interval_minutes} suffix="分钟" /></Card></Col>
            <Col xs={6}><Card size="small"><Statistic title="1小时内切换" value={switchFreqData.summary.switch_count_within_1h} suffix="次" valueStyle={{ color: switchFreqData.summary.switch_count_within_1h > 20 ? "#cf1322" : "#1890ff" }} /></Card></Col>
          </Row>
          {switchFreqData.summary.avg_interval_minutes < 10 && (
            <Alert type="warning" showIcon message="Redo 日志切换频率过高"
              description={`平均切换间隔低于 ${switchFreqData.summary.avg_interval_minutes} 分钟，建议增大 Redo Log 大小或增加 Redo Log 组数量。`} />
          )}
          <Card title={`日志切换频率 (Thread ${switchFreqData.thread})`} size="small">
            <Table dataSource={switchFreqData.records} rowKey="curr_sequence#" size="small"
              pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100] }}
              columns={[
                { title: "前一序列#", dataIndex: "prev_sequence#", width: 110 },
                { title: "前一切换时间", dataIndex: "prev_first_time", width: 170 },
                { title: "当前序列#", dataIndex: "curr_sequence#", width: 110 },
                { title: "当前切换时间", dataIndex: "curr_first_time", width: 170 },
                { title: "切换间隔", dataIndex: "interval_minutes", width: 120, render: (v) => getIntervalTag(v) },
              ]} />
          </Card>
        </>
      )}

      {data && <>
        <Card title="Redo Log 组"><Table dataSource={data.groups} rowKey="group#" size="small" pagination={false}
          columns={[
            { title: "组号", dataIndex: "group#" }, { title: "序列号", dataIndex: "sequence#" },
            { title: "成员数", dataIndex: "members" }, { title: "大小(MB)", dataIndex: "size_mb" },
            { title: "已归档", dataIndex: "archived", render: (v) => <Tag color={v === "YES" ? "green" : "orange"}>{v}</Tag> },
            { title: "状态", dataIndex: "status", render: (v) => <Tag color={v === "CURRENT" ? "blue" : "default"}>{v}</Tag> },
            { title: "操作", render: (_, r) => <Button size="small" danger onClick={() => fetchScript(() => oracleOpsApi.redoLogDropGroupScript(instanceId, r["group#"]), `删除组${r["group#"]}`)}>删除</Button> },
          ]} /></Card>
        <Card title="Redo Log 成员"><Table dataSource={data.members} rowKey="member" size="small" pagination={false}
          columns={[{ title: "组号", dataIndex: "group#" }, { title: "成员路径", dataIndex: "member" }, { title: "类型", dataIndex: "type" }]} /></Card>
        <Card title="Redo 等待事件"><Table dataSource={data.wait_events || []} rowKey="event" size="small" pagination={false}
          columns={[
            { title: "等待事件", dataIndex: "event", ellipsis: true },
            { title: "等待次数", dataIndex: "total_waits", width: 120 },
            { title: "总等待(s)", dataIndex: "time_waited_seconds", width: 120 },
            { title: "平均等待(s)", dataIndex: "avg_wait_seconds", width: 120, render: (v) => v ?? "-" },
          ]} /></Card>
      </>}
      <PlanOnlyScriptModal
        title={scriptModal.title}
        open={scriptModal.open}
        script={scriptModal.content}
        warnings={scriptModal.warnings}
        width={750}
        onClose={() => setScriptModal({ open: false, title: "", content: "", warnings: [] })}
      />
    </Space>
  );
}
