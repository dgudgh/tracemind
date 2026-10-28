# 前端菜单与路由梳理（activeKey 内存路由）

> 现状结论：前端**没有浏览器 URL 路由**（无 react-router）。导航是一套纯内存的 `activeKey`
> 状态机；URL 仅用于登录 hash（`#/login`）和一次性消费 `?token=`。本文档只梳理结构，不改代码。

## 1. 导航链路

```
menuConfig.jsx (MENU_CONFIG 菜单树 + key)
        │  点击菜单项 → onMenuClick(key)
        ▼
AppRoot.jsx → OperationsShell(onMenuClick=setActiveKey)
        │
        ▼
AppRootState.js  持有 activeKey（默认 "diagnosisCenter"）
        │  activeKey 传入
        ▼
AppPageRouter.jsx  activeKey → 页面组件
```

- 菜单点击只改 `activeKey`，不动 URL；刷新页面会丢失当前位置（回到默认 `diagnosisCenter`）。
- 相关文件：`menuConfig.jsx`、`AppRootState.js`、`AppRoot.jsx`、`AppPageRouter.jsx`、`OpsLayout.jsx`。

## 2. AppPageRouter 的解析机制（两套混合）

### 2a. 特判 / 前缀 / 集合分支（按代码顺序优先匹配）

| 判定 | 目标组件 | 注入 props |
|---|---|---|
| `activeKey === "dashboard"` | EditionDashboard | instances, capacityAlerts, onNavigate |
| `BACKUP_CENTER_KEYS.includes` | BackupRecoveryCenterPage | activeKey, instances, selectedInstanceId |
| `STORAGE_RESCUE_KEYS.includes` | StorageRescuePage | activeKey, onNavigate |
| `startsWith("ops.postgresql.")` | PostgreSQLOperationsCenterPage | activeKey, instanceId, instances |
| `startsWith("ops.sqlserver.")` | SQLServerOperationsCenterPage | activeKey, instanceId, instances |
| `=== "instances"` | InstancePage | instances, loading, onChanged, engine |
| `=== "slowSqlAnalysis"` | SlowSqlPage | instances, engine, selectedInstanceId |
| `=== "lockAnalysis"` | LockAnalysisPage | instanceId, instances, engine |
| `=== "awrReport"` | OracleAwrReportPage | instanceId |
| `=== "performanceOverview"` | PerformanceOverviewPage | instances, onNavigate |
| `=== "ai"` | AiPage | instances, engine, selectedInstanceId |

### 2b. 四张路由表（落到 renderRoute，props 契约各不相同）

| 路由表 | props 契约 | 含义 |
|---|---|---|
| `STATIC_ROUTES` | `{activeKey, onNavigate}` | 不需要实例 |
| `INSTANCE_ID_ROUTES` | `{instanceId, instances}` | 需要 header 选中实例 |
| `SELECTED_INSTANCE_ROUTES` | `{instances, selectedInstanceId, onNavigate}` | 需要 header 选中实例 + 可导航 |
| `INSTANCES_ONLY_ROUTES` | `{instances}` | 只要实例列表，不挑单个 |

## 3. 完整对照表：菜单 → key → 组件 → 数据依赖

> 「依赖」列：`—`=无需实例 / `header`=需右上角选中实例 / `list`=只用实例列表 / `dbTypes`=按引擎过滤

