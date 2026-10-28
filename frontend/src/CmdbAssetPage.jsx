import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Col,
  Descriptions,
  Empty,
  Form,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  message,
} from "antd";
import {
  ApiOutlined,
  CheckCircleOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  FileSearchOutlined,
  HistoryOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import {
  connectionTestIsCurrent,
  discoveryError,
  effectiveHealthStatus,
  failedConnectionAfterDiscovery,
  successfulConnectionAfterDiscovery,
} from "./AssetConnectionState";
import { formatClusterName, formatClusterOption } from "./ClusterDisplay";
import { buildAssetTopology, TOPOLOGY_NODE_WIDTH, TOPOLOGY_NODE_HEIGHT } from "./AssetTopology";
import { formatDatabaseVersion } from "./DatabaseVersion";
import { InstanceDrawer } from "./InstanceDrawer";
import {
  formatReplicationLagSeconds,
  hasReplicationLagData,
  replicationLagMetric,
  replicationLagSeconds,
  replicationLagTone,
} from "./ReplicationLag";
import { instanceApi } from "./apiClients";
import {
  applyAssetDiscovery,
  createAssetCorrection,
  createCmdbSource,
  getAssetCluster,
  generateClusterHAPlan,
  getClusterHADiagnostics,
  getAssetHost,
  getNormalizedAssetInstance,
  getAssetOverview,
  getAssetTree,
  listAssetClusters,
  listAssetCorrections,
  listAssetDiscoveryBatches,
  listAssetHosts,
  listAssetInstances,
  listClusterHADiagnosticHistory,
  listCmdbSources,
  previewAssetDiscovery,
  refreshClusterHADiagnostics,
  syncCmdbAssets,
} from "./modules/cmdbApi";

const ARCHITECTURE_LABELS = {
  single: "单机",
  standalone: "单机",
  primary_replica: "主从复制",
  rac: "Oracle RAC",
  adg: "Oracle Data Guard",
  data_guard: "Oracle Data Guard",
  primary_standby: "主备",
  replication: "主从复制",
  mgr: "MySQL MGR",
  always_on: "SQL Server Always On",
  alwayson: "SQL Server Always On",
  streaming_replication: "PostgreSQL 流复制",
  sharding: "分片集群",
  cluster: "数据库集群",
};

const ROLE_LABELS = {
  primary: "主节点",
  standby: "备节点",
  replica: "副本",
  read_only: "只读副本",
  readonly: "只读副本",
  candidate: "候选节点",
  arbiter: "仲裁节点",
  data_node: "数据节点",
  control_node: "控制节点",
  coordinator: "协调节点",
  rac_node: "RAC 节点",
  adg_primary: "ADG 主库",
  adg_standby: "ADG 备库",
  alwayson_primary: "Always On 主副本",
  alwayson_secondary: "Always On 辅助副本",
  dr: "灾备节点",
  report: "报表节点",
  unknown: "待确认",
};

const HEALTH_LABELS = {
  healthy: "健康",
  warning: "警告",
  critical: "严重",
  unhealthy: "异常",
  disabled: "停用",
  unknown: "未知",
  pending_confirmation: "待确认",
};

const ASSET_FACT_LABELS = {
  architecture_evidence: "识别证据",
  architecture_type: "架构类型",
  cluster_name: "集群别名",
  database_name: "数据库名",
  database_types: "数据库类型",
  db_type: "数据库类型",
  db_version: "数据库版本",
  discovery_method: "识别方式",
  enabled: "是否启用",
  environment: "环境",
  factory_name: "工厂",
  health_status: "健康状态",
  host: "主机地址",
  line_name: "产线",
  lag_datum_time: "延迟数据时间",
  lag_time_computed: "延迟计算时间",
  last_verified_at: "最后验证时间",
  native_instance_id: "实例 ID",
  native_type: "原生类型",
  name_customized: "名称已自定义",
  owner: "负责人",
  port: "服务端口",
  replication_delay_seconds: "复制延迟",
  role_type: "节点角色",
  service_name: "服务名",
  source_kind: "来源方式",
  ssh_configured: "SSH 已配置",
  ssh_port: "SSH 端口",
  system_name: "业务系统",
  topology_group: "拓扑分组",
  transport_lag: "传输延迟原值",
  transport_lag_seconds: "传输延迟",
  apply_lag: "应用延迟原值",
  apply_lag_seconds: "应用延迟",
  apply_finish_time: "预计应用完成时间",
  vip: "VIP",
};


const formatTime = (value) => {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date).replaceAll("/", "-");
};

const healthTone = (status) => {
  if (status === "healthy") return "is-healthy";
  if (["warning", "pending_confirmation"].includes(status)) return "is-warning";
  if (["critical", "unhealthy"].includes(status)) return "is-critical";
  return "is-neutral";
};

function HealthState({ status = "unknown" }) {
  return (
    <span className={`asset-health-state ${healthTone(status)}`}>
      <span className="asset-health-dot" />
      {HEALTH_LABELS[status] || status}
    </span>
  );
}


function Metric({ label, value, tone = "default" }) {
  return (
    <div className={`asset-summary-metric tone-${tone}`}>
      <span>{label}</span>
      <strong>{value ?? 0}</strong>
    </div>
  );
}

const DB_TYPE_COLORS = {
  Oracle: "red",
  MySQL: "gold",
  PostgreSQL: "blue",
  SQLServer: "cyan",
  DomesticDB: "purple",
};

const PRIMARY_ROLES = new Set(["primary", "adg_primary", "alwayson_primary", "control_node", "coordinator"]);
const LAG_DETAIL_KEYS = new Set([
  "replication_delay_seconds",
  "transport_lag_seconds",
  "apply_lag_seconds",
  "transport_lag",
  "apply_lag",
  "apply_finish_time",
  "lag_time_computed",
  "lag_datum_time",
  "last_verified_at",
]);

const connectionRecoveryHint = (item) => {
  if (!successfulConnectionAfterDiscovery(item)) return "";
  return `${instanceDisplayName(item)} 已于 ${formatTime(item.attributes.last_connection_test_at)} 连接测试通过。本次测试只验证当前连接，不会改写拓扑；请点击“自动识别”刷新架构和节点角色。`;
};

const discoveryErrorHint = (item) => {
  const error = discoveryError(item);
  if (!error) return "";
  const prefix = failedConnectionAfterDiscovery(item)
    ? `最近一次手工连接测试（${formatTime(item.attributes.last_connection_test_at)}）失败`
    : "最近一次自动识别连接验证失败";
  if (/password authentication failed/i.test(error)) {
    const username = error.match(/user\s+"([^"]+)"/i)?.[1];
    return `${prefix}：数据库账号${username ? ` ${username}` : ""}密码验证失败。已保留上次识别的架构；请在“编辑连接”中更新密码，点击“测试”确认后再执行“自动识别”。`;
  }
  if (/pg_hba\.conf/i.test(error)) {
    const hbaTarget = error.match(/host\s+"([^"]+)".*user\s+"([^"]+)".*database\s+"([^"]+)"/i);
    if (hbaTarget) {
      return `${prefix}：PostgreSQL 拒绝采集连接。请在实际生效的 pg_hba.conf 中放行来源 ${hbaTarget[1]}、用户 ${hbaTarget[2]}、数据库 ${hbaTarget[3]}，reload 后重新自动识别。`;
    }
    return `${prefix}：PostgreSQL 拒绝采集机连接。请在 pg_hba.conf 放行采集来源，并为采集账号授予 pg_monitor 后重新自动识别。`;
  }
  return `${prefix}：${error}`;
};

const architectureDisplay = (item) => ARCHITECTURE_LABELS[item?.architecture] || item?.architecture || "待识别";

const hasKnownArchitecture = (item) => Boolean(item?.architecture && item.architecture !== "unknown");
const hasKnownRole = (item) => Boolean(item?.role && item.role !== "unknown");
const isKnownPrimary = (item) => PRIMARY_ROLES.has(item?.role);

const formatMemory = (value) => {
  if (!value) return "待采集";
  return value >= 1024 ? `${(value / 1024).toFixed(value % 1024 ? 1 : 0)} GB` : `${value} MB`;
};

const locationText = (row) => [row.region, row.site, row.zone].filter(Boolean).join(" / ") || "未分配";

const instanceDisplayName = (row = {}) => {
  const isAutomaticPeer = row.discovery_source === "automatic"
    && row.attributes?.native_type === "discovered_database_instance"
    && !row.name_customized;
  return isAutomaticPeer ? row.host_name || row.host_ip || row.name || "未命名实例" : row.name || row.host_name || row.host_ip || "未命名实例";
};

function DbTypeTag({ value }) {
  return <Tag color={DB_TYPE_COLORS[value]}>{value || "Unknown"}</Tag>;
}

function HostedInstanceSummary({ count, dbTypes = [] }) {
  return (
    <div className="asset-host-instance-summary">
      <strong>{count ?? 0}</strong>
      <div className="asset-host-instance-types">
        {dbTypes.map((item) => <DbTypeTag key={item} value={item} />)}
      </div>
    </div>
  );
}

