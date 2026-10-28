import React, { useEffect, useMemo, useState } from "react";
import { Button, Card, Space, Spin, Tag, Typography, message } from "antd";
import {
} from "@ant-design/icons";
import { capacityApi, mysqlOpsApi, systemApi } from "./apiClients";
import { OpsCenterTabs, OpsEmptyState } from "./OpsLayout";
import {
  loadQuickActionCards,
} from "./modules/quickAccess";
import {

  OracleDGPage,
  ParametersPage,
} from "./modules";

const { Title, Text } = Typography;

const RISK_META = {
  critical: { label: "严重", className: "critical" },
  warning: { label: "警告", className: "warning" },
  normal: { label: "健康", className: "healthy" },
  unknown: { label: "待查", className: "unknown" },
};

const SLO_META = {
  met: { label: "达标", color: "success" },
  breached: { label: "未达标", color: "error" },
  unknown: { label: "未验证", color: "default" },
};

function dashboardTone(value) {
  if (value === "critical" || value === "breached") return "critical";
  if (value === "warning") return "warning";
  if (value === "healthy" || value === "met") return "normal";
  return "unknown";
}

function dbTypeText(value) {
  return {
    oracle: "Oracle",
    mysql: "MySQL",
    postgresql: "PostgreSQL",
    sqlserver: "SQL Server",
  }[value] || (value || "数据库");
}

function alertTone(level) {
  return level === "critical" || level === "error" || level === "serious" ? "critical" : "warning";
}

function getInstanceName(instance) {
  return instance?.instance_name || instance?.name || instance?.host || "未命名实例";
}

