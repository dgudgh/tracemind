import React from "react";
import {
  AlertOutlined,
  ApiOutlined,
  BarChartOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloudServerOutlined,
  ControlOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  DeploymentUnitOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  LineChartOutlined,
  LockOutlined,
  CodeOutlined,
  PlayCircleOutlined,
  RobotOutlined,
  RocketOutlined,
  SafetyOutlined,
  SaveOutlined,
  SearchOutlined,
  SettingOutlined,
  SwapOutlined,
  TableOutlined,
  TeamOutlined,
  IdcardOutlined,
  KeyOutlined,
  ThunderboltOutlined,
  ToolOutlined,
  UndoOutlined,
  BellOutlined,
} from "@ant-design/icons";

// 菜单架构（面向扩展）：见 docs/frontend/menu-architecture-redesign.md
//
// 约定：
//   - key 统一命名空间 <域>.<模块>[.<页>]，前缀即归属。
//   - data 字段声明数据依赖契约，driver: AppPageRouter 据此注入 props。
//       static    → { activeKey, onNavigate }            不需要实例
//       list      → { instances }                         只用实例列表
//       instanceId→ { instanceId, instances }             需选中实例
//       selected  → { instances, selectedInstanceId, onNavigate }  需选中实例 + 可导航
//       special   → 由 AppPageRouter 按 key/前缀自定义渲染（特殊 props）
//   - dbTypes 在引擎子组/单引擎页上声明，子项继承。
//   - 旧 key 经 LEGACY_KEY_MAP 归一，后端模块开关（module_config）仍按旧 key 匹配。