function DatabaseVersion({ value, dbType, emptyText = "待采集" }) {
  const formatted = formatDatabaseVersion(value, dbType);
  if (!formatted) return emptyText;
  const raw = String(value).trim();
  return formatted === raw ? formatted : <Tooltip title={raw}>{formatted}</Tooltip>;
}

function ClusterName({ value, emptyText = "未命名集群" }) {
  const formatted = formatClusterName(value);
  if (!formatted) return emptyText;
  const raw = String(value).trim();
  return formatted === raw ? formatted : <Tooltip title={raw}>{formatted}</Tooltip>;
}

const clusterOptionLabel = (cluster) => formatClusterOption(
  cluster,
  architectureDisplay(cluster),
);

function DiscoveredArchitecture({ item }) {
  const error = discoveryError(item);
  const label = architectureDisplay(item);
  if (successfulConnectionAfterDiscovery(item)) {
    return <Tooltip title={connectionRecoveryHint(item)}><Tag color="blue">{label}</Tag></Tooltip>;
  }
  return error ? <Tooltip title={discoveryErrorHint(item)}><Tag color="orange">{label}</Tag></Tooltip> : label;
}

function DiscoveredRole({ item }) {
  const error = discoveryError(item);
  const label = ROLE_LABELS[item?.role] || item?.role || "待识别";
  if (successfulConnectionAfterDiscovery(item)) {
    return <Tooltip title={connectionRecoveryHint(item)}><Tag color="blue">{label}</Tag></Tooltip>;
  }
  return error ? <Tooltip title={discoveryErrorHint(item)}><Tag color="orange">{label}</Tag></Tooltip> : label;
}

function ConnectionValidationSummary({ item }) {
  const attributes = item?.attributes || {};
  if (!attributes.connection_test_status || !attributes.last_connection_test_at) return null;
  const current = connectionTestIsCurrent(item);
  const testedAt = formatTime(attributes.last_connection_test_at);
  if (!current) {
    return <Alert type="info" showIcon message={`历史连接测试：${testedAt}`} description="此后已执行自动识别，当前拓扑状态以自动识别结果为准。" />;
  }
  if (attributes.connection_test_status === "failed") {
    return <Alert type="error" showIcon message={`当前连接测试失败：${testedAt}`} description={discoveryErrorHint(item)} />;
  }
  return (
    <Alert
      type="success"
      showIcon
      message={`当前连接测试通过：${testedAt}`}
      description={successfulConnectionAfterDiscovery(item)
        ? "连接已经恢复；页面继续展示上次成功识别的拓扑，点击“自动识别”后更新架构和节点角色。"
        : "当前连接可用。架构和节点角色仍以最近一次自动识别结果为准。"}
    />
  );
}

function ReplicationLagValue({ item, compact = false }) {
  const seconds = replicationLagSeconds(item);
  if (seconds === null) return compact ? "-" : "未采集";
  const transport = formatReplicationLagSeconds(
    replicationLagMetric(item, "transport_lag_seconds"),
  );
  const apply = formatReplicationLagSeconds(
    replicationLagMetric(item, "apply_lag_seconds"),
  );
  const tooltip = `传输延迟 ${transport}；应用延迟 ${apply}`;
  const value = formatReplicationLagSeconds(seconds);
  return compact
    ? <Tooltip title={tooltip}><span>{value}</span></Tooltip>
    : <Tooltip title={tooltip}><Tag color={replicationLagTone(item)}>{value}</Tag></Tooltip>;
}

function ReplicationLagDetails({ item }) {
  const isDataGuard = ["adg", "data_guard"].includes(item?.architecture);
  if (!hasReplicationLagData(item)) {
    if (!isDataGuard || !PRIMARY_ROLES.has(item?.role)) return null;
    return (
      <Alert
        type="info"
        showIcon
        message="Data Guard 延迟由备库节点计算"
        description="主库的 v$dataguard_stats 通常不返回延迟，请打开对应 ADG 备库实例查看传输延迟和应用延迟。"
      />
    );
  }
  return (
    <Descriptions title="复制链路" column={{ xs: 1, sm: 2 }} size="small" bordered>
      <Descriptions.Item label="综合延迟"><ReplicationLagValue item={item} /></Descriptions.Item>
      <Descriptions.Item label="传输延迟">
        {formatReplicationLagSeconds(replicationLagMetric(item, "transport_lag_seconds"))}
      </Descriptions.Item>
      <Descriptions.Item label="应用延迟">
        {formatReplicationLagSeconds(replicationLagMetric(item, "apply_lag_seconds"))}
      </Descriptions.Item>
      <Descriptions.Item label="预计应用完成">
        {replicationLagMetric(item, "apply_finish_time") || "-"}
      </Descriptions.Item>
      <Descriptions.Item label="延迟计算时间">
        {replicationLagMetric(item, "lag_time_computed") || item.last_verified_at || "-"}
      </Descriptions.Item>
      <Descriptions.Item label="数据时间">
        {replicationLagMetric(item, "lag_datum_time") || "-"}
      </Descriptions.Item>
    </Descriptions>
  );
}

function FiveLayerOverview({ overview, tree }) {
  const databaseMetrics = ["Oracle", "MySQL", "PostgreSQL", "SQLServer", "DomesticDB"];
  const hierarchyRows = [];
  (tree.regions || []).forEach((region) => region.sites?.forEach((site) => site.zones?.forEach((zone) => zone.environments?.forEach((environment) => {
    const hosts = environment.hosts || [];
    const clusterIds = new Set();
    let instanceCount = 0;
    hosts.forEach((host) => host.clusters?.forEach((cluster) => {
      clusterIds.add(cluster.id);
      instanceCount += cluster.instances?.length || 0;
    }));
    hierarchyRows.push({
      key: `${region.id}-${site.id}-${zone.id}-${environment.id}`,
      region: region.name,
      site: site.name,
      zone: zone.name,
      environment: environment.name,
      hostCount: hosts.length,
      clusterCount: clusterIds.size,
      instanceCount,
    });
  }))));

  return (
    <div className="asset-center-stack">
      <section className="asset-five-summary">
        <div className="asset-five-summary-lead">
          <span className="asset-summary-icon is-healthy"><SafetyCertificateOutlined /></span>
          <div><span>五层资产模型</span><strong>区域、环境、主机、集群、实例已分层</strong><small>自动发现为基础事实，人工校正优先保留</small></div>
        </div>
        <div className="asset-five-primary-metrics">
          <Metric label="主机" value={overview.host_count} />
          <Metric label="集群" value={overview.cluster_count} />
          <Metric label="实例" value={overview.instance_count} />
        </div>
      </section>
      <section className="asset-surface">
        <div className="asset-section-heading"><div><strong>数据库类型</strong><span>按实例统计</span></div></div>
        <div className="asset-five-db-metrics">
          {databaseMetrics.map((dbType) => (
            <div key={dbType}><DbTypeTag value={dbType} /><strong>{overview.db_type_counts?.[dbType] || 0}</strong><span>个实例</span></div>
          ))}
        </div>
      </section>
      <div className="asset-five-overview-grid">
        <section className="asset-surface">
          <div className="asset-section-heading"><div><strong>区域分布</strong><span>{overview.configured_region_count ?? (overview.region_distribution?.length || 0)} 个已配置区域</span></div></div>
          <div className="asset-five-distribution">
            {(overview.region_distribution || []).map((item) => <div key={item.name}><span>{item.name}</span><strong>{item.count}</strong></div>)}
            {!overview.region_distribution?.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无区域数据" /> : null}
          </div>
        </section>
        <section className="asset-surface">
          <div className="asset-section-heading"><div><strong>环境分布</strong><span>{overview.configured_environment_count ?? (overview.environment_distribution?.length || 0)} 个已配置环境</span></div></div>
          <div className="asset-five-distribution">
            {(overview.environment_distribution || []).map((item) => <div key={item.name}><span>{item.name}</span><strong>{item.count}</strong></div>)}
            {!overview.environment_distribution?.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无环境数据" /> : null}
          </div>
        </section>
      </div>
      <section className="asset-surface">
        <div className="asset-section-heading"><div><strong>资产层级</strong><span>Region / Site / Zone → Environment → Host → Cluster → Instance</span></div></div>
        <Table
          rowKey="key"
          size="middle"
          pagination={false}
          dataSource={hierarchyRows}
          columns={[
            { title: "区域", dataIndex: "region" },
            { title: "机房", dataIndex: "site" },
            { title: "可用区", dataIndex: "zone" },
            { title: "环境", dataIndex: "environment" },
            { title: "主机", dataIndex: "hostCount", width: 90 },
            { title: "集群", dataIndex: "clusterCount", width: 90 },
            { title: "实例", dataIndex: "instanceCount", width: 90 },
          ]}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无五层资产数据" /> }}
        />
      </section>
    </div>
  );
}

function TopologyInstanceNode({ data }) {
  const { item, onOpen, onCorrect } = data;
  return (
    <button type="button" className={`asset-five-node ${isKnownPrimary(item) ? "is-primary" : ""}`} onClick={() => onOpen("instance", item)}>
      <Handle type="target" position={Position.Left} className="asset-topology-handle" />
      <span className="asset-five-node-head"><strong>{instanceDisplayName(item)}</strong><HealthState status={effectiveHealthStatus(item)} /></span>
      <span className="asset-five-node-role"><DiscoveredRole item={item} /></span>
      <span className="asset-five-node-address">{item.endpoint}</span>
      <span className="asset-five-node-meta">{item.host_name || item.host_ip || "主机待确认"} · {item.os_type || "OS 待采集"}</span>
      <span className="asset-five-node-meta">
        <DatabaseVersion value={item.version} dbType={item.db_type} emptyText="版本待采集" />
        {hasReplicationLagData(item) ? <> · 延迟 <ReplicationLagValue item={item} compact /></> : null}
      </span>
      <Tooltip title="人工校正"><EditOutlined className="asset-five-node-edit" onClick={(event) => { event.stopPropagation(); onCorrect("instance", item); }} /></Tooltip>
      <Handle type="source" position={Position.Right} className="asset-topology-handle" />
    </button>
  );
}

function TopologyClusterNode({ data }) {
  return (
    <button type="button" className="asset-five-node" onClick={() => data.onOpen("cluster", data.cluster)}>
      <Handle type="target" position={Position.Left} className="asset-topology-handle" />
      <span className="asset-five-node-head"><strong><ClusterName value={data.cluster.name} /></strong></span>
      <span className="asset-five-node-role">{data.cluster.architecture === "mgr" ? "MySQL MGR 组成员关系" : "RAC 集群成员关系"}</span>
    </button>
  );
}

const normalizedTopologyNodeTypes = { normalizedInstance: TopologyInstanceNode, normalizedCluster: TopologyClusterNode };
function TopologyAutoFit({ containerRef, topologyKey }) {
  const { fitView } = useReactFlow();

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;

    let frameId;
    const refit = () => {
      window.cancelAnimationFrame(frameId);
      frameId = window.requestAnimationFrame(() => fitView({ padding: 0.08, maxZoom: 1 }));
    };
    const observer = new window.ResizeObserver(refit);
    observer.observe(container);
    refit();

    return () => {
      window.cancelAnimationFrame(frameId);
      observer.disconnect();
    };
  }, [containerRef, fitView, topologyKey]);

  return null;
}