| 分组 | 菜单项 | key | 解析途径 | 组件 | 依赖 |
|---|---|---|---|---|---|
| 首页 | 首页总览 | `dashboard` | 特判 | EditionDashboard | list |
| 资产与连接 | 数据库实例 | `instances` | 特判 | InstancePage | list |
| 资产与连接 | SSH 资产 | `sshAssets` | STATIC | SshAssetsPage | — |
| 巡检中心 | 巡检总览 | `inspectionOverview` | SELECTED | InspectionOverviewPage | header |
| 巡检中心 | 一键巡检 | `inspectionCenter` | SELECTED | InspectionCenterPage | header |
| 巡检中心 | 巡检任务 | `inspectionTasks` | SELECTED | InspectionTaskPage | header |
| 巡检中心 | 巡检报告 | `inspectionReports` | STATIC | InspectionReportPage | — |
| 巡检中心 | 历史对比 | `inspectionCompare` | SELECTED | InspectionHistoryComparePage | header |
| 巡检中心 | 巡检策略 | `inspectionPolicies` | SELECTED | InspectionPolicyPage | header |
| 巡检中心 | 巡检模板 | `inspectionTemplates` | SELECTED | InspectionTemplatePage | header |
| 运维·Oracle | 运行概览 | `oracleOverview` | INSTANCE_ID | OracleOverviewPage | header, dbTypes=oracle |
| 运维·Oracle | 空间管理 | `oracleSpace` | INSTANCE_ID | OracleSpacePage | header, oracle |
| 运维·Oracle | 日志归档 | `oracleLogArchive` | INSTANCE_ID | OracleLogArchivePage | header, oracle |
| 运维·Oracle | 会话事务 | `oracleSession` | INSTANCE_ID | OracleSessionPage | header, oracle |
| 运维·Oracle | 对象统计 | `oracleObject` | INSTANCE_ID | OracleObjectPage | header, oracle |
| 运维·Oracle | 高可用 | `oracleHA` | INSTANCE_ID | OracleHAPage | header, oracle |
| 运维·Oracle | 数据治理 | `oracleGovernance` | INSTANCE_ID | OracleGovernancePage | header, oracle |
| 运维·MySQL | 运行概览 | `mysqlOverview` | INSTANCE_ID | MySQLOverviewPage | header, mysql |
| 运维·MySQL | 存储日志 | `mysqlStorage` | INSTANCE_ID | MySQLStoragePage | header, mysql |
| 运维·MySQL | InnoDB 事务 | `mysqlInnoDBTx` | INSTANCE_ID | MySQLInnoDBTxPage | header, mysql |
| 运维·MySQL | 用户安全 | `mysqlUserSecurity` | INSTANCE_ID | MySQLUserSecurityPage | header, mysql |
| 运维·MySQL | 表与索引 | `mysqlTableIndex` | INSTANCE_ID | MySQLTableIndexPage | header, mysql |
| 运维·MySQL | 复制高可用 | `mysqlReplHA` | INSTANCE_ID | MySQLReplHAPage | header, mysql |
| 运维·MySQL | 数据治理 | `mysqlGovernance` | INSTANCE_ID | MySQLGovernancePage | header, mysql |
| 运维·PostgreSQL | 运行概览 | `ops.postgresql.overview` | 前缀 | PostgreSQLOperationsCenterPage | header, postgresql |
| 运维·PostgreSQL | 空间管理 | `ops.postgresql.space` | 前缀 | 〃 | header, postgresql |
| 运维·PostgreSQL | WAL 日志 | `ops.postgresql.wal` | 前缀 | 〃 | header, postgresql |
| 运维·PostgreSQL | 会话事务 | `ops.postgresql.sessionTransaction` | 前缀 | 〃 | header, postgresql |
| 运维·PostgreSQL | 对象统计 | `ops.postgresql.objectStats` | 前缀 | 〃 | header, postgresql |
| 运维·PostgreSQL | 高可用 | `ops.postgresql.ha` | 前缀 | 〃 | header, postgresql |
| 运维·PostgreSQL | 数据治理 | `ops.postgresql.governance` | 前缀 | 〃 | header, postgresql |
| 运维·SQLServer | 运行概览 | `ops.sqlserver.overview` | 前缀 | SQLServerOperationsCenterPage | header, sqlserver |
| 运维·SQLServer | 空间管理 | `ops.sqlserver.space` | 前缀 | 〃 | header, sqlserver |
| 运维·SQLServer | 日志管理 | `ops.sqlserver.logs` | 前缀 | 〃 | header, sqlserver |
| 运维·SQLServer | 会话事务 | `ops.sqlserver.sessionTransaction` | 前缀 | 〃 | header, sqlserver |
| 运维·SQLServer | 对象统计 | `ops.sqlserver.objectStats` | 前缀 | 〃 | header, sqlserver |
| 运维·SQLServer | 高可用 | `ops.sqlserver.ha` | 前缀 | 〃 | header, sqlserver |
| 运维·SQLServer | 数据治理 | `ops.sqlserver.governance` | 前缀 | 〃 | header, sqlserver |
| 备份恢复中心 | 备份总览/策略/任务/历史/文件/恢复中心/验证/演练/告警/报告 | `backup*`（10 项） | 集合 | BackupRecoveryCenterPage | header |
| 数据库恢复中心 | 恢复总览/远程Agent/恢复任务/恢复报告/风险审批 | `storageRescue*`（5 项） | 集合 | StorageRescuePage | — |
| 性能诊断 | 性能总览 | `performanceOverview` | 特判 | PerformanceOverviewPage | list |
| 性能诊断 | SQL 画像中心 | `sqlProfile` | SELECTED | SQLProfileCenterPage | header |
| 性能诊断 | 表画像中心 | `tableProfile` | SELECTED | TableProfilePage | header |
| 性能诊断 | 索引分析中心 | `indexAnalysis` | SELECTED | IndexAnalysisPage | header |
| 性能诊断 | 慢 SQL 分析 | `slowSqlAnalysis` | 特判 | SlowSqlPage | header |
| 性能诊断 | 锁等待分析 | `lockAnalysis` | 特判 | LockAnalysisPage | header |
| 性能诊断 | 性能基线 | `perfBaseline` | SELECTED | PerfBaselinePage | header |
| 性能诊断 | 问题趋势 | `problemTrend` | SELECTED | ProblemTrendPage | header |
| 性能诊断 | 容量分析 | `capacity` | SELECTED | CapacityPage | header, dbTypes=oracle |
| 性能诊断 | 优化建议中心 | `optimizationCenter` | SELECTED | OptimizationCenterPage | header |
| 性能诊断 | 数据库画像 | `databaseProfile` | SELECTED | DatabaseProfilePage | header |
| 性能·AWR | AWR 综合 | `awrReport` | 特判 | OracleAwrReportPage | header, oracle |
| 性能·AWR | AWR 对比 | `awrCompare` | SELECTED | AwrComparePage | header, oracle |
| 性能·AWR | AWR 自动采集 | `awrAutoCollect` | INSTANCES_ONLY | AwrAutoCollectPage | list, oracle |
| 性能诊断 | 定时任务 | `schedule` | INSTANCES_ONLY | SchedulePage | list |
| 监控告警 | 综合告警 | `unifiedAlerts` | INSTANCES_ONLY | UnifiedAlertEventPage | list |
| 监控告警 | 数据库告警 | `alertCenter` | SELECTED | AlertCenterPage | header |
| 监控告警 | 可观测与采集中心 | `observabilityCenter` | STATIC | ObservabilityCenterPage | — |
| 监控告警 | Zabbix 联动 | `zabbixIntegration` | INSTANCES_ONLY | ZabbixIntegrationPage | list |
| 监控告警 | Prometheus 告警 | `prometheusIntegration` | INSTANCES_ONLY | PrometheusIntegrationPage | list |
| 监控告警 | 通知管理 | `notifyManagement` | STATIC | NotifyManagementPage | — |
| 监控告警 | 工单管理 | `itsmManagement` | STATIC | ItsmManagementPage | — |
| AI 智能诊断 | AI 诊断 | `ai` | 特判 | AiPage | header |
| AI 智能诊断 | 端到端智能诊断中心 | `diagnosisCenter` | SELECTED | DiagnosisCenterPage | header |
| AI 智能诊断 | 规则诊断中心 | `ruleDiagnosis` | SELECTED | RuleDiagnosisPage | header |
| AI 智能诊断 | AI 配置 | `aiConfig` | STATIC | AiConfigPage | — |
| 报告中心 | 统一报告中心 | `unifiedReports` | STATIC | UnifiedReportCenterPage | — |
| 报告中心 | AWR 分析报告 | `awrReports` | SELECTED | AwrAnalysisHistoryPage | header, oracle |
| 报告中心 | 告警报告 | `alertReports` | STATIC | AlertReportsPage | — |
| 系统设置 | 用户管理 | `userManagement` | STATIC | UserManagementPage | — |
| 系统设置 | 系统参数 | `systemParams` | STATIC | SystemParamsPage | — |

