import React, { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Form,
  Input,
  InputNumber,
  Modal,
  Progress,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  message,
} from "antd";
import { CodeOutlined, CopyOutlined } from "@ant-design/icons";
import {
  buildAuditCleanupPlan,
  buildAwrCleanupPlan,
  buildAwrRetentionPlan,
  buildTablespaceExpandPlan,
  getTablespaceCleanupSummary,
  getTablespaceDatafiles,
  getTablespaceOverview,
} from "./modules/oracleTablespaceApi";
import { copyText } from "./modules/clipboard";
import { useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";

export function TablespaceMonitorPage({ instances, selectedInstanceId }) {
  const oracleInstances = useMemo(() => instances.filter((item) => item.db_type === "oracle"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const [loading, setLoading] = useState(false);
  const [overview, setOverview] = useState(null);
  const [selectedTs, setSelectedTs] = useState(null);
  const [datafiles, setDatafiles] = useState(null);
  const [expandModal, setExpandModal] = useState({ open: false });
  const [expandResult, setExpandResult] = useState(null);
  const [cleanupModal, setCleanupModal] = useState({ open: false, type: "", title: "" });
  const [cleanupSummary, setCleanupSummary] = useState(null);
  const [cleanupResult, setCleanupResult] = useState(null);
  const [cleanupLoading, setCleanupLoading] = useState(false);
  const [expandForm] = Form.useForm();
  const [cleanupForm] = Form.useForm();
  const autoextendWatch = Form.useWatch("autoextend", expandForm);

  const loadOverview = async () => {
    if (!instanceId) return;
    setLoading(true);
    setSelectedTs(null);
    setDatafiles(null);
    try {
      const data = await getTablespaceOverview(instanceId);
      setOverview(data);
    } catch (e) { message.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { if (instanceId) loadOverview(); }, [instanceId]);

  const loadDatafiles = async (tsName) => {
    setSelectedTs(tsName);
    try {
      const data = await getTablespaceDatafiles(instanceId, tsName);
      setDatafiles(data);
    } catch (e) { message.error(e.message); }
  };

  const openExpandModal = async (tsName) => {
    expandForm.setFieldsValue({
      tablespace_name: tsName,
      datafile_path: "",
      file_count: 1,
      size_mb: 30720,
      autoextend: false,
      next_mb: 1024,
      maxsize_mb: 30720,
    });
    setExpandResult(null);
    setExpandModal({ open: true });
    try {
      const dfData = await getTablespaceDatafiles(instanceId, tsName);
      const existingFiles = dfData?.datafiles || [];
      if (existingFiles.length > 0) {
        const lastFile = existingFiles[existingFiles.length - 1].file_name;
        let baseName = lastFile;
        const match = lastFile.match(/^(.+?)[-_]?(\d+)?(\.\w+)$/);
        if (match) {
          const prefix = match[1];
          const ext = match[3] || ".dbf";
          const nextNum = existingFiles.length + 1;
          baseName = `${prefix}${nextNum.toString().padStart(2, "0")}${ext}`;
        }
        expandForm.setFieldsValue({ datafile_path: baseName });
      } else {
        expandForm.setFieldsValue({
          datafile_path: `/oradata/${overview?.instance_name || "ORCL"}/${tsName.toLowerCase()}_01.dbf`,
        });
      }
    } catch (e) {
      expandForm.setFieldsValue({
        datafile_path: `/oradata/${overview?.instance_name || "ORCL"}/${tsName.toLowerCase()}_01.dbf`,
      });
    }
  };

  const generateExpandSQL = async () => {
    const values = await expandForm.validateFields();
    try {
      const data = await buildTablespaceExpandPlan(instanceId, values);
      setExpandResult(data);
    } catch (e) { message.error(e.message); }
  };

  const openSpecialCleanup = async (type) => {
    if (!instanceId) return;
    setCleanupModal({ open: true, type, title: type === "audit" ? "AUD 清理方案" : "AWR 清理方案" });
    setCleanupSummary(null);
    setCleanupResult(null);
    cleanupForm.setFieldsValue({
      retention_days: 30,
      include_fga: true,
      include_location_plan: false,
      audit_trail_location: "SYSAUX",
      interval_minutes: 60,
    });
    setCleanupLoading(true);
    try {
      const data = await getTablespaceCleanupSummary(instanceId, type);
      setCleanupSummary(data);
    } catch (e) { message.error(e.message); }
    finally { setCleanupLoading(false); }
  };

  const generateSpecialCleanupSQL = async (action = "cleanup") => {
    if (!instanceId || !cleanupModal.type) return;
    const values = await cleanupForm.validateFields();
    setCleanupLoading(true);
    try {
      let data;
      if (cleanupModal.type === "audit") {
        data = await buildAuditCleanupPlan(instanceId, values);
      } else if (action === "retention") {
        data = await buildAwrRetentionPlan(instanceId, values);
      } else {
        data = await buildAwrCleanupPlan(instanceId, values);
      }
      setCleanupResult(data);
      message.success(data.message || "已生成 SQL");
    } catch (e) { message.error(e.message); }
    finally { setCleanupLoading(false); }
  };

  const riskColor = { normal: "green", warning: "gold", critical: "red" };
  const usageColor = (pct) => (pct >= 95 ? "red" : pct >= 85 ? "orange" : pct >= 70 ? "gold" : "green");

  return (
    <Space direction="vertical" size={16} className="full-width">
      

      {overview && <>
        <Row gutter={[16, 16]}>
          <Col xs={12} md={6}><Card size="small"><Statistic title="表空间总数" value={overview.summary.total_count} suffix="个" /></Card></Col>
          <Col xs={12} md={6}><Card size="small"><Statistic title="总大小" value={overview.summary.total_gb} precision={2} suffix="GB" /></Card></Col>
          <Col xs={12} md={6}><Card size="small"><Statistic title="已用大小" value={overview.summary.used_gb} precision={2} suffix="GB" valueStyle={{ color: "#1890ff" }} /></Card></Col>
          <Col xs={12} md={6}><Card size="small">
            <Statistic title="告警" value={overview.summary.warning_count + overview.summary.critical_count} suffix={` / ${overview.summary.total_count}`}
              valueStyle={{ color: (overview.summary.critical_count > 0) ? "#cf1322" : (overview.summary.warning_count > 0) ? "#fa8c16" : "#3f8600" }} />
          </Card></Col>
        </Row>

        {overview.omf_enabled !== undefined && (
          <Card size="small">
            <Space>
              <Tag color={overview.omf_enabled ? "green" : "default"}>OMF: {overview.omf_enabled ? "已启用" : "未启用"}</Tag>
              {overview.db_create_file_dest && <Tag>路径: {overview.db_create_file_dest}</Tag>}
            </Space>
          </Card>
        )}

        <Card title={`表空间列表(${overview.tablespaces.length})`} size="small">
          <Table
            dataSource={overview.tablespaces}
            rowKey="tablespace_name"
            size="small"
            pagination={{ defaultPageSize: 15 }}
            scroll={{ x: 1200 }}
            onRow={(record) => ({
              onClick: () => loadDatafiles(record.tablespace_name),
              style: { cursor: "pointer", background: selectedTs === record.tablespace_name ? "#e6f7ff" : undefined },
            })}
            columns={[
              { title: "表空间名", dataIndex: "tablespace_name", width: 180, render: (t, r) => <Space><a>{t}</a>{r.bigfile === "YES" && <Tag color="blue">大文件</Tag>}</Space> },
              { title: "类型", dataIndex: "type", width: 100, render: (v) => <Tag color={v === "TEMPORARY" ? "orange" : "blue"}>{v}</Tag> },
              { title: "状态", dataIndex: "status", width: 80, render: (v) => <Tag color={v === "ONLINE" ? "green" : "default"}>{v}</Tag> },
              { title: "总大小(GB)", dataIndex: "total_gb", width: 100, render: (v) => v?.toFixed(2) },
              { title: "已用(GB)", dataIndex: "used_gb", width: 100, render: (v) => v?.toFixed(2) },
              { title: "剩余(GB)", dataIndex: "free_gb", width: 100, render: (v) => v?.toFixed(2) },
              { title: "使用率", dataIndex: "usage_percent", width: 180,
                render: (v, r) => <Space><Progress percent={parseFloat(v)} size="small" strokeColor={usageColor(v)} style={{ width: 100 }} /><span style={{ color: usageColor(v), fontWeight: 600 }}>{v?.toFixed(1)}%</span></Space> },
              { title: "数据文件", dataIndex: "datafile_count", width: 80 },
              { title: "自动扩展", dataIndex: "autoextensible_count", width: 80, render: (v, r) => `${v || 0}/${r.datafile_count || 0}` },
              { title: "风险", dataIndex: "risk_status", width: 70, render: (v) => <Tag color={riskColor[v]}>{v?.toUpperCase()}</Tag> },
              { title: "建议", dataIndex: "recommendation", ellipsis: true, width: 200 },
              { title: "操作", width: 240, render: (_, r) => r.type !== "TEMPORARY" ? (
                <Space size={4} wrap>
                  <Button size="small" type="primary" ghost onClick={(e) => { e.stopPropagation(); openExpandModal(r.tablespace_name); }}>扩容方案</Button>
                  {String(r.tablespace_name).toUpperCase() === "SYSTEM" && <Button size="small" onClick={(e) => { e.stopPropagation(); openSpecialCleanup("audit"); }}>AUD 清理方案</Button>}
                  {String(r.tablespace_name).toUpperCase() === "SYSAUX" && <Button size="small" onClick={(e) => { e.stopPropagation(); openSpecialCleanup("awr"); }}>AWR 清理方案</Button>}
                </Space>
              ) : null },
            ]}
          />
        </Card>

        {datafiles && (
          <Card title={`${selectedTs} 数据文件详情`} size="small" extra={<Button size="small" onClick={() => { setDatafiles(null); setSelectedTs(null); }}>关闭</Button>}>
            <Table dataSource={datafiles.datafiles} rowKey="file_id" size="small" pagination={false} scroll={{ x: true }}
              columns={[
                { title: "文件ID", dataIndex: "file_id", width: 60 },
                { title: "文件路径", dataIndex: "file_name", ellipsis: true },
                { title: "大小(GB)", dataIndex: "size_gb", width: 100, render: (v) => parseFloat(v || 0).toFixed(2) },
                { title: "最大(GB)", dataIndex: "maxsize_gb", width: 100, render: (v) => parseFloat(v || 0).toFixed(2) },
                { title: "自动扩展", dataIndex: "autoextensible", width: 80, render: (v) => <Tag color={v === "YES" ? "green" : "default"}>{v}</Tag> },
                { title: "状态", dataIndex: "status", width: 80 },
              ]}
            />
          </Card>
        )}
      </>}

      <Modal title="表空间扩容" open={expandModal.open} width={700}
        onCancel={() => setExpandModal({ open: false })}
        footer={[
          <Button key="gen" type="primary" onClick={generateExpandSQL}>生成扩容 SQL</Button>,
          expandResult && <Button key="copy" icon={<CopyOutlined />} onClick={() => copyText(expandResult.sql).then(() => message.success("已复制")).catch(() => message.error("复制失败"))}>复制 SQL</Button>,
          <Button key="close" onClick={() => setExpandModal({ open: false })}>关闭</Button>,
        ]}
      >
        <Form form={expandForm} layout="vertical">
          <Form.Item name="tablespace_name" label="表空间名"><Input disabled /></Form.Item>
          <Form.Item name="datafile_path" label="数据文件路径（不含序号后缀）" rules={[{ required: true }]}>
            <Input placeholder="/oradata/ORCL/users" />
          </Form.Item>
          <Row gutter={16}>
              <Col span={8}><Form.Item name="file_count" label="文件数量" rules={[{ required: true }]}><InputNumber className="full-width" min={1} max={20} /></Form.Item></Col>
              <Col span={8}><Form.Item name="size_mb" label="每文件大小(MB)" rules={[{ required: true }]}><InputNumber className="full-width" min={50} /></Form.Item></Col>
              <Col span={8}><Form.Item name="autoextend" valuePropName="checked"><Checkbox>启用自动扩展</Checkbox></Form.Item></Col>
            </Row>
            {autoextendWatch && (
              <Row gutter={16}>
                <Col span={12}><Form.Item name="next_mb" label="下次扩展(MB)" rules={[{ required: true }]}><InputNumber className="full-width" min={50} /></Form.Item></Col>
                <Col span={12}><Form.Item name="maxsize_mb" label="最大(MB)" rules={[{ required: true }]}><InputNumber className="full-width" min={100} /></Form.Item></Col>
              </Row>
            )}
        </Form>
        {expandResult && (
          <div style={{ marginTop: 12 }}>
            {expandResult.warnings?.length > 0 && <Alert type="warning" showIcon style={{ marginBottom: 8 }} description={<ul style={{ margin: 0, paddingLeft: 20 }}>{expandResult.warnings.map((w) => <li key={w}>{w}</li>)}</ul>} />}
            <pre className="ops-output">{expandResult.sql}</pre>
          </div>
        )}
      </Modal>

      <Modal title={cleanupModal.title} open={cleanupModal.open} width={900}
        onCancel={() => setCleanupModal({ open: false, type: "", title: "" })}
        footer={[
          cleanupResult && <Button key="copy" icon={<CopyOutlined />} onClick={() => copyText(cleanupResult.sql || "").then(() => message.success("已复制")).catch(() => message.error("复制失败"))}>复制 SQL</Button>,
          <Button key="close" type="primary" onClick={() => setCleanupModal({ open: false, type: "", title: "" })}>关闭</Button>,
        ]}
      >
        <Spin spinning={cleanupLoading}>
          {cleanupModal.type === "audit" && (
            <Space direction="vertical" className="full-width" size={12}>
              <Alert type="warning" showIcon message="SYSTEM 表空间优先治理审计对象，不建议直接扩容。" description="清理脚本使用 DBMS_AUDIT_MGMT，不直接 DELETE SYS.AUD$ / SYS.FGA_LOG$。" />
              {cleanupSummary && <Table dataSource={cleanupSummary.items || []} rowKey={(r) => `${r.owner}-${r.segment_name}`} size="small" pagination={false}
                columns={[
                  { title: "Owner", dataIndex: "owner", width: 90 },
                  { title: "对象", dataIndex: "segment_name", width: 140 },
                  { title: "类型", dataIndex: "segment_type", width: 140 },
                  { title: "表空间", dataIndex: "tablespace_name", width: 120 },
                  { title: "大小(MB)", dataIndex: "size_mb", width: 120, render: (v) => Number(v || 0).toFixed(2) },
                ]} />}
              <Form form={cleanupForm} layout="vertical">
                <Row gutter={16}>
                  <Col span={8}><Form.Item name="retention_days" label="保留天数" rules={[{ required: true }]}><InputNumber min={1} max={3650} className="full-width" /></Form.Item></Col>
                  <Col span={8}><Form.Item name="include_fga" valuePropName="checked" label="FGA"><Checkbox>包含 FGA_LOG$ 清理</Checkbox></Form.Item></Col>
                  <Col span={8}><Form.Item name="include_location_plan" valuePropName="checked" label="迁移建议"><Checkbox>生成审计表迁移建议</Checkbox></Form.Item></Col>
                </Row>
                <Form.Item name="audit_trail_location" label="审计表迁移目标表空间"><Input placeholder="SYSAUX" /></Form.Item>
                <Button type="primary" icon={<CodeOutlined />} onClick={() => generateSpecialCleanupSQL("cleanup")}>生成 AUD 清理 SQL</Button>
              </Form>
            </Space>
          )}
          {cleanupModal.type === "awr" && (
            <Space direction="vertical" className="full-width" size={12}>
              <Alert type="warning" showIcon message="SYSAUX 表空间优先分析 AWR/ASH 占用，不建议直接扩容。" description="清理脚本使用 DBMS_WORKLOAD_REPOSITORY，不直接 DELETE WRH$_ / WRI$_ 表。" />
              {cleanupSummary && <>
                <Card size="small" title="SYSAUX 占用构成">
                  <Table dataSource={cleanupSummary.occupants || []} rowKey="occupant_name" size="small" pagination={{ defaultPageSize: 8 }}
                    columns={[
                      { title: "Occupant", dataIndex: "occupant_name", width: 180 },
                      { title: "说明", dataIndex: "occupant_desc", ellipsis: true },
                      { title: "Schema", dataIndex: "schema_name", width: 120 },
                      { title: "大小(MB)", dataIndex: "space_usage_mb", width: 120, render: (v) => Number(v || 0).toFixed(2) },
                    ]} />
                </Card>
                <Card size="small" title="AWR 快照范围">
                  <Table dataSource={cleanupSummary.snapshots || []} rowKey={(r) => `${r.dbid}-${r.instance_number}`} size="small" pagination={false}
                    columns={[
                      { title: "DBID", dataIndex: "dbid", width: 140 },
                      { title: "实例", dataIndex: "instance_number", width: 80 },
                      { title: "最小 Snap", dataIndex: "min_snap_id", width: 110 },
                      { title: "最大 Snap", dataIndex: "max_snap_id", width: 110 },
                      { title: "数量", dataIndex: "snap_count", width: 90 },
                      { title: "最早时间", dataIndex: "min_time", width: 180 },
                      { title: "最新时间", dataIndex: "max_time", width: 180 },
                    ]} />
                </Card>
              </>}
              <Form form={cleanupForm} layout="vertical">
                <Row gutter={16}>
                  <Col span={8}><Form.Item name="retention_days" label="保留天数" rules={[{ required: true }]}><InputNumber min={1} max={3650} className="full-width" /></Form.Item></Col>
                  <Col span={8}><Form.Item name="interval_minutes" label="采集间隔(分钟)" rules={[{ required: true }]}><InputNumber min={10} max={1440} className="full-width" /></Form.Item></Col>
                </Row>
                <Space>
                  <Button type="primary" icon={<CodeOutlined />} onClick={() => generateSpecialCleanupSQL("cleanup")}>生成 AWR 清理 SQL</Button>
                  <Button icon={<CodeOutlined />} onClick={() => generateSpecialCleanupSQL("retention")}>生成保留策略 SQL</Button>
                </Space>
              </Form>
            </Space>
          )}
          {cleanupResult && (
            <div style={{ marginTop: 12 }}>
              {cleanupResult.warnings?.length > 0 && <Alert type="warning" showIcon style={{ marginBottom: 8 }} description={<ul style={{ margin: 0, paddingLeft: 20 }}>{cleanupResult.warnings.map((w) => <li key={w}>{w}</li>)}</ul>} />}
              <pre className="ops-output">{cleanupResult.sql}</pre>
            </div>
          )}
        </Spin>
      </Modal>
    </Space>
  );
}