const HA_SEVERITY_LABELS = { critical: "严重", warning: "警告", info: "提示" };
const HA_SEVERITY_COLORS = { critical: "red", warning: "orange", info: "blue" };

function HADiagnosticPanel({ diagnostic, loading, error, onCapture, onDetails, onPlan }) {
  const summary = diagnostic?.summary;
  const readiness = diagnostic?.action_readiness;
  const blockingCount = (readiness?.blockers || []).length;
  const statusTone = summary?.health_status === "critical"
    ? "is-critical"
    : summary?.health_status === "warning"
      ? "is-warning"
      : "is-healthy";
  return (
    <section className="asset-ha-diagnostic-panel">
      <div className="asset-ha-diagnostic-head">
        <div>
          <span className={`asset-summary-icon ${statusTone}`}><SafetyCertificateOutlined /></span>
          <div><strong>高可用只读诊断</strong><span>角色、复制延迟、日志缺口、脑裂与法定人数</span></div>
        </div>
        <Space wrap>
          <Button icon={<HistoryOutlined />} loading={loading} disabled={!diagnostic} onClick={onCapture}>记录快照</Button>
          <Button icon={<EyeOutlined />} disabled={!diagnostic} onClick={onDetails}>查看证据</Button>
          <Tooltip title={readiness?.plan_endpoint ? readiness.safety_note : "当前架构没有可调用的只读检查方案"}>
            <Button icon={<FileSearchOutlined />} disabled={!readiness?.plan_endpoint} onClick={onPlan}>生成检查方案</Button>
          </Tooltip>
        </Space>
      </div>
      {error ? <Alert type="warning" showIcon message="高可用诊断加载失败" description={error} /> : null}
      {summary ? (
        <>
          <div className="asset-ha-diagnostic-metrics">
            <div><span>综合状态</span><HealthState status={summary.health_status} /></div>
            <div><span>风险分</span><strong>{summary.risk_score}</strong></div>
            <div><span>证据可信度</span><strong>{summary.confidence_score}%</strong></div>
            <div><span>实时验证</span><strong>{summary.verified_count}/{summary.member_count}</strong></div>
            <div><span>最大复制延迟</span><strong>{summary.max_lag_seconds == null ? "未采集" : formatReplicationLagSeconds(summary.max_lag_seconds)}</strong></div>
            <div><span>切换阻断项</span><strong className={blockingCount ? "is-warning" : ""}>{blockingCount}</strong></div>
          </div>
          <div className="asset-ha-findings-preview">
            {diagnostic.findings?.length ? diagnostic.findings.slice(0, 4).map((item) => (
              <span key={item.code}><Tag color={HA_SEVERITY_COLORS[item.severity]}>{HA_SEVERITY_LABELS[item.severity] || item.severity}</Tag>{item.title}</span>
            )) : <span className="is-clear"><CheckCircleOutlined /> 当前证据未发现高可用风险</span>}
            {diagnostic.findings?.length > 4 ? <Button type="link" onClick={onDetails}>另有 {diagnostic.findings.length - 4} 项</Button> : null}
          </div>
        </>
      ) : <div className="asset-ha-diagnostic-loading">{loading ? "正在计算集群健康状态..." : "暂无高可用诊断"}</div>}
    </section>
  );
}

function HADiagnosticModal({ open, diagnostic, history, onClose }) {
  const summary = diagnostic?.summary || {};
  const endpoints = diagnostic?.endpoint_facts || {};
  return (
    <Modal title="集群高可用诊断" open={open} centered width={960} footer={null} onCancel={onClose}>
      {diagnostic ? <div className="asset-ha-modal-content">
        <Alert
          type={summary.health_status === "critical" ? "error" : summary.health_status === "warning" ? "warning" : "success"}
          showIcon
          message={`${diagnostic.cluster.name} · ${architectureDisplay(diagnostic.cluster)}`}
          description={`风险分 ${summary.risk_score}，证据可信度 ${summary.confidence_score}%，实时验证 ${summary.verified_count}/${summary.member_count} 个节点。`}
        />
        <Descriptions column={{ xs: 1, sm: 2, md: 3 }} size="small" bordered>
          <Descriptions.Item label="主节点">{summary.primary_count ?? 0}</Descriptions.Item>
          <Descriptions.Item label="副本/备库">{summary.secondary_count ?? 0}</Descriptions.Item>
          <Descriptions.Item label="候选/仲裁">{summary.candidate_count ?? 0}</Descriptions.Item>
          <Descriptions.Item label="日志缺口">{summary.log_gap_count ?? 0}</Descriptions.Item>
          <Descriptions.Item label="脑裂风险">{summary.split_brain_risk ? <Tag color="red">存在</Tag> : "未发现"}</Descriptions.Item>
          <Descriptions.Item label="法定人数风险">{summary.quorum_risk ? <Tag color="red">存在</Tag> : "未发现"}</Descriptions.Item>
          <Descriptions.Item label="VIP">{endpoints.vips?.join("、") || "未采集"}</Descriptions.Item>
          <Descriptions.Item label="Listener">{endpoints.listeners?.join("、") || "未采集"}</Descriptions.Item>
          <Descriptions.Item label="Service">{endpoints.services?.join("、") || "未采集"}</Descriptions.Item>
        </Descriptions>
        <div className="asset-section-heading"><div><strong>风险与结论</strong><span>{diagnostic.findings?.length || 0} 项</span></div></div>
        <Table
          rowKey="code"
          size="small"
          pagination={false}
          dataSource={diagnostic.findings || []}
          columns={[
            { title: "级别", dataIndex: "severity", width: 90, render: (value) => <Tag color={HA_SEVERITY_COLORS[value]}>{HA_SEVERITY_LABELS[value] || value}</Tag> },
            { title: "结论", dataIndex: "title", width: 210 },
            { title: "判断依据", dataIndex: "description" },
            { title: "建议", dataIndex: "recommendation" },
          ]}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未发现高可用风险" /> }}
        />
        <div className="asset-section-heading"><div><strong>节点证据</strong><span>{diagnostic.members?.length || 0} 个节点</span></div></div>
        <Table
          rowKey="id"
          size="small"
          pagination={false}
          scroll={{ x: 820 }}
          dataSource={diagnostic.members || []}
          columns={[
            { title: "节点", dataIndex: "name", width: 150 },
            { title: "地址", dataIndex: "endpoint", width: 170 },
            { title: "角色", dataIndex: "role", width: 130, render: (value) => ROLE_LABELS[value] || value || "待确认" },
            { title: "可达", dataIndex: "reachable", width: 90, render: (value) => value ? <Tag color="green">是</Tag> : <Tag color="red">否</Tag> },
            { title: "实时验证", dataIndex: "verified", width: 100, render: (value) => value ? "已验证" : "未验证" },
            { title: "延迟", dataIndex: "lag_seconds", width: 110, render: (value) => value == null ? "未采集" : formatReplicationLagSeconds(value) },
            { title: "日志缺口", dataIndex: "log_gap_count", width: 100 },
          ]}
        />
        <div className="asset-section-heading"><div><strong>诊断历史</strong><span>自动识别和人工记录</span></div></div>
        <Table
          rowKey="id"
          size="small"
          pagination={{ pageSize: 5, showSizeChanger: false }}
          dataSource={history || []}
          columns={[
            { title: "时间", dataIndex: "collected_at", render: formatTime },
            { title: "来源", dataIndex: "source", render: (value) => value === "live_discovery" ? "自动识别" : "人工记录" },
            { title: "状态", dataIndex: "health_status", render: (value) => <HealthState status={value} /> },
            { title: "风险分", dataIndex: "risk_score" },
            { title: "可信度", dataIndex: "confidence_score", render: (value) => `${value}%` },
            { title: "验证节点", render: (_, row) => `${row.verified_count}/${row.member_count}` },
          ]}
          locale={{ emptyText: "暂无历史快照" }}
        />
      </div> : null}
    </Modal>
  );
}