export const MENU_CONFIG = [
  { key: "asset.center", icon: <ApiOutlined />, label: "资产管理", data: "special" },

  { key: "home.dashboard", icon: <BarChartOutlined />, label: "工作台", data: "special" },

  { key: "diagnose", icon: <RobotOutlined />, label: "智能诊断", children: [
    { key: "diagnose.smart", icon: <RobotOutlined />, label: "诊断工作台", data: "selected" },
  ]},

  { key: "monitor", icon: <AlertOutlined />, label: "监控告警", children: [
    { key: "monitor.alert", icon: <AlertOutlined />, label: "告警中心", children: [
      { key: "monitor.alert.unified", icon: <AlertOutlined />, label: "综合告警", data: "list" },
      { key: "monitor.alert.db", icon: <DatabaseOutlined />, label: "容量告警", data: "selected" },
    ]},
    { key: "monitor.observabilityGroup", icon: <ApiOutlined />, label: "可观测监控", children: [
      { key: "monitor.observability", icon: <DashboardOutlined />, label: "可观测总览", data: "static" },
    ]},
  ]},

  { key: "inspect", icon: <FileSearchOutlined />, label: "巡检", children: [
    { key: "inspect.overview", icon: <DashboardOutlined />, label: "巡检总览", data: "selected" },
    { key: "inspect.run", icon: <PlayCircleOutlined />, label: "一键巡检", data: "selected" },
    { key: "inspect.results", icon: <FileTextOutlined />, label: "任务与报告", children: [
      { key: "inspect.tasks", icon: <ClockCircleOutlined />, label: "巡检任务", data: "static" },
      { key: "inspect.reports", icon: <FileTextOutlined />, label: "巡检报告", data: "static" },
    ]},

  ]},

  { key: "perf", icon: <DashboardOutlined />, label: "性能分析", children: [
    { key: "perf.sqlAnalysis", icon: <SearchOutlined />, label: "SQL 与等待", children: [
      { key: "perf.slowsql", icon: <ClockCircleOutlined />, label: "慢 SQL 分析", data: "special" },
      { key: "perf.lock", icon: <LockOutlined />, label: "锁等待分析", data: "special" },
    ]},

    { key: "perf.trends", icon: <LineChartOutlined />, label: "基线与趋势", children: [
      { key: "perf.baseline", icon: <LineChartOutlined />, label: "性能基线", data: "selected" },
      { key: "perf.capacity", icon: <DatabaseOutlined />, label: "容量分析", data: "selected", dbTypes: ["oracle"] },
    ]},
    { key: "perf.awr", icon: <FileTextOutlined />, label: "AWR", dbTypes: ["oracle"], children: [
      { key: "perf.awr.report", icon: <FileTextOutlined />, label: "AWR 综合", data: "special" },
    ]},
  ]},

  // 能力优先：单一运维组，按右上角选中实例的 db_type 自适应（见 AppPageRouter 的 OPS_DISPATCH）。
  // engines 声明该能力支持的引擎；选中实例后菜单按引擎隐藏不支持的能力行（filterOpsByEngine）。
  { key: "ops", icon: <ToolOutlined />, label: "数据库运维", children: [
    // 能力矩阵没有独立页面：15 项能力 × 引擎的支持模式属说明性内容，写在
    // docs/blueprint/engine-capabilities.md；但 operations.capability_matrix.read
    // 能力本身保留，供 AI 调度与 API 调用（不再作为菜单入口）。
    { key: "ops.configGovernance", icon: <ControlOutlined />, label: "配置基线", data: "selected" },
    { key: "ops.runtime", icon: <DashboardOutlined />, label: "运行与会话", children: [
      { key: "ops.overview", icon: <DashboardOutlined />, label: "运行概览", data: "opsCapability",
        engines: ["oracle", "mysql", "postgresql", "sqlserver"] },
      { key: "ops.log", icon: <FolderOpenOutlined />, label: "日志管理", data: "opsCapability",
        engines: ["oracle", "postgresql", "sqlserver"] },
      { key: "ops.session", icon: <SwapOutlined />, label: "会话事务", data: "opsCapability",
        engines: ["oracle", "mysql", "postgresql", "sqlserver"] },
    ]},
    { key: "ops.data", icon: <DatabaseOutlined />, label: "空间与对象", children: [
      { key: "ops.space", icon: <DatabaseOutlined />, label: "空间管理", data: "opsCapability",
        engines: ["oracle", "mysql", "postgresql", "sqlserver"] },
      { key: "ops.object", icon: <TableOutlined />, label: "对象统计", data: "opsCapability",
        engines: ["oracle", "mysql", "postgresql", "sqlserver"] },
    ]},
    { key: "ops.reliability", icon: <SafetyOutlined />, label: "安全与高可用", children: [
      { key: "ops.security", icon: <SafetyOutlined />, label: "用户安全", data: "opsCapability",
        engines: ["mysql"] },
    ]},
  ]},

  { key: "protection", icon: <SafetyOutlined />, label: "数据保护", children: [
    // 策略、一次性方案、恢复演练和报告分开，避免执行边界混淆。


  ]},

  { key: "report", icon: <FileTextOutlined />, label: "报告中心", children: [
    { key: "report.unified", icon: <FileTextOutlined />, label: "统一报告", data: "static" },
    { key: "report.alert", icon: <AlertOutlined />, label: "告警报告", data: "static" },
  ]},

  { key: "platform", icon: <SettingOutlined />, label: "平台管理", children: [

    { key: "system", icon: <SettingOutlined />, label: "系统管理", children: [
      { key: "system.user", icon: <TeamOutlined />, label: "用户管理", data: "static" },
      { key: "system.params", icon: <ControlOutlined />, label: "系统参数", data: "static" },
      { key: "system.ai", icon: <RobotOutlined />, label: "AI 配置", data: "static" },
      { key: "system.schedule", icon: <ClockCircleOutlined />, label: "调度任务", data: "list" },
    ]},
    // 只读：展示引擎目录快照与本机驱动状态；不提供安装/激活/回滚（那些只在主机侧 CLI）。
    { key: "platform.engines", icon: <DatabaseOutlined />, label: "引擎与版本", data: "static" },
  ]},
];

// 默认进入智能诊断，一级任务域按需展开。
export const DEFAULT_OPEN_KEYS = [];