function EditionDashboard({ instances = [], capacityAlerts = [], onCapacityAlertsClosed, onNavigate }) {
  const [dashStats, setDashStats] = useState(null);
  const [quickCards, setQuickCards] = useState(() => loadQuickActionCards());

  useEffect(() => {
    systemApi.dashboardStats().then((d) => setDashStats(d)).catch(() => setDashStats(null));
  }, []);

  useEffect(() => {
    const syncQuickCards = () => setQuickCards(loadQuickActionCards());
    const syncFromStorage = (event) => {
      if (event.key && event.key !== QUICK_ACTION_STORAGE_KEY) return;
      syncQuickCards();
    };
    window.addEventListener(QUICK_ACCESS_UPDATED_EVENT, syncQuickCards);
    window.addEventListener("storage", syncFromStorage);
    return () => {
      window.removeEventListener(QUICK_ACCESS_UPDATED_EVENT, syncQuickCards);
      window.removeEventListener("storage", syncFromStorage);
    };
  }, []);

  const dbInstances = instances || [];
  const enabledCount = dbInstances.filter((item) => item.enabled !== false).length;
  const disabledCount = dbInstances.length - enabledCount;
  const capacityCritical = capacityAlerts.filter((item) => item.level === "critical").length;
  const activeAlertCount = dashStats?.alert_count ?? capacityAlerts.length;
  const slowSqlCount = dashStats?.slow_sql_count ?? 0;
  const reportCount = dashStats?.check_report_count ?? 0;
  const aiReportCount = dashStats?.ai_report_count ?? 0;
  const todayRiskCount = activeAlertCount + slowSqlCount;
  const healthTone = dashStats?.platform_status
    ? dashboardTone(dashStats.platform_status)
    : capacityCritical > 0 ? "critical" : todayRiskCount > 0 || disabledCount > 0 ? "warning" : "unknown";
  const healthTitle = dashStats?.platform_status === "critical"
    ? "平台存在高优先级风险"
    : dashStats?.platform_status === "warning"
      ? "平台有风险或数据尚未验证"
      : capacityCritical > 0
        ? "平台存在严重风险"
        : todayRiskCount > 0
          ? "今日有风险待跟进"
          : dashStats?.platform_status === "healthy" ? "平台运行平稳" : "平台状态待确认";
  const healthAdvice = dashStats?.platform_status === "warning" && (dashStats?.slo_cards || []).some((item) => item.status === "unknown")
    ? "部分运行目标缺少有效样本。请先完成巡检、备份和告警闭环，再依据可核验数据判断健康状态。"
    : capacityCritical > 0
      ? "建议先进入综合告警确认严重项，再发起巡检生成处置证据。"
      : todayRiskCount > 0
        ? "建议按告警、慢 SQL、容量风险顺序处理，并同步生成巡检报告。"
        : dashStats?.platform_status === "healthy"
          ? "当前未发现突出风险，可继续关注巡检任务和实例健康变化。"
          : "尚未取得有效的运行数据，当前无法判断平台健康状态。";
  const fallbackKpis = [
    { label: "实例", value: dbInstances.length, desc: `启用 ${enabledCount}`, icon: <DatabaseOutlined />, tone: "normal" },
    { label: "风险", value: todayRiskCount, desc: `告警 ${activeAlertCount} · 慢 SQL ${slowSqlCount}`, icon: <AlertOutlined />, tone: healthTone },
    { label: "巡检报告", value: reportCount, desc: "累计报告", icon: <FileTextOutlined />, tone: "normal" },
    { label: "AI 诊断", value: aiReportCount, desc: "诊断记录", icon: <RobotOutlined />, tone: "normal" },
  ];
  const kpiIcons = {
    instances: <DatabaseOutlined />,
    alerts: <AlertOutlined />,
    inspections: <PlayCircleOutlined />,
    backups: <CheckCircleOutlined />,
    reports: <FileTextOutlined />,
    work_queue: <AppstoreOutlined />,
  };
  const kpis = dashStats?.kpis?.length
    ? dashStats.kpis.map((item) => ({
        ...item,
        desc: item.secondary,
        icon: kpiIcons[item.key] || <AppstoreOutlined />,
        tone: dashboardTone(item.status),
      }))
    : fallbackKpis;

  const defaultQuickEntries = [
    { key: "inspectionCenter", title: "一键巡检", desc: "发起数据库体检", icon: <PlayCircleOutlined /> },
    { key: "unifiedAlerts", title: "综合告警", desc: "统一查看风险事件", icon: <AlertOutlined /> },
    { key: "inspectionReports", title: "巡检报告", desc: "沉淀报告与结论", icon: <FileTextOutlined /> },
    { key: "instances", title: "实例配置", desc: "维护实例与连接", icon: <AppstoreOutlined /> },
  ];
  const pinnedQuickEntries = useMemo(
    () => quickCards.filter((item) => item.enabled !== false && item.pinnedPage && item.targetKey),
    [quickCards],
  );
  const quickEntries = pinnedQuickEntries.length ? pinnedQuickEntries : defaultQuickEntries;

  const matchedAlertsFor = (instance) => {
    const name = getInstanceName(instance);
    const host = instance?.host || "";
    return capacityAlerts.filter((alert) => {
      const text = `${alert?.message || ""} ${alert?.title || ""}`;
      return (name && text.includes(name)) || (host && text.includes(host));
    });
  };
  const focusInstances = dbInstances
    .map((instance) => {
      const matchedAlerts = matchedAlertsFor(instance);
      const hasCritical = matchedAlerts.some((item) => alertTone(item.level) === "critical");
      const tone = instance.enabled === false || hasCritical
        ? "critical"
        : matchedAlerts.length > 0 || !instance.ssh_host
          ? "warning"
          : "unknown";
      const score = (instance.enabled === false ? 80 : 0)
        + matchedAlerts.reduce((sum, item) => sum + (alertTone(item.level) === "critical" ? 70 : 35), 0)
        + (!instance.ssh_host ? 12 : 0);
      const riskCount = matchedAlerts.length + (instance.enabled === false ? 1 : 0) + (!instance.ssh_host ? 1 : 0);
      const healthScore = instance.health_score ?? instance.last_health_score ?? instance.inspection_score ?? null;
      return { instance, matchedAlerts, tone, score, riskCount, healthScore };
    })
    .sort((a, b) => b.score - a.score || getInstanceName(a.instance).localeCompare(getInstanceName(b.instance)))
    .slice(0, 3);

  const allDynamicRows = [
    ...capacityAlerts.slice(0, 3).map((item, index) => ({
      key: `capacity-${item.id ?? index}`,
      title: alertTone(item.level) === "critical" ? "严重容量预警" : "容量风险提醒",
      desc: item.message || "容量阈值触发预警",
      tone: alertTone(item.level),
    })),
    slowSqlCount > 0 && {
      key: "slow-sql",
      title: "慢 SQL 待分析",
      desc: `${slowSqlCount} 条 SQL 超过阈值，建议进入性能诊断查看执行画像。`,
      tone: "warning",
    },
    reportCount > 0 && {
      key: "inspection-report",
      title: "巡检报告已沉淀",
      desc: `当前累计 ${reportCount} 份报告，可进入报告中心查看趋势。`,
      tone: "normal",
    },
  ].filter(Boolean);
  const serverQueue = dashStats?.work_queue || [];
  const dynamicRows = serverQueue.length
    ? serverQueue.slice(0, 4).map((item) => ({
        key: item.key,
        title: item.title,
        desc: item.description,
        tone: item.priority === "critical" ? "critical" : "warning",
        detailPageKey: item.detail_page_key,
      }))
    : allDynamicRows.slice(0, 2);
  const hasMoreDynamicRows = serverQueue.length
    ? serverQueue.length > dynamicRows.length
    : allDynamicRows.length > dynamicRows.length || capacityAlerts.length > 2;

  const serverRiskRows = dashStats?.risk_ranking || [];
  const rankedInstances = serverRiskRows.length
    ? serverRiskRows.slice(0, 5).map((row) => ({
        instance: dbInstances.find((item) => item.id === row.instance_id) || {
          id: row.instance_id,
          instance_name: row.instance_name,
          db_type: row.db_type,
        },
        tone: row.risk_level === "critical" ? "critical" : row.risk_level === "warning" ? "warning" : row.risk_level === "healthy" ? "normal" : "unknown",
        riskCount: row.critical_alerts + row.warning_alerts + row.failed_backups + row.sla_breaches,
        healthScore: row.latest_inspection_score,
        displayScore: row.risk_score,
        scoreLabel: "风险分",
      }))
    : focusInstances;

  const closeCapacityAlerts = async () => {
    try {
      await capacityApi.closeAll();
      onCapacityAlertsClosed?.();
      setDashStats((prev) => prev ? { ...prev, alert_count: 0 } : prev);
      message.success("已关闭全部容量预警");
    } catch (e) {
      message.error(e.message);
    }
  };

  const go = (key, instanceId) => onNavigate?.(key, instanceId);

  return (
    <Space direction="vertical" size={16} className="full-width dashboard-page workbench-page">
      <section className={`workbench-hero workbench-tone-${healthTone}`}>
        <div className="workbench-hero-main">
          <div className="workbench-eyebrow">数据库运维工作台</div>
          <Title level={2} className="workbench-hero-title">{healthTitle}</Title>
          <Text className="workbench-hero-desc">{healthAdvice}</Text>
          <Space size={12} wrap className="workbench-hero-actions">
            <Button type="primary" onClick={() => go("unifiedAlerts")}>查看告警</Button>
            <Button onClick={() => go("inspectionCenter")}>发起巡检</Button>
          </Space>
        </div>
        <div className="workbench-hero-metrics">
          {kpis.map((item) => (
            <button
              className="workbench-hero-metric"
              key={item.key || item.label}
              type="button"
              onClick={() => item.detail_page_key && go(item.detail_page_key)}
            >
              <span className="workbench-hero-metric-icon">{item.icon}</span>
              <span className="workbench-hero-metric-label">{item.label}</span>
              <span className={`workbench-hero-metric-value workbench-text-${item.tone}`}>{item.value}</span>
              <span className="workbench-hero-metric-desc">{item.desc}</span>
            </button>
          ))}
        </div>
      </section>

      <Card
        title="运行目标与证据"
        className="workbench-card workbench-slo-card"
        extra={<Text type="secondary">近 30 天 · 每项均显示真实样本量</Text>}
        bordered={false}
      >
        <div className="workbench-slo-grid">
          {(dashStats?.slo_cards || []).map((item) => {
            const meta = SLO_META[item.status] || SLO_META.unknown;
            const hasActual = item.actual !== null && item.actual !== undefined;
            return (
              <button
                className={`workbench-slo-item is-${item.status}`}
                key={item.key}
                type="button"
                onClick={() => go(item.detail_page_key)}
              >
                <span className="workbench-slo-head"><span>{item.label}</span><Tag color={meta.color}>{meta.label}</Tag></span>
                <strong>{hasActual ? item.actual : "--"}<small>{hasActual ? item.unit : ""}</small></strong>
                <span>目标 {item.lower_is_better ? "≤" : "≥"}{item.target}{item.unit} · 样本 {item.sample_count}</span>
              </button>
            );
          })}
          {!dashStats?.slo_cards?.length ? <div className="workbench-empty-state"><Text>正在汇总运行目标样本...</Text></div> : null}
        </div>
      </Card>

      <div className="workbench-grid">
        <Card
          title="首页快速访问"
          className="workbench-card"
          extra={<Text type="secondary">{pinnedQuickEntries.length ? "固定页面标签" : "未固定时显示默认入口"}</Text>}
          bordered={false}
        >
          <div className="workbench-quick-grid">
            {quickEntries.map((item) => {
              const Icon = item.iconKey ? QUICK_ICON_MAP[item.iconKey] : null;
              return (
                <button
                  className={`workbench-quick-entry${item.pinnedPage ? " is-pinned" : ""}`}
                  key={item.key}
                  type="button"
                  onClick={() => go(item.targetKey || item.key, item.instanceId)}
                >
                  <span className="workbench-quick-icon">{Icon ? <Icon /> : item.icon}</span>
                  <span className="workbench-quick-title">{item.title}</span>
                  <span className="workbench-quick-desc">{item.subtitle || item.desc}</span>
                </button>
              );
            })}
          </div>
        </Card>

        <Card
          title={serverQueue.length ? "我的待办" : "今日风险动态"}
          className="workbench-card"
          extra={(
            <Space size={4}>
              {hasMoreDynamicRows && <Button size="small" type="text" onClick={() => go(serverQueue[0]?.detail_page_key || "unifiedAlerts")}>查看全部</Button>}
              {capacityAlerts.length > 0 ? <Button size="small" type="text" onClick={closeCapacityAlerts}>关闭容量预警</Button> : null}
            </Space>
          )}
          bordered={false}
        >
          <div className="workbench-dynamic-list">
            {dynamicRows.length > 0 ? dynamicRows.map((item) => {
              const meta = RISK_META[item.tone] || RISK_META.warning;
              return (
                <button className={`workbench-dynamic-row workbench-row-${meta.className}`} key={item.key} type="button" onClick={() => go(item.detailPageKey || "unifiedAlerts")}>
                  <span className="workbench-row-dot" />
                  <div className="workbench-row-main">
                    <div className="workbench-row-title">{item.title}</div>
                    <div className="workbench-row-desc">{item.desc}</div>
                  </div>
                  <Tag className={`workbench-risk-tag workbench-tag-${meta.className}`}>{meta.label}</Tag>
                </button>
              );
            }) : (
              <div className="workbench-empty-state">
                <CheckCircleOutlined />
                <Text>今日暂无突出风险，建议保持巡检节奏。</Text>
              </div>
            )}
          </div>
        </Card>
      </div>

      <Card
        title="重点关注实例"
        className="workbench-card workbench-focus-card"
        extra={<Button size="small" type="text" onClick={() => go("instances")}>管理实例</Button>}
        bordered={false}
      >
        <div className="workbench-instance-list">
          {rankedInstances.length > 0 ? rankedInstances.map((item, index) => {
            const instance = item.instance;
            const meta = RISK_META[item.tone] || RISK_META.unknown;
            return (
                <div className={`workbench-instance-row workbench-row-${meta.className}`} key={instance.id || `${getInstanceName(instance)}-${index}`}>
                  <div className="workbench-rank">{String(index + 1).padStart(2, "0")}</div>
                  <div className="workbench-instance-main">
                    <div className="workbench-instance-title">{getInstanceName(instance)}</div>
                    <div className="workbench-instance-meta">{dbTypeText(instance.db_type)}</div>
                  </div>
                  <div className={`workbench-attention-score workbench-text-${item.tone}`}>
                    <span>{item.displayScore ?? (item.riskCount > 0 ? item.riskCount : (item.healthScore ?? "--"))}</span>
                    <small>{item.scoreLabel || (item.riskCount > 0 ? "风险" : "健康分")}</small>
                  </div>
                  <div className="workbench-instance-tags">
                    <Tag className={`workbench-risk-tag workbench-tag-${meta.className}`}>{meta.label}</Tag>
                  </div>
              </div>
            );
          }) : (
            <div className="workbench-empty-state">
              <DatabaseOutlined />
              <Text>暂无数据库实例，请先进入实例配置添加资产。</Text>
            </div>
          )}
        </div>
      </Card>
    </Space>
  );
}