function HAPlanModal({ result, onClose }) {
  if (!result) return null;
  const script = result.sql || result.script || "未返回检查 SQL/脚本";
  return (
    <Modal title={result.title || result.message || "高可用检查方案"} open centered width={900} footer={<Button onClick={onClose}>关闭</Button>} onCancel={onClose}>
      <div className="asset-ha-plan-content">
        <Alert type="info" showIcon message="只读检查方案" description="该方案用于核对切换条件，不会执行 switchover、failover 或节点变更。" />
        {(result.warnings || []).map((item, index) => <Alert key={`${index}-${item}`} type="warning" showIcon message={item} />)}
        <pre>{script}</pre>
      </div>
    </Modal>
  );
}

function FiveLayerTopology({ clusters, instances, loading, onOpen, onCorrect }) {
  const [clusterId, setClusterId] = useState();
  const [haDiagnostic, setHaDiagnostic] = useState(null);
  const [haHistory, setHaHistory] = useState([]);
  const [haLoading, setHaLoading] = useState(false);
  const [haError, setHaError] = useState("");
  const [haModalOpen, setHaModalOpen] = useState(false);
  const [haPlanResult, setHaPlanResult] = useState(null);
  const topologyMapRef = useRef(null);
  const topologyClusters = useMemo(
    () => clusters.filter((item) => item.instance_count > 0 || item.discovery_source === "manual"),
    [clusters],
  );
  useEffect(() => {
    if (!topologyClusters.length) return setClusterId(undefined);
    if (!topologyClusters.some((item) => item.id === clusterId)) setClusterId(topologyClusters[0].id);
  }, [clusterId, topologyClusters]);
  const cluster = topologyClusters.find((item) => item.id === clusterId) || topologyClusters[0];
  useEffect(() => {
    if (!cluster?.id) {
      setHaDiagnostic(null);
      setHaHistory([]);
      return undefined;
    }
    let cancelled = false;
    setHaLoading(true);
    setHaError("");
    Promise.all([
      getClusterHADiagnostics(cluster.id),
      listClusterHADiagnosticHistory(cluster.id, { limit: 30 }),
    ]).then(([diagnostic, history]) => {
      if (cancelled) return;
      setHaDiagnostic(diagnostic);
      setHaHistory(history.items || []);
    }).catch((error) => {
      if (!cancelled) setHaError(error.message || "高可用诊断加载失败");
    }).finally(() => {
      if (!cancelled) setHaLoading(false);
    });
    return () => { cancelled = true; };
  }, [cluster?.id]);

  const captureHADiagnostic = async () => {
    if (!cluster?.id) return;
    setHaLoading(true);
    try {
      const result = await refreshClusterHADiagnostics(cluster.id);
      setHaDiagnostic(result);
      const history = await listClusterHADiagnosticHistory(cluster.id, { limit: 30 });
      setHaHistory(history.items || []);
      message.success("当前高可用诊断已记录");
    } catch (error) {
      message.error(error.message || "诊断快照记录失败");
    } finally {
      setHaLoading(false);
    }
  };

  const generateHAPlan = async () => {
    const endpoint = haDiagnostic?.action_readiness?.plan_endpoint;
    if (!endpoint) return;
    setHaLoading(true);
    try {
      setHaPlanResult(await generateClusterHAPlan(endpoint));
    } catch (error) {
      Modal.error({ title: "只读检查方案生成失败", content: error.message || "请求失败" });
    } finally {
      setHaLoading(false);
    }
  };
  const members = instances.filter((item) => item.cluster_id === cluster?.id);
  const failedMembers = members.filter((item) => discoveryError(item));
  const recoveredMembers = members.filter(successfulConnectionAfterDiscovery);
  const clusterDiscoveryError = failedMembers.length
    ? discoveryError(failedMembers[0])
    : members.length
      ? ""
      : discoveryError(cluster);
  const hasStoredTopology = hasKnownArchitecture(cluster) && members.some(hasKnownRole);
  const topologyNeedsRefresh = recoveredMembers.length > 0;
  const topology = buildAssetTopology(cluster, members);
  const flowNodes = [
    ...members.map((item) => ({
      id: String(item.id),
      type: "normalizedInstance",
      data: { item, onOpen, onCorrect },
      position: topology.positions[String(item.id)],
      style: { width: TOPOLOGY_NODE_WIDTH, height: TOPOLOGY_NODE_HEIGHT },
    })),
    ...(topology.hasClusterNode ? [{
      id: topology.clusterNodeId,
      type: "normalizedCluster",
      data: { cluster, onOpen },
      position: topology.positions[topology.clusterNodeId],
      style: { width: TOPOLOGY_NODE_WIDTH, height: TOPOLOGY_NODE_HEIGHT },
    }] : []),
  ];
  const flowEdges = topology.edges.map((edge) => {
    const isMembership = edge.relation_type === "member_of";
    const relationLabel = isMembership ? "集群成员" : "复制（已存关系）";
    return {
      ...edge,
      label: clusterDiscoveryError
        ? `${relationLabel}（待验证）`
        : topologyNeedsRefresh
          ? `${relationLabel}（待刷新）`
          : relationLabel,
      type: "smoothstep",
      markerEnd: isMembership ? undefined : { type: MarkerType.ArrowClosed, color: "#8a99a3" },
      style: { stroke: "#9aa6af", strokeWidth: 1.5, strokeDasharray: isMembership ? "5 4" : undefined },
      labelStyle: { fill: "#68737c", fontSize: 12 },
      labelBgStyle: { fill: "#fbfcfc", fillOpacity: 1 },
    };
  });

  if (!loading && !topologyClusters.length) return <section className="asset-surface"><Empty description="暂无包含实例的集群，请先运行自动识别" /></section>;
  return (
    <div className="asset-center-stack">
      <section className="asset-five-topology-toolbar">
        <div><span>当前集群</span><strong>{cluster ? <ClusterName value={cluster.name} /> : "加载中"}</strong><small>{locationText(cluster || {})} · {cluster?.environment || "环境未分配"}</small></div>
        <Space wrap>
          <Select className="asset-topology-select" value={cluster?.id} onChange={setClusterId} options={topologyClusters.map((item) => ({ value: item.id, label: clusterOptionLabel(item), title: item.name }))} />
          {cluster ? <DbTypeTag value={cluster.db_type} /> : null}
          <Tag color={clusterDiscoveryError ? "orange" : "blue"}>{architectureDisplay(cluster)}</Tag>
          <Button icon={<EditOutlined />} disabled={!cluster} onClick={() => onCorrect("cluster", cluster)}>校正集群</Button>
        </Space>
      </section>
      {clusterDiscoveryError ? (
        <Alert
          type="warning"
          showIcon
          message={hasStoredTopology ? `架构已识别，${failedMembers.length || 1} 个节点当前连接验证失败` : "集群架构尚未采集"}
          description={hasStoredTopology
            ? `继续展示已保存的架构与节点角色。${failedMembers.map((item) => `${instanceDisplayName(item)}：${discoveryErrorHint(item)}`).join("；") || discoveryErrorHint(cluster)}`
            : failedMembers.map(discoveryErrorHint).find(Boolean) || discoveryErrorHint(cluster)}
        />
      ) : topologyNeedsRefresh ? (
        <Alert
          type="info"
          showIcon
          message="连接已恢复，拓扑等待刷新"
          description={recoveredMembers.map(connectionRecoveryHint).join("；")}
        />
      ) : null}
      {!loading && members.length > 1 && !topology.edges.length ? (
        <Alert type="info" showIcon message={["rac", "mgr"].includes(cluster?.architecture) ? "集群成员关系未知（未采集）" : "复制关系未知（未采集）"} />
      ) : null}
      <HADiagnosticPanel
        diagnostic={haDiagnostic}
        loading={haLoading}
        error={haError}
        onCapture={captureHADiagnostic}
        onDetails={() => setHaModalOpen(true)}
        onPlan={generateHAPlan}
      />
      <section className="asset-five-topology-surface">
        <div className="asset-topology-surface-heading"><div><strong>集群架构拓扑</strong><span>{members.length} 个实例 · {cluster?.host_count || 0} 台主机</span></div><HealthState status={haDiagnostic?.summary?.health_status || (failedMembers.length ? "warning" : recoveredMembers.length ? "pending_confirmation" : cluster?.status || "unknown")} /></div>
        <div className="asset-five-topology-map" ref={topologyMapRef}>
          <ReactFlow
            key={cluster?.id || "empty"}
            nodes={flowNodes}
            edges={flowEdges}
            nodeTypes={normalizedTopologyNodeTypes}
            fitView
            fitViewOptions={{ padding: 0.08, maxZoom: 1 }}
            minZoom={0.05}
            maxZoom={1.25}
            nodesDraggable={false}
            nodesConnectable={false}
            proOptions={{ hideAttribution: true }}
          >
            <TopologyAutoFit containerRef={topologyMapRef} topologyKey={JSON.stringify([cluster?.id, topology.positions, topology.edges])} />
            <Background color="#e1e5e8" gap={20} size={1} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
        <div className="asset-five-topology-foot"><span>集群 ID <strong>#{cluster?.id || "-"}</strong></span><span>架构 <strong><DiscoveredArchitecture item={cluster} /></strong></span><span>数据来源 <strong>{cluster?.discovery_source === "manual" ? "人工创建" : "自动发现"}</strong></span></div>
      </section>
      <HADiagnosticModal open={haModalOpen} diagnostic={haDiagnostic} history={haHistory} onClose={() => setHaModalOpen(false)} />
      <HAPlanModal result={haPlanResult} onClose={() => setHaPlanResult(null)} />
    </div>
  );
}