// 默认落地页
export const DEFAULT_ACTIVE_KEY = "diagnose.smart";

// 旧 key → 新 key 归一映射（兼容深链/收藏/smartNavigation/硬编码导航）。
export const LEGACY_KEY_MAP = {
  dashboard: "home.dashboard",
  instances: "asset.center",
  sshAssets: "asset.center",
  cmdbAssets: "asset.center",
  "asset.instances": "asset.center",
  "asset.hosts": "asset.center",
  "asset.cmdb": "asset.center",
  inspectionOverview: "inspect.overview",
  inspectionCenter: "inspect.run",
  inspectionTasks: "inspect.tasks",
  inspectionReports: "inspect.reports",
  inspectionCompare: "inspect.compare",
  inspectionPolicies: "inspect.policies",
  inspectionTemplates: "inspect.templates",
  // 运维：四引擎旧 key 全部归一到能力 key（能力组 + 引擎自适应分发）。
  oracleOverview: "ops.overview",
  oracleSpace: "ops.space",
  oracleLogArchive: "ops.log",
  oracleSession: "ops.session",
  oracleObject: "ops.object",
  oracleHA: "ops.ha",
  oracleGovernance: "ops.governance",
  mysqlOverview: "ops.overview",
  mysqlStorage: "ops.space",
  mysqlInnoDBTx: "ops.session",
  mysqlUserSecurity: "ops.security",
  mysqlTableIndex: "ops.object",
  mysqlReplHA: "ops.ha",
  mysqlGovernance: "ops.governance",
  "ops.postgresql.overview": "ops.overview",
  "ops.postgresql.space": "ops.space",
  "ops.postgresql.wal": "ops.log",
  "ops.postgresql.sessionTransaction": "ops.session",
  "ops.postgresql.objectStats": "ops.object",
  "ops.postgresql.ha": "ops.ha",
  "ops.postgresql.governance": "ops.governance",
  "ops.sqlserver.overview": "ops.overview",
  "ops.sqlserver.space": "ops.space",
  "ops.sqlserver.logs": "ops.log",
  "ops.sqlserver.sessionTransaction": "ops.session",
  "ops.sqlserver.objectStats": "ops.object",
  "ops.sqlserver.ha": "ops.ha",
  "ops.sqlserver.governance": "ops.governance",
  // 备份 10→4 收敛：旧 key 全部归一到 总览/备份方案(plan)/恢复方案(restore)/报告与告警(report)。
  backupOverview: "backup.overview",
  backupPolicies: "backup.plan",
  backupTasks: "backup.plan",
  backupHistory: "backup.report",
  backupFiles: "backup.plan",
  backupRecoveryCenter: "backup.restore",
  backupValidation: "backup.plan",
  backupDrill: "backup.restore",
  backupAlerts: "backup.report",
  backupReports: "backup.report",
  "backup.policy": "backup.policy",
  "backup.task": "backup.plan",
  "backup.history": "backup.report",
  "backup.file": "backup.plan",
  "backup.validate": "backup.plan",
  "backup.drill": "backup.restore",
  "backup.alert": "backup.report",
  storageRescueOverview: "rescue.overview",
  storageRescueCases: "rescue.cases",
  storageRescueTools: "rescue.tools",
  storageRescueAgents: "rescue.agents",
  storageRescueTasks: "rescue.tasks",
  storageRescueReports: "rescue.reports",
  storageRescueApprovals: "rescue.approvals",
  automationCenter: "protection.automation",
  performanceOverview: "perf.overview",
  sqlProfile: "perf.sql",
  tableProfile: "perf.table",
  indexAnalysis: "perf.index",
  slowSqlAnalysis: "perf.slowsql",
  lockAnalysis: "perf.lock",
  perfBaseline: "perf.baseline",
  problemTrend: "perf.trend",
  capacity: "perf.capacity",
  optimizationCenter: "perf.optimization",
  databaseProfile: "perf.optimization",
  "perf.database": "perf.optimization",
  awrReport: "perf.awr.report",
  awrCompare: "perf.awr.compare",
  awrAutoCollect: "perf.awr.collect",
  schedule: "system.schedule",
  unifiedAlerts: "monitor.alert.unified",
  alertCenter: "monitor.alert.db",
  observabilityCenter: "monitor.observability",
  zabbixIntegration: "integration.zabbix",
  prometheusIntegration: "integration.prometheus",
  notifyManagement: "integration.notify",
  itsmManagement: "integration.itsm",
  "diagnose.ai": "diagnose.smart",
  "diagnose.rule": "diagnose.smart",
  ai: "diagnose.smart",
  diagnosisCenter: "diagnose.smart",
  ruleDiagnosis: "diagnose.smart",
  incidentCenter: "diagnose.incidents",
  incidentAgents: "diagnose.snapshot.agents",
  incidentSnapshotPolicies: "diagnose.snapshot.policies",
  aiConfig: "system.ai",
  unifiedReports: "report.unified",
  awrReports: "perf.awr.history",
  "report.awr": "perf.awr.history",
  alertReports: "report.alert",
  userManagement: "system.user",
  enterpriseIdentity: "system.identity",
  systemParams: "system.params",
};

