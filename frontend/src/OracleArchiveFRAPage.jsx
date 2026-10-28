import React, { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Progress,
  Row,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  message,
} from "antd";
import { oracleOpsApi } from "./apiClients";
import { PlanOnlyScriptModal, useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";

export function ArchiveFRAPage({ instances, selectedInstanceId }) {
  const oracleInstances = useMemo(() => instances.filter((item) => item.db_type === "oracle"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const [activeTab, setActiveTab] = useState("status");
  const [loading, setLoading] = useState(false);
  const [archiveConfig, setArchiveConfig] = useState(null);
  const [spaceDetail, setSpaceDetail] = useState(null);
  const [flashbackStatus, setFlashbackStatus] = useState(null);
  const [scriptModal, setScriptModal] = useState({ open: false, title: "", content: "", warnings: [] });

  const loadData = async () => {
    if (!instanceId) return;
    setLoading(true);
    try {
      const [config, detail, flashback] = await Promise.all([
        oracleOpsApi.archiveConfig(instanceId),
        oracleOpsApi.archiveSpaceDetail(instanceId),
        oracleOpsApi.flashbackStatus(instanceId),
      ]);
      setArchiveConfig(config);
      setSpaceDetail(detail);
      setFlashbackStatus(flashback);
    } catch (e) { message.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { if (instanceId) loadData(); }, [instanceId]);

  const fetchScript = async (request, title) => {
    if (!instanceId) return;
    try {
      const data = await request();
      setScriptModal({ open: true, title, content: data.sql || data.script, warnings: data.warnings || [] });
    } catch (e) { message.error(e.message); }
  };

  const emergencyScript = async (mode, title) => {
    if (!instanceId) return;
    try {
      const data = await oracleOpsApi.archiveEmergencyScript(instanceId, mode);
      setScriptModal({ open: true, title, content: data.sql, warnings: data.warnings || [] });
    } catch (e) { message.error(e.message); }
  };

  const statusColor = (pct) => (pct >= 95 ? "red" : pct >= 85 ? "orange" : "green");
  const riskColor = (level) => ({ critical: "red", warning: "orange", info: "blue", normal: "green" }[level] || "default");
  const archiveRuntimeColor = (value) => {
    const status = String(value || "").toUpperCase();
    if (status === "VALID") return "green";
    if (status === "ERROR") return "red";
    if (status === "DEFERRED" || status === "INACTIVE") return "orange";
    return "default";
  };

  return (
    <Space direction="vertical" size={16} className="full-width">
      

      <Card>
        <Tabs activeKey={activeTab} onChange={setActiveTab} items={[
          { key: "status", label: "归档状态" },
          { key: "space", label: "归档空间" },
          { key: "fra", label: "FRA 详情" },
          { key: "flashback", label: "Flashback" },
          { key: "logs", label: "归档日志" },
          { key: "settings", label: "归档设置" },
          { key: "emergency", label: "⚠️ 应急处理" },
        ]} />

        {activeTab === "status" && archiveConfig && (
          <Space direction="vertical" className="full-width">
            {archiveConfig.mode && archiveConfig.mode.length > 0 && (
              <Descriptions column={2} bordered size="small" title="归档模式">
                <Descriptions.Item label="日志模式"><Tag color={archiveConfig.mode[0]?.log_mode === "ARCHIVELOG" ? "green" : "orange"}>{archiveConfig.mode[0]?.log_mode}</Tag></Descriptions.Item>
                <Descriptions.Item label="归档进程">{archiveConfig.mode[0]?.archiver}</Descriptions.Item>
              </Descriptions>
            )}
            {archiveConfig.destinations.length > 0 && (
              <Card title="归档目标" size="small" style={{ marginTop: 8 }}>
                <Table dataSource={archiveConfig.destinations} rowKey="dest_id" size="small" pagination={false}
                  columns={[
                    { title: "目标ID", dataIndex: "dest_id", width: 70 },
                    { title: "名称", dataIndex: "dest_name", width: 140, ellipsis: true },
                    { title: "路径", dataIndex: "destination", ellipsis: true },
                    { title: "路径开关", dataIndex: "path_state", width: 100, render: (v) => <Tag color={v === "ENABLE" ? "green" : v === "DEFER" ? "orange" : "default"}>{v || "-"}</Tag> },
                    { title: "运行状态", dataIndex: "runtime_status", width: 100, render: (v) => <Tag color={archiveRuntimeColor(v)}>{v || "-"}</Tag> },
                    { title: "风险", dataIndex: "risk_level", width: 80, render: (v) => <Tag color={riskColor(v)}>{v || "-"}</Tag> },
                    { title: "错误", dataIndex: "error", width: 180, ellipsis: true, render: (v) => v || "-" },
                    { title: "目标", dataIndex: "target", width: 90 },
                    { title: "DB_UNIQUE_NAME", dataIndex: "db_unique_name", width: 160, ellipsis: true },
                  ]} />
              </Card>
            )}
          </Space>
        )}

        {activeTab === "space" && spaceDetail && (
          <Space direction="vertical" className="full-width">
            {spaceDetail.fra.length > 0 && (
              <Row gutter={[16, 16]}>
                <Col span={6}><Card size="small"><Statistic title="FRA 限制(GB)" value={parseFloat(spaceDetail.fra[0].limit_gb || 0).toFixed(2)} /></Card></Col>
                <Col span={6}><Card size="small"><Statistic title="已用(GB)" value={parseFloat(spaceDetail.fra[0].used_gb || 0).toFixed(2)} valueStyle={{ color: statusColor(spaceDetail.fra_used_pct) }} /></Card></Col>
                <Col span={6}><Card size="small"><Statistic title="可回收(GB)" value={parseFloat(spaceDetail.fra[0].reclaimable_gb || 0).toFixed(2)} /></Card></Col>
                <Col span={6}><Card size="small"><Statistic title="使用率" value={spaceDetail.fra_used_pct} suffix="%" valueStyle={{ color: statusColor(spaceDetail.fra_used_pct) }} /></Card></Col>
              </Row>
            )}
            <Progress percent={spaceDetail.fra_used_pct} strokeColor={statusColor(spaceDetail.fra_used_pct)} style={{ maxWidth: 400 }} />
            {spaceDetail.usage_by_type.length > 0 && (
              <Card title="空间分布" size="small">
                <Table dataSource={spaceDetail.usage_by_type} rowKey="file_type" size="small" pagination={false}
                  columns={[{ title: "文件类型", dataIndex: "file_type" }, { title: "空间占比(%)", dataIndex: "percent_space_used" }, { title: "可回收(%)", dataIndex: "percent_space_reclaimable" }, { title: "文件数", dataIndex: "number_of_files" }]} />
              </Card>
            )}
            {spaceDetail.log_summary.length > 0 && (
              <Card title="归档日志汇总" size="small">
                <Descriptions column={4} bordered size="small">
                  <Descriptions.Item label="总日志数">{spaceDetail.log_summary[0].log_count}</Descriptions.Item>
                  <Descriptions.Item label="总大小(GB)">{spaceDetail.log_summary[0].total_gb}</Descriptions.Item>
                  <Descriptions.Item label="已归档(GB)">{spaceDetail.log_summary[0].archived_gb}</Descriptions.Item>
                  <Descriptions.Item label="未归档(GB)">{spaceDetail.log_summary[0].not_archived_gb}</Descriptions.Item>
                </Descriptions>
              </Card>
            )}
          </Space>
        )}

        {activeTab === "fra" && spaceDetail && (
          <Space direction="vertical" className="full-width">
            {spaceDetail.fra.length > 0 && (
              <Descriptions column={2} bordered size="small" title="FRA 配置">
                <Descriptions.Item label="路径">{spaceDetail.fra[0].dest_name}</Descriptions.Item>
                <Descriptions.Item label="文件数">{spaceDetail.fra[0].file_count}</Descriptions.Item>
                <Descriptions.Item label="限制(GB)">{parseFloat(spaceDetail.fra[0].limit_gb || 0).toFixed(2)}</Descriptions.Item>
                <Descriptions.Item label="已用(GB)">{parseFloat(spaceDetail.fra[0].used_gb || 0).toFixed(2)}</Descriptions.Item>
              </Descriptions>
            )}
            {spaceDetail.redo_groups.length > 0 && (
              <Card title="Redo Log 组" size="small" style={{ marginTop: 8 }}>
                <Table dataSource={spaceDetail.redo_groups} rowKey="group#" size="small" pagination={false}
                  columns={[{ title: "组号", dataIndex: "group#" }, { title: "序列号", dataIndex: "sequence#" }, { title: "成员数", dataIndex: "members" }, { title: "大小(MB)", dataIndex: "size_mb" }, { title: "已归档", dataIndex: "archived", render: (v) => <Tag color={v === "YES" ? "green" : "orange"}>{v}</Tag> }, { title: "状态", dataIndex: "status", render: (v) => <Tag color={v === "CURRENT" ? "blue" : "default"}>{v}</Tag> }]} />
              </Card>
            )}
          </Space>
        )}

        {activeTab === "flashback" && flashbackStatus && (
          <Space direction="vertical" className="full-width">
            <Alert type="info" showIcon message="Flashback 操作只生成 plan_only 方案，平台不会直接执行 ALTER DATABASE / ALTER SYSTEM。" />
            <Row gutter={[16, 16]}>
              <Col xs={24} md={6}><Card size="small"><Statistic title="Flashback" value={flashbackStatus.flashback_on || "-"} valueStyle={{ color: flashbackStatus.flashback_on === "YES" ? "#3f8600" : "#1677ff" }} /></Card></Col>
              <Col xs={24} md={6}><Card size="small"><Statistic title="FRA 使用率" value={flashbackStatus.fra_used_pct || 0} suffix="%" valueStyle={{ color: statusColor(flashbackStatus.fra_used_pct || 0) }} /></Card></Col>
              <Col xs={24} md={6}><Card size="small"><Statistic title="风险等级" value={flashbackStatus.risk_level || "-"} valueStyle={{ color: flashbackStatus.risk_level === "critical" ? "#cf1322" : flashbackStatus.risk_level === "warning" ? "#d46b08" : "#3f8600" }} /></Card></Col>
              <Col xs={24} md={6}><Card size="small"><Statistic title="Flashback Log" value={(flashbackStatus.flashback_space || [])[0]?.used_gb || 0} suffix="GB" valueStyle={{ color: statusColor(((flashbackStatus.flashback_usage || [])[0]?.percent_space_used || 0) * 2) }} /></Card></Col>
            </Row>
            <Space wrap>
              <Button onClick={() => fetchScript(() => oracleOpsApi.flashbackPlan(instanceId, "enable"), "开启 Flashback 方案")}>开启方案</Button>
              <Button onClick={() => fetchScript(() => oracleOpsApi.flashbackPlan(instanceId, "retention"), "调整保留时间方案")}>调整保留时间</Button>
              <Button onClick={() => fetchScript(() => oracleOpsApi.flashbackPlan(instanceId, "extend_fra"), "扩展 FRA 方案")}>扩展 FRA</Button>
              <Button danger onClick={() => fetchScript(() => oracleOpsApi.flashbackPlan(instanceId, "disable"), "关闭 Flashback 方案")}>关闭方案</Button>
            </Space>
            <Row gutter={[16, 16]}>
              <Col xs={24} lg={12}>
                <Card title="Flashback 参数" size="small">
                  <Table dataSource={flashbackStatus.parameters || []} rowKey="name" size="small" pagination={false}
                    columns={[{ title: "参数", dataIndex: "name" }, { title: "值", dataIndex: "value", ellipsis: true }]} />
                </Card>
              </Col>
              <Col xs={24} lg={12}>
                <Card title="Flashback Log 空间" size="small">
                  <Table dataSource={flashbackStatus.flashback_space || flashbackStatus.flashback_usage || []} rowKey="file_type" size="small" pagination={false}
                    columns={[
                      { title: "文件类型", dataIndex: "file_type" },
                      { title: "已用(%)", dataIndex: "percent_space_used" },
                      { title: "已用(GB)", dataIndex: "used_gb", render: (v) => v ?? "-" },
                      { title: "可回收(%)", dataIndex: "percent_space_reclaimable" },
                      { title: "可回收(GB)", dataIndex: "reclaimable_gb", render: (v) => v ?? "-" },
                      { title: "文件数", dataIndex: "number_of_files" },
                      { title: "风险", dataIndex: "risk_level", render: (v) => <Tag color={riskColor(v)}>{v || "-"}</Tag> },
                    ]} />
                </Card>
              </Col>
            </Row>
          </Space>
        )}

        {activeTab === "logs" && spaceDetail && spaceDetail.recent_logs.length > 0 && (
          <Table dataSource={spaceDetail.recent_logs} rowKey="sequence#" size="small" pagination={{ defaultPageSize: 10 }}
            columns={[{ title: "序列号", dataIndex: "sequence#", width: 80 }, { title: "完成时间", dataIndex: "completion_time", width: 170 }, { title: "大小(MB)", dataIndex: "size_mb", width: 90, render: (v) => parseFloat(v || 0).toFixed(2) }, { title: "已归档", dataIndex: "archived", width: 70, render: (v) => <Tag color={v === "YES" ? "green" : "orange"}>{v}</Tag> }, { title: "已删除", dataIndex: "deleted", width: 70, render: (v) => <Tag color={v === "YES" ? "red" : "default"}>{v}</Tag> }, { title: "路径", dataIndex: "name", ellipsis: true }]} />
        )}

        {activeTab === "settings" && (
          <Space direction="vertical" className="full-width">
            <Alert type="info" showIcon message="归档设置操作仅生成 SQL 脚本，需在维护窗口手工执行。" />
            <Space wrap>
              <Button type="primary" onClick={() => fetchScript(() => oracleOpsApi.archiveEnableScript(instanceId), "开启归档模式")}>开启归档</Button>
              <Button danger onClick={() => fetchScript(() => oracleOpsApi.archiveDisableScript(instanceId), "关闭归档模式")}>关闭归档</Button>
              <Button onClick={() => fetchScript(() => oracleOpsApi.archivePathScript(instanceId, { dest_id: 1, path: "/archivelog" }), "修改归档路径")}>修改归档路径</Button>
            </Space>
            {archiveConfig?.parameters && archiveConfig.parameters.length > 0 && (
              <Card title="归档参数" size="small" style={{ marginTop: 8 }}>
                <Table dataSource={archiveConfig.parameters} rowKey="name" size="small" pagination={false}
                  columns={[{ title: "参数", dataIndex: "name" }, { title: "值", dataIndex: "value" }]} />
              </Card>
            )}
          </Space>
        )}

        {activeTab === "emergency" && (
          <Space direction="vertical" className="full-width">
            <Alert type="error" showIcon message="ORA-00257 应急处理" description="当归档日志占满 FRA 空间导致数据库挂起时，使用以下方案恢复数据库。请谨慎选择处理方式。" />
            <Row gutter={[16, 16]}>
              <Col xs={24} md={8}>
                <Card hoverable title="方案一：增加 FRA" size="small" onClick={() => emergencyScript("extend_fra", "应急处理 - 增加 FRA 大小")}>
                  <p style={{ color: "#059669", fontWeight: 600 }}>推荐 · 最安全</p>
                  <p style={{ color: "#666" }}>增加 db_recovery_file_dest_size，不删除任何归档日志</p>
                </Card>
              </Col>
              <Col xs={24} md={8}>
                <Card hoverable title="方案二：RMAN 清理" size="small" onClick={() => emergencyScript("rman", "应急处理 - RMAN 清理归档")}>
                  <p style={{ color: "#D97706", fontWeight: 600 }}>保留最近 7 天</p>
                  <p style={{ color: "#666" }}>RMAN 清理已备份归档日志，保留最近 7 天</p>
                </Card>
              </Col>
              <Col xs={24} md={8}>
                <Card hoverable title="方案三：强制清理" size="small" onClick={() => emergencyScript("force", "应急处理 - 强制删除所有归档")}>
                  <p style={{ color: "#DC2626", fontWeight: 600 }}>⚠️ 极端紧急</p>
                  <p style={{ color: "#666" }}>强制删除所有归档日志，将无法时间点恢复</p>
                </Card>
              </Col>
            </Row>
            {spaceDetail && spaceDetail.fra.length > 0 && (
              <Card title="当前 FRA 状态" size="small">
                <Descriptions column={3} bordered size="small">
                  <Descriptions.Item label="已用率"><span style={{ color: statusColor(spaceDetail.fra_used_pct), fontWeight: 600 }}>{spaceDetail.fra_used_pct}%</span></Descriptions.Item>
                  <Descriptions.Item label="已用(GB)">{parseFloat(spaceDetail.fra[0].used_gb || 0).toFixed(2)}</Descriptions.Item>
                  <Descriptions.Item label="限制(GB)">{parseFloat(spaceDetail.fra[0].limit_gb || 0).toFixed(2)}</Descriptions.Item>
                </Descriptions>
              </Card>
            )}
          </Space>
        )}
      </Card>

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
