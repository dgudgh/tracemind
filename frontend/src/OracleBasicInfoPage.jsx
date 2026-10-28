import React, { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  message,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { instanceApi, oracleOpsApi } from "./apiClients";
import { useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";
import { stableRecordKey } from "./modules/stableKeys";

export function BasicInfoPage({ instances, selectedInstanceId }) {
  const oracleInstances = useMemo(() => instances.filter((item) => item.db_type === "oracle"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const [loading, setLoading] = useState(false);
  const [info, setInfo] = useState(null);

  const loadInfo = async () => {
    if (!instanceId) {
      message.warning("请先选择 Oracle 实例");
      return;
    }
    setLoading(true);
    try {
      const data = await oracleOpsApi.basicInfo(instanceId);
      setInfo(data);
    } catch (error) {
      message.error(error.message);
    } finally {
      setLoading(false);
    }
  };

  const db = info?.database || {};
  const inst = info?.instance || {};
  const nlsMap = {};
  (info?.nls || []).forEach((row) => {
    nlsMap[row.parameter] = row.value;
  });
  const latestPatch = info?.patches?.[0];
  const patchInfo =
    inst.patch_info ||
    info?.patch_info_manual ||
    (latestPatch
      ? `最新补丁 ${latestPatch.patch_id || latestPatch.patch_uid || "未知补丁"} | ${latestPatch.status || "未知状态"} | ${latestPatch.action_time || "未知时间"}\n${latestPatch.description || ""}`.trim()
      : "未查询到补丁记录");
  const os = info?.os || {};
  const osStatsMap = {};
  (os.database_os_stats || []).forEach((row) => {
    osStatsMap[row.stat_name] = row.value;
  });
  const sshSystem = os.ssh_system_info || {};
  const sshData = sshSystem.data || {};
  const formatBytes = (value) => {
    const num = Number(value);
    if (!Number.isFinite(num) || num <= 0) return value || "-";
    const gb = num / 1024 / 1024 / 1024;
    return `${gb.toFixed(2)} GB`;
  };

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          
          <Button type="primary" icon={<ReloadOutlined />} loading={loading} disabled={!instanceId} onClick={loadInfo}>
            获取基础信息
          </Button>
        </Space>
      </Card>

      {info && (
        <>
          <Card title="操作系统信息">
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="数据库主机名">{os.database_host_name || inst.host_name}</Descriptions.Item>
              <Descriptions.Item label="连接地址">{os.db_connect_host}</Descriptions.Item>
              <Descriptions.Item label="数据库平台">{os.database_platform || db.platform_name}</Descriptions.Item>
              <Descriptions.Item label="SSH">
                {os.ssh_configured ? (
                  <Tag color={sshSystem.ok ? "green" : "orange"}>{`${os.ssh_username || "-"}@${os.ssh_host}:${os.ssh_port || 22}`}</Tag>
                ) : (
                  <Tag>未配置</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="OS 名称">{sshData.os || "-"}</Descriptions.Item>
              <Descriptions.Item label="内核版本">{sshData.kernel || "-"}</Descriptions.Item>
              <Descriptions.Item label="主机名">{sshData.hostname || os.database_host_name || "-"}</Descriptions.Item>
              <Descriptions.Item label="运行时间">{sshData.uptime || "-"}</Descriptions.Item>
              <Descriptions.Item label="CPU 型号" span={2}>{sshData.cpu_info || "-"}</Descriptions.Item>
              <Descriptions.Item label="内存">{sshData.memory || formatBytes(osStatsMap.PHYSICAL_MEMORY_BYTES)}</Descriptions.Item>
              <Descriptions.Item label="CPU 数">{osStatsMap.NUM_CPUS || "-"} / 核心 {osStatsMap.NUM_CPU_CORES || "-"}</Descriptions.Item>
              <Descriptions.Item label="系统负载">{osStatsMap.LOAD || "-"}</Descriptions.Item>
              <Descriptions.Item label="SSH 当前用户">{sshData.current_user || "-"}</Descriptions.Item>
              <Descriptions.Item label="磁盘使用" span={2}>
                <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>{sshData.disk_usage || "未配置 SSH 时无法获取磁盘明细"}</pre>
              </Descriptions.Item>
              <Descriptions.Item label="网络地址" span={2}>
                <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>{sshData.network || "-"}</pre>
              </Descriptions.Item>
            </Descriptions>
          </Card>

          <Card title="数据库信息">
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="数据库名">{db.name}</Descriptions.Item>
              <Descriptions.Item label="DBID">{db.dbid}</Descriptions.Item>
              <Descriptions.Item label="创建时间">{db.created}</Descriptions.Item>
              <Descriptions.Item label="日志模式">
                <Tag color={db.log_mode === "ARCHIVELOG" ? "green" : "orange"}>{db.log_mode}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="打开模式">{db.open_mode}</Descriptions.Item>
              <Descriptions.Item label="数据库角色">{db.database_role}</Descriptions.Item>
              <Descriptions.Item label="平台">{db.platform_name}</Descriptions.Item>
              <Descriptions.Item label="强制日志">
                <Tag color={db.force_logging === "YES" ? "green" : "default"}>{db.force_logging}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="数据库启动时间" span={2}>{db.startup_time || inst.startup_time}</Descriptions.Item>
            </Descriptions>
          </Card>

          <Card title="实例信息">
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="实例名">{inst.instance_name}</Descriptions.Item>
              <Descriptions.Item label="主机名">{inst.host_name}</Descriptions.Item>
              <Descriptions.Item label="版本">{inst.version}</Descriptions.Item>
              <Descriptions.Item label="完整版本" span={2}>{info.version_full}</Descriptions.Item>
              <Descriptions.Item label="实例状态">
                <Tag color={inst.status === "OPEN" ? "green" : "blue"}>{inst.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="数据库状态">{inst.database_status}</Descriptions.Item>
              <Descriptions.Item label="启动时间">{inst.startup_time}</Descriptions.Item>
              <Descriptions.Item label="归档进程">{inst.archiver}</Descriptions.Item>
              <Descriptions.Item label="实例角色">{inst.instance_role}</Descriptions.Item>
              <Descriptions.Item label="补丁信息" span={2}>
                <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>{patchInfo}</pre>
              </Descriptions.Item>
            </Descriptions>
          </Card>

          <Card title="字符集 / NLS">
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="NLS_LANGUAGE">{nlsMap["NLS_LANGUAGE"]}</Descriptions.Item>
              <Descriptions.Item label="NLS_TERRITORY">{nlsMap["NLS_TERRITORY"]}</Descriptions.Item>
              <Descriptions.Item label="NLS_CHARACTERSET">{nlsMap["NLS_CHARACTERSET"]}</Descriptions.Item>
              <Descriptions.Item label="NLS_NCHAR_CHARACTERSET">{nlsMap["NLS_NCHAR_CHARACTERSET"]}</Descriptions.Item>
            </Descriptions>
          </Card>

          {info.patches && info.patches.length > 0 && (
            <Card title={`补丁信息（${info.patches.length} 个）`}>
              <Table size="small" rowKey={(row) => stableRecordKey("oracle-patch", row, ["patch_id", "description", "status", "action_time"])} dataSource={info.patches} pagination={false}
                columns={[
                  { title: "Patch ID", dataIndex: "patch_id", width: 120 },
                  { title: "描述", dataIndex: "description", ellipsis: true },
                  { title: "状态", dataIndex: "status", width: 100, render: v => <Tag color={v === "SUCCESS" ? "green" : "orange"}>{v}</Tag> },
                  { title: "应用时间", dataIndex: "action_time", width: 170 },
                ]}
              />
            </Card>
          )}

          {info.patch_info_manual && (
            <Card title="手动维护的补丁信息">
              <pre style={{ whiteSpace: "pre-wrap", background: "#f5f5f5", padding: 12, borderRadius: 6, margin: 0 }}>{info.patch_info_manual}</pre>
            </Card>
          )}

          <HostOverviewPanel instanceId={instanceId} />
        </>
      )}
    </Space>
  );
}

function HostOverviewPanel({ instanceId }) {
  const [hostData, setHostData] = useState(null);
  const [hostLoading, setHostLoading] = useState(false);

  const loadHost = async () => {
    if (!instanceId) return;
    setHostLoading(true);
    try {
      const data = await instanceApi.hostOverview(instanceId);
      setHostData(data);
    } catch (e) { message.error(e.message); }
    setHostLoading(false);
  };

  useEffect(() => { if (instanceId) loadHost(); }, [instanceId]);

  if (!hostData) return (
    <Card title="主机运行信息" extra={<Button size="small" icon={<ReloadOutlined />} loading={hostLoading} onClick={loadHost}>刷新</Button>}>
      <Alert type="info" message="点击「刷新」获取主机运行信息" />
    </Card>
  );

  const h = hostData.host_basic || {};
  const r = hostData.resources || {};
  const riskColor = (l) => l === "critical" ? "red" : l === "warning" ? "orange" : "green";

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {hostData.error && <Alert type="warning" showIcon message={hostData.error} />}
      {hostData.risks?.length > 0 && hostData.risks.map((risk) => (
        <Alert key={stableRecordKey("oracle-host-risk", risk, ["level", "item", "message"])} type={risk.level === "critical" ? "error" : "warning"} showIcon message={risk.message} />
      ))}

      <Card title="主机基础信息" extra={<Button size="small" icon={<ReloadOutlined />} loading={hostLoading} onClick={loadHost}>刷新主机信息</Button>}>
        <Descriptions column={2} bordered size="small">
          <Descriptions.Item label="主机名">{h.hostname || "-"}</Descriptions.Item>
          <Descriptions.Item label="IP 地址">{h.ip_address || "-"}</Descriptions.Item>
          <Descriptions.Item label="操作系统">{h.os_name || "-"} {h.os_version || ""}</Descriptions.Item>
          <Descriptions.Item label="内核版本">{h.kernel_version || "-"}</Descriptions.Item>
          <Descriptions.Item label="CPU 型号" span={2}>{h.cpu_model || "-"}</Descriptions.Item>
          <Descriptions.Item label="CPU 核数">{h.cpu_count || "-"}</Descriptions.Item>
          <Descriptions.Item label="内存总量">{h.memory_total_mb ? `${h.memory_total_mb} MB` : "-"}</Descriptions.Item>
          <Descriptions.Item label="运行时间">{h.uptime || "-"}</Descriptions.Item>
          <Descriptions.Item label="采集时间">{hostData.collect_time || "-"}</Descriptions.Item>
        </Descriptions>
      </Card>

      <Card title="CPU / 内存 / 负载">
        <Row gutter={16}>
          <Col span={6}><Statistic title="CPU 使用率" value={r.cpu_used_pct || 0} suffix="%" valueStyle={{ color: (r.cpu_used_pct || 0) > 80 ? "red" : (r.cpu_used_pct || 0) > 60 ? "orange" : "green" }} /></Col>
          <Col span={6}><Statistic title="内存使用率" value={r.memory_used_pct || 0} suffix="%" valueStyle={{ color: (r.memory_used_pct || 0) > 80 ? "red" : (r.memory_used_pct || 0) > 60 ? "orange" : "green" }} /></Col>
          <Col span={6}><Statistic title="Swap 使用率" value={r.swap_used_pct || 0} suffix="%" valueStyle={{ color: (r.swap_used_pct || 0) > 30 ? "red" : "green" }} /></Col>
          <Col span={6}><Statistic title="Load 1m/5m/15m" value={`${r.load_1m || 0} / ${r.load_5m || 0} / ${r.load_15m || 0}`} /></Col>
        </Row>
      </Card>

      <Card title="文件系统空间">
        <Table dataSource={hostData.filesystems || []} rowKey="mount_point" size="small" pagination={false} columns={[
          { title: "文件系统", dataIndex: "filesystem", width: 200, ellipsis: true },
          { title: "挂载点", dataIndex: "mount_point", width: 120 },
          { title: "总大小", dataIndex: "total", width: 100 },
          { title: "已用", dataIndex: "used", width: 100 },
          { title: "可用", dataIndex: "available", width: 100 },
          { title: "使用率", dataIndex: "used_pct", width: 100, render: v => <Tag color={v > 90 ? "red" : v > 80 ? "orange" : "green"}>{v}%</Tag> },
          { title: "状态", dataIndex: "level", width: 80, render: v => <Tag color={riskColor(v)}>{v === "critical" ? "危险" : v === "warning" ? "警告" : "正常"}</Tag> },
        ]} />
      </Card>

      <Card title="数据库进程状态">
        <Table dataSource={hostData.processes || []} rowKey="name" size="small" pagination={false} columns={[
          { title: "进程名", dataIndex: "name", width: 180 },
          { title: "状态", dataIndex: "status", width: 100, render: v => <Tag color={v === "running" ? "green" : "red"}>{v === "running" ? "运行中" : "未检测到"}</Tag> },
          { title: "说明", dataIndex: "detail", ellipsis: true },
        ]} />
      </Card>

      <Card title="端口监听状态">
        <Table dataSource={hostData.ports || []} rowKey="port" size="small" pagination={false} columns={[
          { title: "端口", dataIndex: "port", width: 80 },
          { title: "服务", dataIndex: "service", width: 180 },
          { title: "状态", dataIndex: "listening", width: 100, render: v => <Tag color={v ? "green" : "red"}>{v ? "监听中" : "未监听"}</Tag> },
        ]} />
      </Card>
    </Space>
  );
}