function FiveLayerInstanceList({ items, loading, testingInstanceId, onOpen, onTest, onCorrect, onEditConnection, onDelete }) {
  const [keyword, setKeyword] = useState("");
  const [dbType, setDbType] = useState();
  const filtered = items.filter((item) => {
    const textValue = [instanceDisplayName(item), item.name, item.endpoint, item.cluster_name, item.host_name, item.environment].filter(Boolean).join(" ").toLowerCase();
    return (!keyword || textValue.includes(keyword.toLowerCase())) && (!dbType || item.db_type === dbType);
  });
  return (
    <section className="asset-surface asset-inventory-surface">
      <div className="asset-filter-bar"><Input.Search allowClear placeholder="搜索实例、IP、集群或主机" value={keyword} onChange={(event) => setKeyword(event.target.value)} /><Select allowClear placeholder="数据库类型" value={dbType} onChange={setDbType} options={Object.keys(DB_TYPE_COLORS).map((value) => ({ value, label: value }))} /><span className="asset-filter-count">{filtered.length} / {items.length}</span></div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={filtered}
        tableLayout="fixed"
        scroll={{ x: 1810 }}
        pagination={{ defaultPageSize: 12, showSizeChanger: false }}
        columns={[
          { title: "实例名", dataIndex: "name", width: 180, fixed: "left", render: (_, row) => <Button type="link" className="asset-inline-link" onClick={() => onOpen("instance", row)}>{instanceDisplayName(row)}</Button> },
          { title: "数据库类型", dataIndex: "db_type", width: 130, render: (value) => <DbTypeTag value={value} /> },
          { title: "集群", dataIndex: "cluster_name", width: 210, ellipsis: true, render: (value) => <ClusterName value={value} /> },
          { title: "架构", dataIndex: "architecture", width: 160, render: (_, row) => <DiscoveredArchitecture item={row} /> },
          { title: "角色", dataIndex: "role", width: 160, render: (_, row) => <DiscoveredRole item={row} /> },
          { title: "复制延迟", dataIndex: "replication_delay_seconds", width: 130, render: (_, row) => <ReplicationLagValue item={row} compact /> },
          { title: "IP:Port", dataIndex: "endpoint", width: 180 },
          { title: "版本", dataIndex: "version", width: 130, render: (value, row) => <DatabaseVersion value={value} dbType={row.db_type} /> },
          { title: "主机", dataIndex: "host_name", width: 160, render: (value, row) => value || row.host_ip || "-" },
          { title: "操作系统", key: "os", width: 170, render: (_, row) => [row.os_type, row.os_version].filter(Boolean).join(" ") || "待采集" },
          { title: "区域 / 机房 / 可用区", key: "location", width: 260, render: (_, row) => locationText(row) },
          { title: "环境", dataIndex: "environment", width: 150 },
          { title: "状态", dataIndex: "status", width: 110, render: (_, row) => <HealthState status={effectiveHealthStatus(row)} /> },
          { title: "操作", key: "action", fixed: "right", width: 310, render: (_, row) => <Space size={0}><Button type="link" icon={<CheckCircleOutlined />} loading={testingInstanceId === row.id} disabled={!row.native_instance_id} onClick={() => onTest(row)}>测试</Button><Button type="link" icon={<DatabaseOutlined />} disabled={!row.native_instance_id} onClick={() => onEditConnection(row)}>编辑连接</Button><Button type="link" icon={<EditOutlined />} onClick={() => onCorrect("instance", row)}>校正</Button><Button type="link" danger icon={<DeleteOutlined />} disabled={!row.native_instance_id} onClick={() => onDelete(row)}>删除</Button></Space> },
        ]}
      />
    </section>
  );
}

function FiveLayerHostList({ items, loading, onOpen, onCorrect }) {
  return (
    <section className="asset-surface asset-inventory-surface">
      <Table
        rowKey="id"
        loading={loading}
        dataSource={items}
        tableLayout="fixed"
        scroll={{ x: 1580 }}
        pagination={{ defaultPageSize: 12, showSizeChanger: false }}
        columns={[
          { title: "主机名", dataIndex: "name", width: 180, fixed: "left", render: (value, row) => <Button type="link" className="asset-inline-link" onClick={() => onOpen("host", row)}>{value}</Button> },
          { title: "IP", dataIndex: "ip", width: 170 },
          { title: "OS", key: "os", width: 210, render: (_, row) => [row.os_type, row.os_version].filter(Boolean).join(" ") || "待采集" },
          { title: "CPU", key: "cpu", width: 190, render: (_, row) => row.cpu_cores ? `${row.cpu_cores} 核${row.cpu_model ? ` · ${row.cpu_model}` : ""}` : "待采集" },
          { title: "内存", dataIndex: "memory_mb", width: 120, render: formatMemory },
          { title: "区域", key: "location", width: 250, render: (_, row) => locationText(row) },
          { title: "环境", dataIndex: "environment", width: 150 },
          { title: "承载实例", dataIndex: "instance_count", width: 190, render: (value, row) => <HostedInstanceSummary count={value} dbTypes={row.db_types} /> },
          { title: "状态", dataIndex: "status", width: 100, render: (value) => <HealthState status={value} /> },
          { title: "操作", key: "action", fixed: "right", width: 80, render: (_, row) => <Button type="link" icon={<EditOutlined />} onClick={() => onCorrect("host", row)}>校正</Button> },
        ]}
      />
    </section>
  );
}