## 4. 发现的问题（按严重度）

### P1 — key 命名两套风格并存（核心「路径」问题）
- Oracle / MySQL 用扁平驼峰：`oracleOverview`、`mysqlStorage`
- PostgreSQL / SQL Server 用点号命名空间：`ops.postgresql.overview`
- 后果：同层级（DB 运维子页）两套 key 体系，router 里 PG/SQLServer 走 `startsWith` 前缀分支、
  Oracle/MySQL 走 `INSTANCE_ID_ROUTES` 扁平表——**四引擎、两条路由通道**。

### P1 — 四引擎运维子菜单语义平行但结构不齐
| | 概览 | 空间 | 日志 | 会话 | 对象 | 高可用 | 治理 | 其它 |
|---|---|---|---|---|---|---|---|---|
| Oracle | ✓ | 空间管理 | 日志归档 | 会话事务 | 对象统计 | ✓ | ✓ | — |
| PostgreSQL | ✓ | 空间管理 | WAL 日志 | 会话事务 | 对象统计 | ✓ | ✓ | — |
| SQLServer | ✓ | 空间管理 | 日志管理 | 会话事务 | 对象统计 | ✓ | ✓ | — |
| **MySQL** | ✓ | **存储日志（合并）** | ↤合并 | **InnoDB 事务** | **表与索引** | 复制高可用 | ✓ | **用户安全** |