function OracleOverviewPage({ instanceId, instances }) {
  const [tab, setTab] = useState("basicInfo");
  const instance = (instances || []).find((item) => item.id === instanceId);
  return <OpsCenterTabs title="Oracle 运行概览" description="统一查看基础信息、启停、参数、安全和用户权限，危险操作仅生成脚本。" instance={instance} emptyText="请在右上角选择 Oracle 实例" activeKey={tab} onChange={setTab} items={[
      {key:"basicInfo", label:"基础信息", children:<BasicInfoPage instances={instances} selectedInstanceId={instanceId} />},
      {key:"dbControl", label:"启停管理", riskLevel:"warning", children:<DbControlPage instances={instances} selectedInstanceId={instanceId} />},
      {key:"parameters", label:"参数管理", riskLevel:"warning", children:<ParametersPage instances={instances} selectedInstanceId={instanceId} />},
      {key:"securityCheck", label:"安全检查", children:<SecurityCheckPage instances={instances} selectedInstanceId={instanceId} />},
      {key:"userMgmt", label:"用户管理", riskLevel:"warning", children:<UserMgmtPage instances={instances} selectedInstanceId={instanceId} />},
    ]} />;
}

function OracleSpacePage({ instanceId, instances }) {
  const [tab, setTab] = useState("tablespaceMonitor");
  const instance = (instances || []).find((item) => item.id === instanceId);
  return <OpsCenterTabs title="Oracle 空间管理" description="统一查看表空间、TEMP、UNDO、Redo Log 资源和扩容方案，变更默认 plan_only。" instance={instance} emptyText="请在右上角选择 Oracle 实例" activeKey={tab} onChange={setTab} items={[
      {key:"tablespaceMonitor", label:"表空间监控", children:<TablespaceMonitorPage instances={instances} selectedInstanceId={instanceId} />},
      {key:"tempTablespace", label:"TEMP 管理", riskLevel:"warning", children:<OracleTempTablespacePage instances={instances} selectedInstanceId={instanceId} />},
      {key:"undo", label:"UNDO 管理", riskLevel:"warning", children:<OracleUndoPage instances={instances} selectedInstanceId={instanceId} />},
      {key:"redoLog", label:"Redo Log", riskLevel:"warning", children:<RedoLogPage instances={instances} selectedInstanceId={instanceId} />},
    ]} />;
}

