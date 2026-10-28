import React, { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { PlayCircleOutlined, PoweroffOutlined, ReloadOutlined } from "@ant-design/icons";
import { oracleOpsApi } from "./apiClients";
import { PlanOnlyScriptModal, useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";
import { normalizeDgMessage } from "./modules/oracleOpsHelpers";

const { Text } = Typography;

export function DbControlPage({ instances, selectedInstanceId }) {
  const oracleInstances = useMemo(() => instances.filter((item) => item.db_type === "oracle"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const [dbStatus, setDbStatus] = useState(null);
  const [loadingStatus, setLoadingStatus] = useState(false);
  const [scriptResult, setScriptResult] = useState(null);
  const [scriptModal, setScriptModal] = useState({ open: false, title: "", content: "", warnings: [] });
  const [bootPrereq, setBootPrereq] = useState(null);
  const [bootLoading, setBootLoading] = useState(false);
  const [bootResult, setBootResult] = useState(null);

  const checkStatus = async () => {
    if (!instanceId) return;
    setLoadingStatus(true);
    try {
      const data = await oracleOpsApi.dbStatus(instanceId);
      setDbStatus(data);
    } catch (error) {
      message.error(error.message);
    } finally {
      setLoadingStatus(false);
    }
  };

  useEffect(() => {
    if (instanceId) checkStatus();
  }, [instanceId]);

  const generateStartup = async (mode) => {
    if (!instanceId) return;
    try {
      const data = await oracleOpsApi.startupScript(instanceId, mode);
      setScriptResult(data);
      setScriptModal({ open: true, title: `启动脚本 (${mode.toUpperCase()})`, content: data.script, warnings: data.warnings });
    } catch (error) {
      message.error(error.message);
    }
  };

  const generateShutdown = async (mode) => {
    if (!instanceId) return;
    try {
      const data = await oracleOpsApi.shutdownScript(instanceId, mode);
      setScriptResult(data);
      setScriptModal({ open: true, title: `关闭脚本 (${mode.toUpperCase()})`, content: data.script, warnings: data.warnings });
    } catch (error) {
      message.error(error.message);
    }
  };

  const checkBootPrerequisites = async () => {
    if (!instanceId) return;
    setBootLoading(true);
    try {
      const data = await oracleOpsApi.bootPrerequisites(instanceId);
      setBootPrereq(data);
    } catch (error) {
      message.error(error.message);
    } finally {
      setBootLoading(false);
    }
  };

  useEffect(() => {
    if (!instanceId) {
      setBootPrereq(null);
      setBootResult(null);
      return;
    }
    setBootPrereq(null);
    setBootResult(null);
    checkBootPrerequisites();
  }, [instanceId]);

  const generateBootScript = async (mode) => {
    if (!instanceId) return;
    setBootLoading(true);
    try {
      const data = await oracleOpsApi.bootScript(instanceId, mode);
      setBootResult(data);
    } catch (error) {
      message.error(error.message);
    } finally {
      setBootLoading(false);
    }
  };

  const statusColor = { OPEN: "green", MOUNT: "blue", NOMOUNT: "orange", DOWN: "red", UNKNOWN: "default" };
  const stepStatusIcon = { success: "green", warning: "orange", failed: "red" };
  const rawDataguard = bootPrereq?.summary?.dataguard;
  const dataguard = useMemo(() => {
    if (!rawDataguard) return rawDataguard;
    const role = String(rawDataguard.database_role || "").toUpperCase();
    const remoteDestIds = rawDataguard.remote_dest_ids || [];
    if (role.includes("PRIMARY") && remoteDestIds.length === 0) {
      return { ...rawDataguard, configured: false, running: false, destinations: [], message: "未配置 DG" };
    }
    return rawDataguard;
  }, [rawDataguard]);

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          
          <Button icon={<ReloadOutlined />} loading={loadingStatus} onClick={checkStatus}>刷新状态</Button>
          {dbStatus && (
            <Tag color={statusColor[dbStatus.status] || "default"} style={{ fontSize: 14, padding: "2px 12px" }}>
              {dbStatus.status}
            </Tag>
          )}
        </Space>
      </Card>

      {dbStatus && (
        <Card title="数据库状态">
          <Descriptions column={2} bordered size="small">
            <Descriptions.Item label="状态"><Tag color={statusColor[dbStatus.status]}>{dbStatus.status}</Tag></Descriptions.Item>
            <Descriptions.Item label="实例名">{dbStatus.instance_name}</Descriptions.Item>
            <Descriptions.Item label="数据库名">{dbStatus.database_name}</Descriptions.Item>
            <Descriptions.Item label="打开模式">{dbStatus.open_mode}</Descriptions.Item>
          </Descriptions>
        </Card>
      )}

      <Card title="引导顺序" extra={<Button icon={<ReloadOutlined />} loading={bootLoading} disabled={!instanceId} onClick={checkBootPrerequisites}>刷新检查</Button>}>
        {bootPrereq ? (
          <Space direction="vertical" size={12} className="full-width">
            {bootPrereq.steps.map((s) => (
              <div key={s.step} style={{ display: "flex", alignItems: "center", gap: 12, padding: "8px 12px", background: "#fafafa", borderRadius: 6 }}>
                <div style={{ width: 28, height: 28, borderRadius: "50%", background: stepStatusIcon[s.status] || "#d9d9d9", color: "#fff", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 600, fontSize: 14, flexShrink: 0 }}>
                  {s.step}
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 500 }}>{s.name}</div>
                  <div style={{ fontSize: 12, color: "#666" }}>{s.key === "dataguard_status" ? (dataguard?.message || "未配置 DG") : normalizeDgMessage(s.message)}</div>
                </div>
                <Tag color={stepStatusIcon[s.status]}>{s.status === "success" ? "通过" : s.status === "warning" ? "警告" : "未通过"}</Tag>
              </div>
            ))}
            {dataguard?.configured && (
              <Card
                title="Data Guard 状态"
                size="small"
                extra={<Tag color={dataguard.running ? "green" : "orange"}>{dataguard.running ? "运行中" : "需要检查"}</Tag>}
              >
                <Space direction="vertical" size={12} className="full-width">
                  <Alert
                    type={dataguard.running ? "success" : "warning"}
                    showIcon
                    message={normalizeDgMessage(dataguard.message) || "已检测到 Data Guard 配置"}
                  />
                  <Descriptions column={2} bordered size="small">
                    <Descriptions.Item label="数据库角色">{dataguard.database_role || "-"}</Descriptions.Item>
                    <Descriptions.Item label="打开模式">{dataguard.open_mode || "-"}</Descriptions.Item>
                    <Descriptions.Item label="保护模式">{dataguard.protection_mode || "-"}</Descriptions.Item>
                    <Descriptions.Item label="保护级别">{dataguard.protection_level || "-"}</Descriptions.Item>
                    <Descriptions.Item label="切换状态">{dataguard.switchover_status || "-"}</Descriptions.Item>
                    <Descriptions.Item label="Guard 状态">{dataguard.guard_status || "-"}</Descriptions.Item>
                  </Descriptions>
                  {dataguard.destinations?.length > 0 && (
                    <Table
                      title={() => "归档传输目标"}
                      size="small"
                      rowKey={(row) => `${row.dest_id}-${row.db_unique_name || row.destination || row.status}`}
                      pagination={false}
                      dataSource={dataguard.destinations}
                      columns={[
                        { title: "DEST", dataIndex: "dest_id", width: 70 },
                        { title: "状态", dataIndex: "status", width: 100, render: (v) => <Tag color={v === "VALID" ? "green" : "orange"}>{v || "-"}</Tag> },
                        { title: "类型", dataIndex: "dest_type", width: 100 },
                        { title: "DB_UNIQUE_NAME", dataIndex: "db_unique_name", width: 150 },
                        { title: "恢复模式", dataIndex: "recovery_mode", width: 140 },
                        { title: "GAP", dataIndex: "gap_status", width: 120 },
                        { title: "错误", dataIndex: "error", ellipsis: true, render: (v) => v || "-" },
                      ]}
                    />
                  )}
                  {dataguard.managed_standby?.length > 0 && (
                    <Table
                      title={() => "DG 进程"}
                      size="small"
                      rowKey={(row) => `${row.process}-${row.thread_no}-${row.sequence_no}-${row.client_process}`}
                      pagination={false}
                      dataSource={dataguard.managed_standby}
                      columns={[
                        { title: "进程", dataIndex: "process", width: 90 },
                        { title: "状态", dataIndex: "status", width: 150, render: (v) => <Tag color={["APPLYING_LOG", "WAIT_FOR_LOG", "WAIT_FOR_GAP", "IDLE"].includes(v) ? "green" : "orange"}>{v || "-"}</Tag> },
                        { title: "线程", dataIndex: "thread_no", width: 80 },
                        { title: "序列", dataIndex: "sequence_no", width: 100 },
                        { title: "客户端进程", dataIndex: "client_process" },
                      ]}
                    />
                  )}
                  {dataguard.stats?.length > 0 && (
                    <Table
                      title={() => "Data Guard 延迟"}
                      size="small"
                      rowKey="name"
                      pagination={false}
                      dataSource={dataguard.stats}
                      columns={[
                        { title: "指标", dataIndex: "name", width: 180 },
                        { title: "值", dataIndex: "value" },
                        { title: "单位", dataIndex: "unit", width: 120 },
                      ]}
                    />
                  )}
                </Space>
              </Card>
            )}
            {dataguard && !dataguard.configured && dataguard.inspectable !== false && (
              <Alert type="info" showIcon message={dataguard.message || "未配置 DG"} />
            )}
            {dataguard && !dataguard.configured && dataguard.inspectable === false && (
              <Alert type="warning" showIcon message="Data Guard 状态未确认" description={dataguard.message || "当前账号无法完整检查 Data Guard 视图"} />
            )}
            {dataguard?.configured && !dataguard.running && (
              <Alert
                type="warning"
                showIcon
                message="Data Guard 未正常运行，可生成引导启动 DG 脚本"
                description="引导顺序：SSH 登录服务器 → 启动监听 → 启动数据库 → 启动归档传输或备库 MRP 归档应用 → 验证归档应用状态。"
              />
            )}
            {bootPrereq.summary.need_startup && (
              <Alert type="info" showIcon message="数据库未处于 OPEN 状态，可生成引导启动脚本"
                description={`当前类型: ${bootPrereq.summary.cluster_mode}，${bootPrereq.summary.is_rac ? "RAC 模式将使用 crsctl start crs" : "单机模式: SSH登录 -> 切换oracle用户 -> 启动监听 -> 启动数据库"}`} />
            )}
            {!bootPrereq.summary.need_startup && (
              <Alert type="success" showIcon message="数据库已处于 OPEN 状态，无需引导启动" />
            )}
            {bootResult && (
              <Card title={`引导步骤（${bootResult.cluster_mode}模式）`} size="small">
                <Table dataSource={bootResult.boot_steps} rowKey="step" size="small" pagination={false}
                  columns={[
                    { title: "步骤", dataIndex: "step", width: 60, render: (v) => <Badge count={v} style={{ backgroundColor: "#1890ff" }} /> },
                    { title: "操作", dataIndex: "action", width: 180 },
                    { title: "说明", dataIndex: "desc" },
                  ]} />
              </Card>
            )}
            <Divider style={{ margin: "4px 0" }} />
            <Row gutter={[16, 16]}>
              <Col xs={24} md={12}>
                <Card title="启动数据库" size="small" extra={<PlayCircleOutlined style={{ color: "#52c41a", fontSize: 16 }} />}>
                  <Space direction="vertical" className="full-width">
                    <Space wrap>
                      {bootPrereq.summary.need_startup && <>
                        <Button type="primary" loading={bootLoading} onClick={() => generateBootScript("open")}>引导启动 (OPEN)</Button>
                        <Button loading={bootLoading} onClick={() => generateBootScript("nomount")}>NOMOUNT</Button>
                        <Button loading={bootLoading} onClick={() => generateBootScript("mount")}>MOUNT</Button>
                      </>}
                      {bootPrereq.summary.dataguard?.configured && !bootPrereq.summary.dataguard?.running && (
                        <Button danger type="primary" loading={bootLoading} onClick={() => generateBootScript("dg")}>引导启动 DG</Button>
                      )}
                      <Button type="primary" style={{ background: "#52c41a" }} onClick={() => generateStartup("open")}>SQL启动 OPEN</Button>
                      <Button onClick={() => generateStartup("nomount")}>NOMOUNT</Button>
                      <Button onClick={() => generateStartup("mount")}>MOUNT</Button>
                    </Space>
                  </Space>
                </Card>
              </Col>
              <Col xs={24} md={12}>
                <Card title="关闭数据库" size="small" extra={<PoweroffOutlined style={{ color: "#ff4d4f", fontSize: 16 }} />}>
                  <Space direction="vertical" className="full-width">
                    <Space wrap>
                      <Button onClick={() => generateShutdown("normal")}>NORMAL</Button>
                      <Button onClick={() => generateShutdown("transactional")}>TRANSACTIONAL</Button>
                      <Button danger onClick={() => generateShutdown("immediate")}>IMMEDIATE</Button>
                      <Button danger type="primary" onClick={() => generateShutdown("abort")}>ABORT</Button>
                    </Space>
                  </Space>
                </Card>
              </Col>
            </Row>
          </Space>
        ) : (
          bootLoading ? (
            <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "16px 4px" }}>
              <Spin />
              <Text type="secondary">正在自动检查服务器、数据库、Data Guard 和 SSH 状态...</Text>
            </div>
          ) : (
            <Alert type="info" showIcon message="请选择 Oracle 实例，系统会自动显示引导顺序和 Data Guard 状态" />
          )
        )}
      </Card>

      <PlanOnlyScriptModal
        title={scriptModal.title}
        open={scriptModal.open}
        script={scriptModal.content}
        warnings={scriptModal.warnings}
        width={820}
        onClose={() => setScriptModal({ open: false, title: "", content: "", warnings: [] })}
      />
    </Space>
  );
}