// 新 key → 旧 key（用于后端 module_config 模块开关匹配）。
export const NEW_TO_LEGACY_KEY = Object.fromEntries(
  Object.entries(LEGACY_KEY_MAP).map(([oldKey, newKey]) => [newKey, oldKey]),
);

// 把任意（可能是旧的）key 归一为当前命名空间下的新 key。
export const normalizeMenuKey = (key) => LEGACY_KEY_MAP[key] || key;

export const getMenuItems = () => MENU_CONFIG;

export const findMenuLabel = (items, key) => {
  for (const item of items || []) {
    if (item.key === key) return item.label;
    const child = findMenuLabel(item.children, key);
    if (child) return child;
  }
  return null;
};

export const findMenuItem = (items, key, parentDbTypes) => {
  for (const item of items || []) {
    const dbTypes = item.dbTypes || parentDbTypes;
    if (item.key === key) return { item, dbTypes };
    if (item.children) {
      const found = findMenuItem(item.children, key, dbTypes);
      if (found) return found;
    }
  }
  return null;
};

// hiddenKeys 来自后端 module_config，仍以旧 key 表达；这里同时按新旧 key 命中。
const isHidden = (item, hiddenKeys) =>
  hiddenKeys.has(item.key) || hiddenKeys.has(NEW_TO_LEGACY_KEY[item.key]);

export const filterMenuItems = (items, hiddenKeys = new Set()) => items.map((item) => {
  if (item.type === "divider") return item;
  const { dbTypes, data, ...menuItem } = item;
  if (item.children) {
    const filteredChildren = filterMenuItems(item.children, hiddenKeys);
    if (filteredChildren.length === 0) return null;
    return { ...menuItem, children: filteredChildren };
  }
  if (isHidden(item, hiddenKeys)) return null;
  return menuItem;
}).filter(Boolean);

// 引擎自适应：选中实例后隐藏该引擎不支持的能力行（按 engines 字段），同时剥离 engines。
// 未选实例（selectedDbType 为空）时保留全部能力行，仅清理 engines 字段。
export const filterMenuByEngine = (items, selectedDbType) => items.map((item) => {
  const { engines, ...rest } = item;
  if (item.children) {
    const children = filterMenuByEngine(item.children, selectedDbType);
    if (children.length === 0) return null;
    return { ...rest, children };
  }
  if (engines && selectedDbType && !engines.includes(selectedDbType)) return null;
  return rest;
}).filter(Boolean);

export const collectMenuKeys = (items) => {
  const keys = new Set();
  const walk = (nodes) => {
    for (const item of nodes || []) {
      if (item.key) keys.add(item.key);
      if (item.children) walk(item.children);
    }
  };
  walk(items);
  return keys;
};