function OracleLogArchivePage({ instanceId, instances }) {
  const [tab, setTab] = useState("archiveFRA");
  const instance = (instances || []).find((item) => item.id === instanceId);
  return <OpsCenterTabs title="Oracle 日志归档" description="统一查看归档、FRA、日志清理和日常维护入口，清理与调整仅生成方案。" instance={instance} emptyText="请在右上角选择 Oracle 实例" activeKey={tab} onChange={setTab} items={[
      {key:"archiveFRA", label:"归档/FRA", riskLevel:"warning", children:<ArchiveFRAPage instances={instances} selectedInstanceId={instanceId} />},
    ]} />;
}

function OracleSessionPage({ instanceId, instances }) {
  return <LockSessionPage instances={instances} selectedInstanceId={instanceId} />;
}

function OracleObjectPage({ instanceId, instances }) {
  const [tab, setTab] = useState("tableIndex");
  const instance = (instances || []).find((item) => item.id === instanceId);
  return <OpsCenterTabs title="Oracle 对象统计" description="统一查看表、索引、统计信息、碎片和治理脚本。" instance={instance} emptyText="请在右上角选择 Oracle 实例" activeKey={tab} onChange={setTab} items={[
      {key:"tableIndex", label:"表与索引", riskLevel:"warning", children:<TableIndexPage instances={instances} selectedInstanceId={instanceId} />},
    ]} />;
}