function FiveLayerDiscovery({ sources, batches, preview, loading, onPreview, onApply, onCreateSource, onExternalSync }) {
  const externalSources = sources.filter((item) => item.source_type !== "internal");
  return (
    <div className="asset-center-stack">
      <section className="asset-discovery-band"><div><span className="asset-source-icon is-internal"><SyncOutlined /></span><div><strong>自动发现与五层建模</strong><span>识别主机、数据库成员、集群归属、角色和冲突</span></div></div><Space><Button icon={<EyeOutlined />} loading={loading} onClick={onPreview}>查看差异</Button><Button type="primary" icon={<SyncOutlined />} loading={loading} onClick={onApply}>立即识别</Button></Space></section>
      {preview ? <section className="asset-diff-strip"><Metric label="新增" value={preview.created_count} /><Metric label="更新" value={preview.updated_count} /><Metric label="待确认" value={preview.normalized?.pending_instance_count || preview.missing_count} tone="warning" /><Metric label="建议集群" value={preview.normalized?.cluster_count || preview.relation_count} /><Metric label="冲突" value={preview.normalized?.conflict_count || 0} tone="warning" /></section> : null}
      <section className="asset-surface">
        <div className="asset-section-heading asset-section-heading-actions"><div><strong>发现批次</strong><span>{batches.length} 条记录</span></div><Space><Button icon={<PlusOutlined />} onClick={onCreateSource}>外部 CMDB</Button><Button icon={<ApiOutlined />} disabled={!externalSources.length} onClick={onExternalSync}>导入资产</Button></Space></div>
        <Table rowKey="id" dataSource={batches} pagination={{ defaultPageSize: 10, showSizeChanger: false }} columns={[
          { title: "批次", dataIndex: "id", width: 90, render: (value) => `#${value}` },
          { title: "发现来源", dataIndex: "source_name", width: 200 },
          { title: "发现资产", dataIndex: "discovered_count", width: 110 },
          { title: "建议集群", dataIndex: "suggested_cluster_count", width: 110 },
          { title: "待确认实例", dataIndex: "pending_instance_count", width: 120 },
          { title: "冲突项", dataIndex: "conflict_count", width: 90, render: (value) => <Tag color={value ? "orange" : "green"}>{value}</Tag> },
          { title: "结果", dataIndex: "status", width: 110, render: (value) => <HealthState status={value === "success" ? "healthy" : "warning"} /> },
          { title: "完成时间", dataIndex: "finished_at", width: 190, render: formatTime },
        ]} />
      </section>
      <section className="asset-surface"><div className="asset-section-heading"><div><strong>外部资产源</strong><span>{externalSources.length} 个</span></div></div><Table rowKey="id" pagination={false} dataSource={externalSources} columns={[{ title: "名称", dataIndex: "source_name" }, { title: "类型", dataIndex: "source_type", width: 140 }, { title: "地址", dataIndex: "endpoint", render: (value) => value || "离线导入" }, { title: "状态", dataIndex: "enabled", width: 110, render: (value) => <HealthState status={value ? "healthy" : "disabled"} /> }]} /></section>
    </div>
  );
}

const compactJson = (value) => {
  if (!value) return "-";
  const textValue = JSON.stringify(value);
  return textValue.length > 100 ? `${textValue.slice(0, 100)}…` : textValue;
};

function FiveLayerCorrectionAudit({ items }) {
  return (
    <section className="asset-surface">
      <div className="asset-section-heading"><div><strong>校正与审计</strong><span>{items.length} 条人工变更</span></div></div>
      <Table rowKey="id" dataSource={items} scroll={{ x: 1200 }} pagination={{ defaultPageSize: 12, showSizeChanger: false }} columns={[
        { title: "时间", dataIndex: "create_time", width: 190, render: formatTime },
        { title: "对象", key: "entity", width: 150, render: (_, row) => `${row.entity_type} #${row.entity_id}` },
        { title: "动作", dataIndex: "action", width: 150, render: (value) => ({ update: "修改元数据", change_role: "修改角色", move_instance: "调整归属", split_cluster: "拆分集群", merge: "合并重复资产", delete_connection: "删除数据库连接" }[value] || value) },
        { title: "修改前", dataIndex: "before", width: 280, ellipsis: true, render: (value) => <Tooltip title={JSON.stringify(value)}>{compactJson(value)}</Tooltip> },
        { title: "修改后", dataIndex: "after", width: 280, ellipsis: true, render: (value) => <Tooltip title={JSON.stringify(value)}>{compactJson(value)}</Tooltip> },
        { title: "操作人", dataIndex: "operator", width: 120 },
        { title: "修改原因", dataIndex: "reason", ellipsis: true },
      ]} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无人工校正记录" /> }} />
    </section>
  );
}

