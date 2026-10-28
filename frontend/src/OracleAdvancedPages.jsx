import React, { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Form,
  InputNumber,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import {
  CodeOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { oracleOpsApi } from "./apiClients";
import { CurrentInstanceSummary, OpsEmptyState, PlanOnlyScriptModal } from "./OpsLayout";

const { Title, Text } = Typography;

function useSyncedInstanceId(selectedInstanceId, availableInstances) {
  const [instanceId, setInstanceId] = useState();

  useEffect(() => {
    if (selectedInstanceId && availableInstances.some((item) => item.id === selectedInstanceId)) {
      setInstanceId(selectedInstanceId);
    } else {
      setInstanceId(undefined);
    }
  }, [selectedInstanceId, availableInstances]);

  return [instanceId, setInstanceId];
}
function OracleUndoPage({ instances, selectedInstanceId }) {
  const oracleInstances = useMemo(() => instances.filter((item) => item.db_type === "oracle"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const [activeTab, setActiveTab] = useState("overview");
  const [loading, setLoading] = useState(false);
  const [overview, setOverview] = useState(null);
  const [extents, setExtents] = useState({ items: [], summary: [] });
  const [transactions, setTransactions] = useState([]);
  const [history, setHistory] = useState([]);
  const [plan, setPlan] = useState(null);
  const [planForm] = Form.useForm();

  const selectedInstance = oracleInstances.find((item) => item.id === instanceId);

  const loadData = async (targetId = instanceId) => {
    if (!targetId) return;
    setLoading(true);
    try {
      const [overviewData, extentsData, transactionData, historyData] = await Promise.all([
        oracleOpsApi.undoOverview(targetId),
        oracleOpsApi.undoExtents(targetId),
        oracleOpsApi.undoTransactions(targetId),
        oracleOpsApi.undoHistory(targetId),
      ]);
      setOverview(overviewData);
      setExtents(extentsData || { items: [], summary: [] });
      setTransactions(transactionData.items || []);
      setHistory(historyData || []);
      planForm.setFieldsValue({
        retention_seconds: overviewData.undo_retention || 3600,
        scope: "BOTH",
        rac_all_instances: true,
      });
    } catch (e) {
      message.error(e.message || "UNDO 信息加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (instanceId) loadData(instanceId);
  }, [instanceId]);

  const riskColor = (level) => ({ normal: "green", warning: "orange", critical: "red" }[level] || "blue");
  const mbText = (value) => `${Number(value || 0).toFixed(2)} MB`;
  const gbText = (value) => `${(Number(value || 0) / 1024).toFixed(2)} GB`;
  const statusLabel = {
    ACTIVE: "活跃事务占用",
    UNEXPIRED: "未过期保留空间",
    EXPIRED: "已过期可复用空间",
    FREE: "空闲空间",
  };

  const createRetentionPlan = async () => {
    if (!instanceId) return;
    try {
      const values = await planForm.validateFields();
      const data = await oracleOpsApi.undoRetentionPlan(instanceId, values);
      setPlan(data);
      message.success("已生成 UNDO_RETENTION 修改 SQL");
    } catch (e) {
      if (e.message) message.error(e.message);
    }
  };

  const overviewTab = (
    <Space direction="vertical" className="full-width" size={16}>
      {overview && (
        <>
          <Row gutter={[16, 16]}>
            <Col xs={24} md={6}><Card size="small"><Statistic title="当前 UNDO 表空间" value={overview.undo_tablespace || "-"} /></Card></Col>
            <Col xs={24} md={6}><Card size="small"><Statistic title="当前保留时间" value={overview.undo_retention || 0} suffix="秒" /></Card></Col>
            <Col xs={24} md={6}><Card size="small"><Statistic title="实际调优保留时间" value={overview.tuned_undoretention || 0} suffix="秒" /></Card></Col>
            <Col xs={24} md={6}>
              <Card size="small">
                <Statistic title="风险等级" value={overview.risk_level || "normal"} valueStyle={{ color: riskColor(overview.risk_level) }} />
              </Card>
            </Col>
          </Row>
          <Row gutter={[16, 16]}>
            <Col xs={24} md={6}><Card size="small"><Statistic title="UNDO 总空间" value={gbText(overview.total_mb)} /></Card></Col>
            <Col xs={24} md={6}><Card size="small"><Statistic title="ACTIVE" value={mbText(overview.active_mb)} suffix={`${overview.active_pct || 0}%`} valueStyle={{ color: "#cf1322" }} /></Card></Col>
            <Col xs={24} md={6}><Card size="small"><Statistic title="UNEXPIRED" value={mbText(overview.unexpired_mb)} suffix={`${overview.unexpired_pct || 0}%`} valueStyle={{ color: "#d46b08" }} /></Card></Col>
            <Col xs={24} md={6}><Card size="small"><Statistic title="EXPIRED / FREE" value={mbText((overview.expired_mb || 0) + (overview.free_mb || 0))} suffix={`${Number((overview.expired_pct || 0) + (overview.free_pct || 0)).toFixed(2)}%`} valueStyle={{ color: "#389e0d" }} /></Card></Col>
          </Row>
          <Card title="UNDO 空间状态" size="small">
            <Space direction="vertical" className="full-width">
              <div><Text strong>ACTIVE 活跃事务占用</Text><Progress percent={overview.active_pct || 0} strokeColor="#cf1322" /></div>
              <div><Text strong>UNEXPIRED 未过期保留空间</Text><Progress percent={overview.unexpired_pct || 0} strokeColor="#d46b08" /></div>
              <div><Text strong>EXPIRED 已过期可复用空间</Text><Progress percent={overview.expired_pct || 0} strokeColor="#1677ff" /></div>
              <div><Text strong>FREE 空闲空间</Text><Progress percent={overview.free_pct || 0} strokeColor="#389e0d" /></div>
            </Space>
          </Card>
          <Alert
            type={overview.risk_level === "critical" ? "error" : overview.risk_level === "warning" ? "warning" : "success"}
            showIcon
            message="ORA-01555 / UNDO 风险分析"
            description={(overview.suggestions || []).join(" ")}
          />
          <Table
            title={() => "UNDO 参数"}
            dataSource={overview.parameters || []}
            rowKey="name"
            size="small"
            pagination={false}
            columns={[
              { title: "参数名", dataIndex: "name", width: 180 },
              { title: "当前值", dataIndex: "value", width: 160 },
              { title: "显示值", dataIndex: "display_value", width: 160 },
              { title: "默认", dataIndex: "isdefault", width: 90 },
              { title: "可修改", dataIndex: "issys_modifiable", width: 120 },
            ]}
          />
        </>
      )}
    </Space>
  );

  const extentsTab = (
    <Space direction="vertical" className="full-width" size={16}>
      <Table
        dataSource={extents.summary || []}
        rowKey="tablespace_name"
        size="small"
        pagination={false}
        columns={[
          { title: "UNDO 表空间", dataIndex: "tablespace_name", width: 150 },
          { title: "总空间", dataIndex: "total_mb", width: 120, render: gbText },
          { title: "ACTIVE", dataIndex: "active_mb", width: 120, render: mbText },
          { title: "UNEXPIRED", dataIndex: "unexpired_mb", width: 120, render: mbText },
          { title: "EXPIRED", dataIndex: "expired_mb", width: 120, render: mbText },
          { title: "FREE", dataIndex: "free_mb", width: 120, render: mbText },
        ]}
      />
      <Table
        dataSource={extents.items || []}
        rowKey={(row) => `${row.tablespace_name}-${row.status}`}
        size="small"
        pagination={{ defaultPageSize: 10 }}
        columns={[
          { title: "UNDO 表空间", dataIndex: "tablespace_name", width: 150 },
          { title: "状态", dataIndex: "status", width: 130, render: (v) => <Tag color={v === "ACTIVE" ? "red" : v === "UNEXPIRED" ? "orange" : "green"}>{v}</Tag> },
          { title: "中文含义", dataIndex: "status", width: 160, render: (v) => statusLabel[v] || v },
          { title: "Extent 数", dataIndex: "extent_count", width: 100 },
          { title: "大小", dataIndex: "size_mb", width: 120, render: mbText },
          { title: "占比", dataIndex: "pct", width: 180, render: (v) => <Progress percent={Number(v || 0)} size="small" /> },
        ]}
      />
    </Space>
  );

  const transactionsTab = (
    <Table
      dataSource={transactions}
      rowKey={(row) => `${row.sid}-${row.serial_num}-${row.sql_id || ""}`}
      size="small"
      pagination={{ defaultPageSize: 10 }}
      scroll={{ x: 1200 }}
      columns={[
        { title: "SID", dataIndex: "sid", width: 80 },
        { title: "SERIAL#", dataIndex: "serial_num", width: 90 },
        { title: "用户", dataIndex: "username", width: 120 },
        { title: "机器", dataIndex: "machine", width: 160, ellipsis: true },
        { title: "程序", dataIndex: "program", width: 220, ellipsis: true },
        { title: "状态", dataIndex: "status", width: 90 },
        { title: "开始时间", dataIndex: "start_time", width: 180 },
        { title: "USED_UBLK", dataIndex: "used_ublk", width: 110, sorter: (a, b) => (a.used_ublk || 0) - (b.used_ublk || 0) },
        { title: "UNDO MB", dataIndex: "undo_mb", width: 110, sorter: (a, b) => (a.undo_mb || 0) - (b.undo_mb || 0) },
        { title: "SQL_ID", dataIndex: "sql_id", width: 140 },
      ]}
    />
  );

  const retentionTab = (
    <Space direction="vertical" className="full-width" size={16}>
      <Alert type="info" showIcon message="只生成 SQL，不直接执行" description="请在维护窗口人工确认后执行。RAC 环境可勾选 SID='*'，单实例环境可取消勾选。" />
      <Card title="UNDO_RETENTION 调整方案" size="small">
        <Form form={planForm} layout="vertical" initialValues={{ retention_seconds: 3600, scope: "BOTH", rac_all_instances: true }}>
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item name="retention_seconds" label="保留时间（秒）" rules={[{ required: true, message: "请输入保留时间" }]}>
                <InputNumber min={60} max={31536000} className="full-width" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="scope" label="SCOPE" rules={[{ required: true }]}>
                <Select options={[{ value: "BOTH", label: "BOTH" }, { value: "MEMORY", label: "MEMORY" }, { value: "SPFILE", label: "SPFILE" }]} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="rac_all_instances" valuePropName="checked" label="RAC">
                <Checkbox>生成 SID='*'</Checkbox>
              </Form.Item>
            </Col>
          </Row>
          <Button type="primary" icon={<CodeOutlined />} disabled={!instanceId} onClick={createRetentionPlan}>生成修改 SQL</Button>
        </Form>
      </Card>
    </Space>
  );

  const historyTab = (
    <Space direction="vertical" className="full-width" size={16}>
      <Row gutter={[16, 16]}>
        <Col xs={24} md={6}><Card size="small"><Statistic title="ORA-01555 次数" value={overview?.ssolderrcnt || 0} valueStyle={{ color: (overview?.ssolderrcnt || 0) > 0 ? "#cf1322" : "#389e0d" }} /></Card></Col>
        <Col xs={24} md={6}><Card size="small"><Statistic title="空间不足次数" value={overview?.nospaceerrcnt || 0} valueStyle={{ color: (overview?.nospaceerrcnt || 0) > 0 ? "#cf1322" : "#389e0d" }} /></Card></Col>
        <Col xs={24} md={6}><Card size="small"><Statistic title="未过期抢占" value={overview?.unxpstealcnt || 0} valueStyle={{ color: (overview?.unxpstealcnt || 0) > 0 ? "#d46b08" : "#389e0d" }} /></Card></Col>
        <Col xs={24} md={6}><Card size="small"><Statistic title="最长查询" value={overview?.maxquerylen || 0} suffix="秒" /></Card></Col>
      </Row>
      <Table
        dataSource={history}
        rowKey={(row) => `${row.begin_time}-${row.end_time}`}
        size="small"
        pagination={{ defaultPageSize: 12 }}
        scroll={{ x: 1200 }}
        columns={[
          { title: "开始时间", dataIndex: "begin_time", width: 180 },
          { title: "结束时间", dataIndex: "end_time", width: 180 },
          { title: "UNDO 块", dataIndex: "undoblks", width: 100 },
          { title: "事务数", dataIndex: "txncount", width: 100 },
          { title: "最长查询(秒)", dataIndex: "maxquerylen", width: 120 },
          { title: "调优保留(秒)", dataIndex: "tuned_undoretention", width: 130 },
          { title: "ORA-01555", dataIndex: "ssolderrcnt", width: 100 },
          { title: "空间不足", dataIndex: "nospaceerrcnt", width: 100 },
          { title: "UNEXPIRED 抢占", dataIndex: "unxpstealcnt", width: 140 },
          { title: "EXPIRED 复用", dataIndex: "expstealcnt", width: 120 },
        ]}
      />
    </Space>
  );

  return (
    <Space direction="vertical" size={16} className="full-width">
      {!instanceId ? (
        <OpsEmptyState title="请选择 Oracle 实例" description="选择实例后可查看 UNDO 参数、空间状态、活跃事务、历史趋势和 UNDO_RETENTION 调整方案。" />
      ) : (
      <>
      <Card>
        <Space wrap>
          <CurrentInstanceSummary instance={selectedInstance} emptyText="请在右上角选择 Oracle 实例" />
          <Button icon={<ReloadOutlined />} loading={loading} disabled={!instanceId} onClick={() => loadData()}>刷新</Button>
          {selectedInstance && <Tag color="blue">{selectedInstance.env_type || "未标注环境"} - {selectedInstance.factory_name || "未标注角色"}</Tag>}
          <Tag color="blue">Plan Only</Tag>
        </Space>
      </Card>
      <Spin spinning={loading}>
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            { key: "overview", label: "概览", children: overviewTab },
            { key: "extents", label: "空间明细", children: extentsTab },
            { key: "transactions", label: "活跃事务", children: transactionsTab },
            { key: "retention", label: "保留时间调整", children: retentionTab },
            { key: "history", label: "历史趋势", children: historyTab },
          ]}
        />
      </Spin>
      <PlanOnlyScriptModal
        title="UNDO_RETENTION 调整方案"
        open={!!plan}
        script={plan?.sql}
        warnings={plan?.warnings || [plan?.message].filter(Boolean)}
        onClose={() => setPlan(null)}
      />
      </>
      )}
    </Space>
  );
}

export { OracleUndoPage };