// ====== T-071 MySQL Aggregated Pages ======

function MySQLOverviewPage({ instanceId, instances }) {
  const [tab, setTab] = useState("basic");
  const instance = (instances || []).find((item) => item.id === instanceId);
  return <OpsCenterTabs title="MySQL 运行概览" description="统一查看基础信息、参数、连接、运行状态和关键配置。" instance={instance} emptyText="请在右上角选择 MySQL 实例" activeKey={tab} onChange={setTab} items={[
      {key:"basic", label:"基础信息", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="basic" />},
      {key:"variables", label:"参数管理", riskLevel:"warning", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="variables" />},
    ]} />;
}

function MySQLStoragePage({ instanceId, instances }) {
  const [tab, setTab] = useState("capacity");
  const instance = (instances || []).find((item) => item.id === instanceId);
  return <OpsCenterTabs title="MySQL 存储日志" description="统一查看库表容量、Binlog、Relay Log、Error Log 和清理方案。" instance={instance} emptyText="请在右上角选择 MySQL 实例" activeKey={tab} onChange={setTab} items={[
      {key:"capacity", label:"库表容量", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="capacity" />},
      {key:"binlog", label:"Binlog 管理", riskLevel:"warning", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="binlog" />},
      {key:"relaylog", label:"Relay Log", riskLevel:"warning", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="relaylog" />},
      {key:"errorlog", label:"Error Log", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="errorlog" />},
    ]} />;
}