MySQL 自成一套，与其余三引擎不对齐。

### P2 — 三个「恢复中心」概念打架
- 顶级 `g_backup`「备份恢复中心」
- 顶级 `g_storage_rescue`「数据库恢复中心」
- `g_backup` 子项 `backupRecoveryCenter`「恢复中心」

命名高度重叠，用户难分辨。

### P2 — 落地页与菜单首项不一致
默认 `activeKey = "diagnosisCenter"`（埋在第 7 组 g_ai 下），菜单第一项却是 `dashboard`。
刷新后总是回到 diagnosisCenter 而非用户上次位置（无 URL 持久化）。

### P3 — AWR 散落 3 处
`g_performance > g_awr`、`g_reports > awrReports`、以及 Oracle 运维语境，入口分散。

### P3 — STATIC_ROUTES 中的 storageRescue* 是死代码
`storageRescueOverview/Agents/Tasks/Reports/Approvals` 同时出现在 `STORAGE_RESCUE_KEYS` 集合判断
（先命中）和 `STATIC_ROUTES` 表里，后者永远不会被执行到，应删除。

### P3 — 新增页面无契约可循
一个 key 该进哪张路由表、注入哪套 props，全凭记忆，没有文档化规则（本表即补此空缺）。

## 5. 后续改造建议（待决策，本次不实施）

1. **统一 key 命名**：全部改成 `ops.<engine>.<page>` 或全部扁平，二选一；随后把 router 的
   前缀分支与 INSTANCE_ID 表合并为单一「引擎运维」通道。
2. **对齐四引擎子菜单**：以 Oracle/PG/SQLServer 的 7 项模板为基线，评估 MySQL 是否归一
   （或显式说明 MySQL 为何不同）。
3. **重命名恢复类分组**：让「备份恢复中心」与「数据库恢复中心（存储抢救）」名称区分清晰，
   并消除子项「恢复中心」与顶级名的重复。
4. **路由表契约文档化**：四张表的 props 语义（依赖列）以本表为准，新增页对号入座。
5. （可选，已决定暂不做）引入 hash/history URL 路由以支持刷新保留与分享。