export default function AssetCenterPage({ loading: instanceLoading = false, onChanged, engine = "oracle" }) {
  const [activeTab, setActiveTab] = useState("overview");
  const [overview, setOverview] = useState({ db_type_counts: {} });
  const [tree, setTree] = useState({ regions: [] });
  const [clusters, setClusters] = useState([]);
  const [assetInstances, setAssetInstances] = useState([]);
  const [hosts, setHosts] = useState([]);
  const [sources, setSources] = useState([]);
  const [batches, setBatches] = useState([]);
  const [corrections, setCorrections] = useState([]);
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [detail, setDetail] = useState(null);
  const [detailType, setDetailType] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [testingInstanceId, setTestingInstanceId] = useState(null);
  const [correctionEntity, setCorrectionEntity] = useState(null);
  const [sourceModalOpen, setSourceModalOpen] = useState(false);
  const [syncModalOpen, setSyncModalOpen] = useState(false);
  const [instanceDrawerOpen, setInstanceDrawerOpen] = useState(() => {
    try {
      return window.sessionStorage.getItem("mesdb:open-instance-drawer") === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    if (!instanceDrawerOpen) return;
    try {
      window.sessionStorage.removeItem("mesdb:open-instance-drawer");
    } catch {
      void 0;
    }
  }, [instanceDrawerOpen]);
  const [editingInstance, setEditingInstance] = useState(null);
  const [sourceForm] = Form.useForm();
  const [syncForm] = Form.useForm();
  const [correctionForm] = Form.useForm();
  const correctionAction = Form.useWatch("action", correctionForm);

  const fetchAll = useCallback(async () => {
    const [overviewData, treeData, clusterData, instanceData, hostData, sourceData, batchData, correctionData] = await Promise.all([
      getAssetOverview(),
      getAssetTree(),
      listAssetClusters(),
      listAssetInstances(),
      listAssetHosts(),
      listCmdbSources(),
      listAssetDiscoveryBatches({ limit: 100 }),
      listAssetCorrections({ limit: 200 }),
    ]);
    return {
      overview: overviewData || { db_type_counts: {} },
      tree: treeData || { regions: [] },
      clusters: clusterData.items || [],
      assetInstances: instanceData.items || [],
      hosts: hostData.items || [],
      sources: sourceData.items || [],
      batches: batchData.items || [],
      corrections: correctionData.items || [],
    };
  }, []);

  const applyData = (data) => {
    setOverview(data.overview);
    setTree(data.tree);
    setClusters(data.clusters);
    setAssetInstances(data.assetInstances);
    setHosts(data.hosts);
    setSources(data.sources);
    setBatches(data.batches);
    setCorrections(data.corrections);
  };

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      applyData(await fetchAll());
    } catch (error) {
      setLoadError(error.message || "资产中心加载失败");
    } finally {
      setLoading(false);
    }
  }, [fetchAll]);

  useEffect(() => { load(); }, [load]);

  const previewDiscovery = async () => {
    setLoading(true);
    try {
      const result = await previewAssetDiscovery();
      setPreview(result);
      message.success("发现差异已更新");
    } catch (error) {
      message.error(error.message || "发现预览失败");
    } finally {
      setLoading(false);
    }
  };

  const applyDiscovery = async () => {
    setLoading(true);
    try {
      const result = await applyAssetDiscovery({ probe_live: true });
      setPreview(result);
      const summary = `识别完成：${result.normalized?.host_count || 0} 台主机、${result.normalized?.cluster_count || 0} 个集群、${result.normalized?.instance_count || 0} 个实例`;
      if (result.warning_count) message.warning(`${summary}；${result.warning_count} 个采集问题待处理`);
      else message.success(summary);
      applyData(await fetchAll());
    } catch (error) {
      message.error(error.message || "自动识别失败");
    } finally {
      setLoading(false);
    }
  };

  const openEntity = async (type, row) => {
    setDetailType(type);
    setDetail(row);
    setDetailLoading(true);
    try {
      if (type === "cluster") setDetail(await getAssetCluster(row.id));
      if (type === "host") setDetail(await getAssetHost(row.id));
      if (type === "instance") setDetail(await getNormalizedAssetInstance(row.id));
    } catch (error) {
      message.error(error.message || "资产详情加载失败");
    } finally {
      setDetailLoading(false);
    }
  };

  const openConnectionEditor = async (row) => {
    if (!row?.native_instance_id) {
      message.warning("该资产来自外部 CMDB，没有本地数据库连接配置");
      return;
    }
    try {
      const nativeInstance = await instanceApi.get(row.native_instance_id);
      setEditingInstance(nativeInstance);
      setInstanceDrawerOpen(true);
    } catch (error) {
      message.error(error.message || "数据库连接配置加载失败");
    }
  };

  const testConnection = async (row) => {
    if (!row?.native_instance_id) {
      message.warning("该资产没有可测试的本地数据库连接配置");
      return;
    }
    const refreshConnectionState = async () => {
      try {
        applyData(await fetchAll());
        if (detailType === "instance" && detail?.id === row.id) {
          setDetail(await getNormalizedAssetInstance(row.id));
        }
      } catch {
        message.warning("连接状态已记录，但页面状态刷新失败，请点击“刷新”重试");
      }
    };
    setTestingInstanceId(row.id);
    try {
      const result = await instanceApi.test(row.native_instance_id);
      await refreshConnectionState();
      if (result.ok) {
        const version = formatDatabaseVersion(result.detail?.version || row.version, row.db_type);
        const detectedArchitecture = result.detail?.architecture || {};
        const architectureLabel = ARCHITECTURE_LABELS[detectedArchitecture.architecture_type]
          || detectedArchitecture.architecture_type
          || "未返回";
        const roleLabel = ROLE_LABELS[detectedArchitecture.role_type]
          || detectedArchitecture.role_type
          || "未返回";
        Modal.success({
          title: "数据库连接测试通过",
          content: <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label="实例">{instanceDisplayName(row)}</Descriptions.Item>
            <Descriptions.Item label="地址">{row.endpoint}</Descriptions.Item>
            <Descriptions.Item label="版本">{version || "已连接"}</Descriptions.Item>
            <Descriptions.Item label="实时探测">{architectureLabel} · {roleLabel}</Descriptions.Item>
            <Descriptions.Item label="说明">本次只验证连接，不会改写拓扑；点击“自动识别”后才会刷新架构和节点角色。</Descriptions.Item>
          </Descriptions>,
        });
      } else {
        Modal.error({
          title: "数据库连接测试失败",
          content: result.detail?.error || result.message || "连接失败",
        });
      }
    } catch (error) {
      await refreshConnectionState();
      Modal.error({ title: "数据库连接测试失败", content: error.message || "连接失败" });
    } finally {
      setTestingInstanceId(null);
    }
  };

  const removeConnection = (row) => {
    if (!row?.native_instance_id) {
      message.warning("外部 CMDB 资产需要从原数据源删除或停用");
      return;
    }
    Modal.confirm({
      title: `删除数据库连接“${row.name}”？`,
      content: "将删除连接账号配置，并从资产清单和拓扑中隐藏该实例；历史巡检、诊断和报告仍会保留。",
      okText: "确认删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          await instanceApi.remove(row.native_instance_id);
          if (detail?.id === row.id) setDetail(null);
          await onChanged?.();
          applyData(await fetchAll());
          message.success("数据库连接及当前资产投影已删除");
        } catch (error) {
          message.error(error.message || "删除数据库连接失败");
          throw error;
        }
      },
    });
  };

  const locationOptions = useMemo(() => {
    const unique = new Map();
    [...hosts, ...clusters, ...assetInstances].forEach((item) => {
      if (item.env_id && !unique.has(item.env_id)) unique.set(item.env_id, item);
    });
    return [...unique.values()];
  }, [assetInstances, clusters, hosts]);

  const openCorrection = (type, row) => {
    setCorrectionEntity({ type, row });
    correctionForm.setFieldsValue({
      action: "update",
      name: row.name,
      status: row.status,
      role: row.role,
      host_id: row.host_id,
      cluster_id: row.cluster_id,
      version: row.version,
      architecture: row.architecture,
      hostname: row.hostname,
      os_type: row.os_type,
      os_version: row.os_version,
      cpu_cores: row.cpu_cores,
      memory_mb: row.memory_mb,
      env_id: row.env_id,
      entity_ids: [],
      reason: "",
    });
  };

  const saveCorrection = async () => {
    try {
      const values = await correctionForm.validateFields();
      const { type, row } = correctionEntity;
      const payload = {
        action: values.action,
        entity_type: type,
        entity_id: row.id,
        entity_ids: values.entity_ids || [],
        reason: values.reason,
        changes: {},
      };
      if (values.action === "update") {
        const fields = type === "instance"
          ? ["name", "status", "role", "host_id", "cluster_id", "version"]
          : type === "cluster"
            ? ["name", "status", "architecture"]
            : ["name", "status", "hostname", "os_type", "os_version", "cpu_cores", "memory_mb"];
        fields.forEach((field) => {
          if (values[field] !== undefined && values[field] !== row[field]) payload.changes[field] = values[field];
        });
        if (values.env_id && values.env_id !== row.env_id) {
          const location = locationOptions.find((item) => item.env_id === values.env_id);
          if (location) Object.assign(payload.changes, {
            region_id: location.region_id,
            site_id: location.site_id,
            zone_id: location.zone_id,
            env_id: location.env_id,
          });
        }
      }
      if (values.action === "split_cluster") {
        payload.changes = { name: values.name, architecture: values.architecture };
      }
      await createAssetCorrection(payload);
      message.success(values.action === "merge" ? "重复资产已合并" : values.action === "split_cluster" ? "集群已拆分" : "人工校正已保存");
      setCorrectionEntity(null);
      applyData(await fetchAll());
    } catch (error) {
      if (!error?.errorFields) message.error(error.message || "保存人工校正失败");
    }
  };

  const createSource = async () => {
    try {
      await createCmdbSource(await sourceForm.validateFields());
      sourceForm.resetFields();
      setSourceModalOpen(false);
      message.success("外部 CMDB 数据源已保存");
      applyData(await fetchAll());
    } catch (error) {
      if (!error?.errorFields) message.error(error.message || "保存数据源失败");
    }
  };

  const externalSync = async () => {
    try {
      const values = await syncForm.validateFields();
      const items = JSON.parse(values.items_text);
      if (!Array.isArray(items)) throw new Error("资产数据必须是 JSON 数组");
      const result = await syncCmdbAssets({ source_id: values.source_id, authoritative_snapshot: Boolean(values.authoritative_snapshot), items });
      setSyncModalOpen(false);
      message.success(`导入完成：新增 ${result.created_count || 0}，更新 ${result.updated_count || 0}`);
      applyData(await fetchAll());
    } catch (error) {
      if (!error?.errorFields) message.error(error.message || "外部资产导入失败");
    }
  };

  const sameTypeItems = correctionEntity?.type === "host" ? hosts : correctionEntity?.type === "cluster" ? clusters : assetInstances;
  const splitMembers = correctionEntity?.type === "cluster" ? assetInstances.filter((item) => item.cluster_id === correctionEntity.row.id) : [];
  const actionOptions = correctionEntity?.type === "cluster"
    ? [{ value: "update", label: "修改元数据" }, { value: "split_cluster", label: "拆分错误集群" }, { value: "merge", label: "合并重复集群" }]
    : [{ value: "update", label: "修改元数据" }, { value: "merge", label: `合并重复${correctionEntity?.type === "host" ? "主机" : "实例"}` }];

  const tabs = [
    { key: "overview", label: "总览", children: <FiveLayerOverview overview={overview} tree={tree} /> },
    { key: "topology", label: "集群拓扑", children: <FiveLayerTopology clusters={clusters} instances={assetInstances} loading={loading} onOpen={openEntity} onCorrect={openCorrection} /> },
    { key: "instances", label: "实例清单", children: <FiveLayerInstanceList items={assetInstances} loading={loading || instanceLoading} testingInstanceId={testingInstanceId} onOpen={openEntity} onTest={testConnection} onCorrect={openCorrection} onEditConnection={openConnectionEditor} onDelete={removeConnection} /> },
    { key: "hosts", label: "主机清单", children: <FiveLayerHostList items={hosts} loading={loading} onOpen={openEntity} onCorrect={openCorrection} /> },
    { key: "discovery", label: "发现与同步", children: <FiveLayerDiscovery sources={sources} batches={batches} preview={preview} loading={loading} onPreview={previewDiscovery} onApply={applyDiscovery} onCreateSource={() => setSourceModalOpen(true)} onExternalSync={() => setSyncModalOpen(true)} /> },
    { key: "audit", label: "校正与审计", children: <FiveLayerCorrectionAudit items={corrections} /> },
  ];

  return (
    <div className="asset-center-page asset-five-layer-page">
      <header className="asset-center-header">
        <div className="asset-center-title"><span><DatabaseOutlined /></span><div><h2>资产中心</h2><small>{overview.host_count || 0} 台主机 · {overview.cluster_count || 0} 个集群 · {overview.instance_count || 0} 个实例</small></div></div>
        <Space wrap><Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button><Button icon={<SyncOutlined />} loading={loading} onClick={applyDiscovery}>自动识别</Button><Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditingInstance(null); setInstanceDrawerOpen(true); }}>新增实例</Button></Space>
      </header>
      {loadError ? <Alert type="error" showIcon message="资产中心加载失败" description={loadError} action={<Button size="small" onClick={load}>重试</Button>} /> : null}
      <Tabs className="asset-center-tabs" activeKey={activeTab} onChange={setActiveTab} items={tabs} />

      <Modal
        title={`${detailType === "host" ? "主机" : detailType === "cluster" ? "集群" : "实例"}详情`}
        open={Boolean(detail)}
        width={820}
        centered
        footer={null}
        loading={detailLoading}
        className="asset-detail-modal"
        onCancel={() => setDetail(null)}
      >
        {detail ? (
          <Space direction="vertical" size={16} className="full-width">
            <div className="asset-detail-modal-actions">
              <Space wrap>
                {detailType === "instance" && detail.native_instance_id ? <Button icon={<CheckCircleOutlined />} loading={testingInstanceId === detail.id} onClick={() => testConnection(detail)}>测试</Button> : null}
                {detailType === "instance" && detail.native_instance_id ? <Button icon={<DatabaseOutlined />} onClick={() => openConnectionEditor(detail)}>编辑连接</Button> : null}
                <Button icon={<EditOutlined />} onClick={() => openCorrection(detailType, detail)}>人工校正</Button>
                {detailType === "instance" && detail.native_instance_id ? <Button danger icon={<DeleteOutlined />} onClick={() => removeConnection(detail)}>删除</Button> : null}
              </Space>
            </div>
            {detailType === "instance" ? <ConnectionValidationSummary item={detail} /> : null}
            {detailType === "instance" ? <ReplicationLagDetails item={detail} /> : null}
            <Descriptions column={1} size="small" bordered>
              {Object.entries(detail).filter(([key, value]) => !LAG_DETAIL_KEYS.has(key) && ["string", "number", "boolean"].includes(typeof value)).map(([key, value]) => (
                <Descriptions.Item key={key} label={ASSET_FACT_LABELS[key] || ({ cluster_name: "集群", host_name: "主机", endpoint: "访问地址", architecture: "架构", role: "角色", region: "区域", site: "机房", zone: "可用区" }[key] || key)}>
                  {key === "status" ? <HealthState status={effectiveHealthStatus(detail)} /> : key === "db_type" ? <DbTypeTag value={value} /> : key === "architecture" ? <DiscoveredArchitecture item={detail} /> : key === "role" ? <DiscoveredRole item={detail} /> : key === "cluster_name" ? <ClusterName value={value} /> : ["version", "db_version"].includes(key) ? <DatabaseVersion value={value} dbType={detail.db_type} /> : String(value)}
                </Descriptions.Item>
              ))}
            </Descriptions>
            {detail.instances?.length ? <Table rowKey="id" size="small" pagination={false} scroll={{ x: 620 }} dataSource={detail.instances} columns={[{ title: "实例", dataIndex: "name", render: (_, row) => instanceDisplayName(row) }, { title: "角色", dataIndex: "role", render: (_, row) => <DiscoveredRole item={row} /> }, { title: "地址", dataIndex: "endpoint" }, { title: "版本", dataIndex: "version", render: (value, row) => <DatabaseVersion value={value} dbType={row.db_type || detail.db_type} /> }, { title: "状态", dataIndex: "status", render: (_, row) => <HealthState status={effectiveHealthStatus(row)} /> }]} /> : null}
          </Space>
        ) : null}
      </Modal>

      <Modal title="人工校正" open={Boolean(correctionEntity)} onCancel={() => setCorrectionEntity(null)} onOk={saveCorrection} okText="保存校正" width={720}>
        <Form form={correctionForm} layout="vertical">
          <Row gutter={12}>
            <Col span={24}><Form.Item name="action" label="校正动作" rules={[{ required: true }]}><Select options={actionOptions} /></Form.Item></Col>
            {correctionAction === "update" ? <>
              <Col xs={24} md={12}><Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item></Col>
              <Col xs={24} md={12}><Form.Item name="status" label="状态"><Select options={["healthy", "warning", "critical", "unknown", "disabled"].map((value) => ({ value, label: HEALTH_LABELS[value] || value }))} /></Form.Item></Col>
              {correctionEntity?.type === "instance" ? <>
                <Col xs={24} md={12}><Form.Item name="role" label="实例角色"><Select options={Object.entries(ROLE_LABELS).filter(([value]) => !["readonly", "dr", "report"].includes(value)).map(([value, label]) => ({ value, label }))} /></Form.Item></Col>
                <Col xs={24} md={12}><Form.Item name="version" label="数据库版本"><Input /></Form.Item></Col>
                <Col xs={24} md={12}><Form.Item name="host_id" label="所属主机"><Select showSearch optionFilterProp="label" options={hosts.map((item) => ({ value: item.id, label: `${item.name} · ${item.ip}` }))} /></Form.Item></Col>
                <Col xs={24} md={12}><Form.Item name="cluster_id" label="所属集群"><Select showSearch optionFilterProp="label" options={clusters.map((item) => ({ value: item.id, label: clusterOptionLabel(item), title: item.name }))} /></Form.Item></Col>
              </> : null}
              {correctionEntity?.type === "cluster" ? <Col xs={24} md={12}><Form.Item name="architecture" label="架构"><Select options={Object.entries(ARCHITECTURE_LABELS).filter(([value]) => ["single", "primary_replica", "mgr", "rac", "adg", "alwayson", "streaming_replication", "sharding"].includes(value)).map(([value, label]) => ({ value, label }))} /></Form.Item></Col> : null}
              {correctionEntity?.type === "host" ? <>
                <Col xs={24} md={12}><Form.Item name="hostname" label="Hostname"><Input /></Form.Item></Col>
                <Col xs={24} md={12}><Form.Item name="os_type" label="操作系统"><Input /></Form.Item></Col>
                <Col xs={24} md={12}><Form.Item name="os_version" label="OS 版本"><Input /></Form.Item></Col>
                <Col xs={24} md={12}><Form.Item name="cpu_cores" label="CPU 核数"><Input type="number" min={1} /></Form.Item></Col>
                <Col xs={24} md={12}><Form.Item name="memory_mb" label="内存 MB"><Input type="number" min={1} /></Form.Item></Col>
              </> : null}
              <Col xs={24} md={12}><Form.Item name="env_id" label="区域与环境"><Select showSearch optionFilterProp="label" options={locationOptions.map((item) => ({ value: item.env_id, label: `${locationText(item)} · ${item.environment}` }))} /></Form.Item></Col>
            </> : null}
            {correctionAction === "split_cluster" ? <>
              <Col span={24}><Form.Item name="entity_ids" label="移入新集群的实例" rules={[{ required: true, type: "array", min: 1 }]}><Select mode="multiple" options={splitMembers.map((item) => ({ value: item.id, label: `${item.name} · ${item.endpoint}` }))} /></Form.Item></Col>
              <Col xs={24} md={12}><Form.Item name="name" label="新集群名称" rules={[{ required: true }]}><Input /></Form.Item></Col>
              <Col xs={24} md={12}><Form.Item name="architecture" label="新集群架构" rules={[{ required: true }]}><Select options={Object.entries(ARCHITECTURE_LABELS).filter(([value]) => ["single", "primary_replica", "mgr", "rac", "adg", "alwayson", "streaming_replication", "sharding"].includes(value)).map(([value, label]) => ({ value, label }))} /></Form.Item></Col>
            </> : null}
            {correctionAction === "merge" ? <Col span={24}><Form.Item name="entity_ids" label="选择要并入当前资产的重复项" rules={[{ required: true, type: "array", min: 1 }]}><Select mode="multiple" options={sameTypeItems.filter((item) => item.id !== correctionEntity?.row.id).map((item) => ({ value: item.id, label: `${item.name}${item.ip ? ` · ${item.ip}` : ""}` }))} /></Form.Item></Col> : null}
            <Col span={24}><Form.Item name="reason" label="修改原因" rules={[{ required: true, message: "请填写修改原因" }]}><Input.TextArea rows={3} /></Form.Item></Col>
          </Row>
        </Form>
      </Modal>

      <Modal title="新增外部 CMDB" open={sourceModalOpen} onCancel={() => setSourceModalOpen(false)} onOk={createSource} okText="保存数据源"><Form form={sourceForm} layout="vertical" initialValues={{ source_type: "rest", enabled: 1 }}><Form.Item name="source_name" label="数据源名称" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="source_type" label="接入类型" rules={[{ required: true }]}><Select options={[{ value: "rest", label: "REST API" }, { value: "cmdb", label: "通用 CMDB" }, { value: "import", label: "文件导入" }]} /></Form.Item><Form.Item name="endpoint" label="服务地址"><Input placeholder="https://cmdb.example/api" /></Form.Item><Form.Item name="remark" label="备注"><Input.TextArea rows={2} /></Form.Item></Form></Modal>
      <Modal title="导入外部资产" open={syncModalOpen} onCancel={() => setSyncModalOpen(false)} onOk={externalSync} okText="执行导入" width={760}><Form form={syncForm} layout="vertical" initialValues={{ authoritative_snapshot: false }}><Form.Item name="source_id" label="数据源" rules={[{ required: true }]}><Select options={sources.filter((item) => item.source_type !== "internal").map((item) => ({ value: item.id, label: item.source_name }))} /></Form.Item><Form.Item name="authoritative_snapshot" label="权威全量快照" valuePropName="checked"><Switch checkedChildren="全量" unCheckedChildren="增量" /></Form.Item><Form.Item name="items_text" label="资产 JSON" rules={[{ required: true }]}><Input.TextArea rows={8} placeholder='[{"external_id":"db-01","asset_type":"database_instance","name":"MES DB","attributes":{"db_type":"mysql","host":"10.0.0.1","port":3306}}]' /></Form.Item><Alert type="warning" showIcon message="全量快照中缺失的旧资产只会标记为待确认，不会直接删除。" /></Form></Modal>
      <InstanceDrawer open={instanceDrawerOpen} editing={editingInstance} engine={engine} onClose={() => setInstanceDrawerOpen(false)} onSaved={async () => { setInstanceDrawerOpen(false); await onChanged?.(); }} />
    </div>
  );
}