function MySQLInnoDBTxPage({ instanceId, instances }) {
  const [tab, setTab] = useState("innodb");
  const instance = (instances || []).find((item) => item.id === instanceId);
  return <OpsCenterTabs title="MySQL InnoDB 事务" description="统一查看 InnoDB 状态、长事务、锁等待、阻塞链路和死锁分析。" instance={instance} emptyText="请在右上角选择 MySQL 实例" activeKey={tab} onChange={setTab} items={[
      {key:"innodb", label:"InnoDB 状态", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="innodb" />},
      {key:"locks", label:"锁与事务", riskLevel:"critical", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="locks" />},
      {key:"deadlocks", label:"死锁分析", riskLevel:"warning", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="deadlocks" />},
    ]} />;
}

function MySQLUserSecurityPage({ instanceId, instances }) {
  const [tab, setTab] = useState("users");
  const instance = (instances || []).find((item) => item.id === instanceId);
  return <OpsCenterTabs title="MySQL 用户安全" description="统一查看账号、授权、密码状态和安全风险。" instance={instance} emptyText="请在右上角选择 MySQL 实例" activeKey={tab} onChange={setTab} items={[
      {key:"users", label:"用户权限", riskLevel:"warning", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="users" />},
      {key:"security", label:"安全概览", riskLevel:"warning", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="security" />},
    ]} />;
}

function MySQLTableIndexPage({ instanceId, instances }) {
  const [tab, setTab] = useState("tables");
  const instance = (instances || []).find((item) => item.id === instanceId);
  return <OpsCenterTabs title="MySQL 表与索引" description="统一查看表容量、主键、索引、统计信息和维护方案。" instance={instance} emptyText="请在右上角选择 MySQL 实例" activeKey={tab} onChange={setTab} items={[
      {key:"tables", label:"表与索引", riskLevel:"warning", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="tables" />},
      {key:"statistics", label:"统计信息", riskLevel:"warning", children:<MySQLDailyOperationsPage instanceId={instanceId} initialTab="statistics" />},
    ]} />;
}



// ====== T-071 Performance Pages ======

function OracleLockAnalysisPage({ instanceId, instances }) {
  return <LockSessionPage instances={instances} selectedInstanceId={instanceId} />;
}

function LockAnalysisPage({ instanceId, instances, engine }) {
  return engine === "mysql"
    ? <MySQLLockAnalysisPage instanceId={instanceId} instances={instances} />
    : <OracleLockAnalysisPage instanceId={instanceId} instances={instances} />;
}

function MySQLLockAnalysisPage({ instanceId, instances }) {
  const [data, setData] = useState({});
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!instanceId) return;
    setLoading(true);
    mysqlOpsApi.lockTransactions(instanceId).then(r => setData(r)).catch(e => setData({ error: e.message })).finally(() => setLoading(false));
  }, [instanceId]);
  if (!instanceId) return <OpsEmptyState title="请选择 MySQL 实例" description="选择 MySQL 实例后可查看事务、锁等待和阻塞链路。" />;
  return <Spin spinning={loading}><MySQLLockTab data={data} instanceId={instanceId} /></Spin>;
}

export {


};
