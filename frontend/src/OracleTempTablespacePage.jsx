import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { oracleOpsApi } from "./apiClients";
import { CurrentInstanceSummary, OpsEmptyState, PlanOnlyScriptModal, useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";
import { stableRecordKey } from "./modules/stableKeys";

export function OracleTempTablespacePage({ instances, selectedInstanceId }) {
  const oracleInstances = (instances || []).filter(i => i.db_type === "oracle" && i.enabled !== false);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const selectedInstance = oracleInstances.find((item) => item.id === instanceId);
  const [tab, setTab] = useState("overview");
  const [loading, setLoading] = useState(false);
  const [overview, setOverview] = useState(null);
  const [files, setFiles] = useState(null);
  const [sqlUsage, setSqlUsage] = useState(null);
  const [sessionUsage, setSessionUsage] = useState(null);
  const [extendPlan, setExtendPlan] = useState(null);
  const [extendLoading, setExtendLoading] = useState(false);
  const [extendForm] = Form.useForm();

  const loadData = async (t) => {
    if (!instanceId) return;
    setLoading(true);
    const activeTab = t || tab;
    try {
      if (activeTab === "overview" || !overview) {
        const r = await oracleOpsApi.tempOverview(instanceId);
        setOverview(r);
      }
      if (activeTab === "files" || !files) {
        const r = await oracleOpsApi.tempFiles(instanceId);
        setFiles(r);
      }
      if (activeTab === "sql" || !sqlUsage) {
        const r = await oracleOpsApi.tempSqlUsage(instanceId);
        setSqlUsage(r);
      }
      if (activeTab === "session" || !sessionUsage) {
        const r = await oracleOpsApi.tempSessionUsage(instanceId);
        setSessionUsage(r);
      }
    } catch (e) { message.error(e.message); }
    setLoading(false);
  };

  const genExtendPlan = async () => {
    if (!instanceId) return;
    setExtendLoading(true);
    try {
      const v = await extendForm.validateFields();
      const r = await oracleOpsApi.tempExtendPlan(instanceId, {
        tablespace_name: v.tablespace_name || "TEMP",
        size_mb: v.size_mb || 20480,
        autoextend: v.autoextend !== false,
      });
      setExtendPlan(r);
    } catch (e) { message.error(e.message); }
    setExtendLoading(false);
  };

  useEffect(() => { if (instanceId) { setOverview(null); setFiles(null); setSqlUsage(null); setSessionUsage(null); setExtendPlan(null); loadData("overview"); } }, [instanceId]);

  if (!instanceId) {
    return <OpsEmptyState title="请选择 Oracle 实例" description="选择实例后可查看 TEMP 使用率、TEMP 文件、SQL/会话占用排行和扩容方案。" />;
  }

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          <CurrentInstanceSummary instance={selectedInstance} emptyText="请在右上角选择 Oracle 实例" />
          <Button type="primary" icon={<ReloadOutlined />} loading={loading} disabled={!instanceId} onClick={() => loadData()}>刷新</Button>
          <Tag color="blue">Plan Only</Tag>
        </Space>
      </Card>
      {overview && (
        <Alert style={{ marginBottom: 12 }} type="info" showIcon message={
          <span>数据库角色：<Tag color="blue">{overview.database_role || "-"}</Tag> 打开模式：<Tag>{overview.open_mode || "-"}</Tag></span>
        } />
      )}
      {overview?.risks?.length > 0 && overview.risks.map((r) => (
        <Alert key={stableRecordKey("oracle-temp-overview-risk", r, ["level", "message", "tablespace_name"])} style={{ marginBottom: 8 }} type={r.level === "critical" ? "error" : "warning"} showIcon message={r.message} />
      ))}
      <Tabs activeKey={tab} onChange={(k) => { setTab(k); loadData(k); }} items={[
        { key: "overview", label: "使用率概览", children: (
          <Table dataSource={overview?.items || []} columns={[
            { title: "表空间", dataIndex: "tablespace_name", width: 150 },
            { title: "总大小(MB)", dataIndex: "total_mb", width: 120 },
            { title: "已用(MB)", dataIndex: "used_mb", width: 120 },
            { title: "剩余(MB)", dataIndex: "free_mb", width: 120 },
            { title: "使用率", dataIndex: "used_pct", width: 100, render: v => {
              if (v == null) return "-";
              const color = v > 90 ? "red" : v > 80 ? "orange" : "green";
              return <Tag color={color}>{v}%</Tag>;
            }},
          ]} rowKey="tablespace_name" size="small" pagination={false} />
        )},
        { key: "files", label: "TEMP 文件", children: (
          <div>
            {files?.risks?.length > 0 && files.risks.map((r) => (
              <Alert key={stableRecordKey("oracle-temp-file-risk", r, ["level", "message", "tablespace_name", "file_name"])} style={{ marginBottom: 8 }} type={r.level === "critical" ? "error" : "warning"} showIcon message={r.message} />
            ))}
            <Table dataSource={files?.items || []} columns={[
              { title: "表空间", dataIndex: "tablespace_name", width: 120 },
              { title: "FILE_ID", dataIndex: "file_id", width: 70 },
              { title: "文件路径", dataIndex: "file_name", ellipsis: true },
              { title: "大小(MB)", dataIndex: "size_mb", width: 100 },
              { title: "自动扩展", dataIndex: "autoextensible", width: 90, render: v => v === "YES" ? <Tag color="green">ON</Tag> : <Tag color="red">OFF</Tag> },
              { title: "MAX(MB)", dataIndex: "max_mb", width: 100 },
            ]} rowKey="file_id" size="small" pagination={false} />
          </div>
        )},
        { key: "sql", label: "SQL 占用排行", children: (
          <div>
            {sqlUsage?.risks?.length > 0 && sqlUsage.risks.map((r) => (
              <Alert key={stableRecordKey("oracle-temp-sql-risk", r, ["level", "message", "sql_id", "sid"])} style={{ marginBottom: 8 }} type={r.level === "critical" ? "error" : "warning"} showIcon message={r.message} />
            ))}
            <Table dataSource={sqlUsage?.items || []} columns={[
              { title: "SQL_ID", dataIndex: "sql_id", width: 130, render: v => <Typography.Text copyable={{ text: v }}>{v}</Typography.Text> },
              { title: "会话数", dataIndex: "session_count", width: 80 },
              { title: "TEMP(MB)", dataIndex: "temp_mb", width: 110, render: v => v ? `${v} MB` : "-", sorter: (a, b) => (a.temp_mb || 0) - (b.temp_mb || 0), defaultSortOrder: "descend" },
              { title: "SQL 文本", dataIndex: "sql_text", ellipsis: true },
            ]} rowKey="sql_id" size="small" pagination={{ pageSize: 20 }} />
          </div>
        )},
        { key: "session", label: "会话占用明细", children: (
          <Table dataSource={sessionUsage?.items || []} columns={[
            { title: "SID", dataIndex: "sid", width: 60 },
            { title: "Serial#", dataIndex: "serial#", width: 75 },
            { title: "用户", dataIndex: "username", width: 90 },
            { title: "机器", dataIndex: "machine", width: 130, ellipsis: true },
            { title: "程序", dataIndex: "program", width: 130, ellipsis: true },
            { title: "SEGTYPE", dataIndex: "segtype", width: 80 },
            { title: "TEMP(MB)", dataIndex: "temp_mb", width: 100, render: v => v ? `${v} MB` : "-" },
            { title: "SQL_ID", dataIndex: "sql_id", width: 130 },
            { title: "SQL 文本", dataIndex: "sql_text", ellipsis: true },
          ]} rowKey={(r) => `${r.sid}-${r["serial#"]}`} size="small" pagination={{ pageSize: 20 }} />
        )},
        { key: "extend", label: "扩容方案", children: (
          <div>
            <Form form={extendForm} layout="inline" style={{ marginBottom: 16 }}>
              <Form.Item name="tablespace_name" label="表空间名" initialValue="TEMP"><Input style={{ width: 120 }} /></Form.Item>
              <Form.Item name="size_mb" label="大小(MB)" initialValue={20480}><InputNumber min={100} max={102400} style={{ width: 120 }} /></Form.Item>
              <Form.Item name="autoextend" label="自动扩展" initialValue={true}><Select style={{ width: 80 }} options={[{ value: true, label: "ON" }, { value: false, label: "OFF" }]} /></Form.Item>
              <Form.Item><Button type="primary" loading={extendLoading} onClick={genExtendPlan}>生成扩容 SQL</Button></Form.Item>
            </Form>
            <Alert type="warning" showIcon message="扩容仅生成 SQL，不会直接执行。" description="执行前请确认磁盘/ASM 磁盘组空间、文件路径、变更窗口和回滚方案。" />
          </div>
        )},
      ]} />
      <PlanOnlyScriptModal
        title={`TEMP 扩容方案 - ${extendPlan?.tablespace_name || "TEMP"}`}
        open={!!extendPlan}
        script={extendPlan?.sql}
        warnings={[
          extendPlan?.message,
          extendPlan ? `文件路径：${extendPlan.file_path || "-"}` : "",
          extendPlan ? `文件大小：${extendPlan.size_mb || "-"} MB，自动扩展：${extendPlan.autoextend ? "ON" : "OFF"}` : "",
          "请确认磁盘或 ASM 磁盘组空间后再人工执行。"
        ].filter(Boolean)}
        onClose={() => setExtendPlan(null)}
      />
    </Space>
  );
}
