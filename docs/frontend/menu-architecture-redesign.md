# 前端菜单架构重设计（面向扩展）

> 目标：跳出现有结构，给出命名清晰、模块边界互斥、且对未来扩展友好的信息架构（IA）。
> 配套现状梳理见 [`menu-routing.md`](./menu-routing.md)。

> **实施状态（已落地）**：第 8 节步骤 1–6 已实现并通过构建（65 个叶子全部可解析、
> legacy 映射全部有效、8 个运维能力均有引擎分发）。具体：
> - `menuConfig.jsx` 已配置化（新分组/命名/`data` 契约/`dbTypes`），新增 `LEGACY_KEY_MAP`
>   `NEW_TO_LEGACY_KEY` `normalizeMenuKey` `DEFAULT_OPEN_KEYS` `DEFAULT_ACTIVE_KEY`。
> - `AppPageRouter.jsx` 退化为「前缀路由 + 特殊页 switch + 标准注册表(`data` 契约)」单文件。
> - `AppRootState.js`/`AppRoot.jsx` 在 `setActiveKey` 与 smartNav 出口处归一旧→新 key；默认落地页
>   `diagnose.smart`。`AdminShell.jsx` 的 `cockpitMode`、tab 不可关闭判断同步到新 key。
> - 后端 `module_config` 仍以旧 key 表达，`filterMenuItems` 经 `NEW_TO_LEGACY_KEY` 双向命中，开关不受影响。
> - **第 5 步已落地**：`ops` 四引擎子树塌缩为 8 个能力行（运行概览/空间/日志/会话/对象/用户安全/
>   高可用/数据治理），由 `AppPageRouter` 的 `OPS_DISPATCH` 按选中实例 `db_type` 分发到 Oracle/MySQL
>   逐页组件或 PG/SS 中心页；`filterMenuByEngine` 在选中实例后隐藏该引擎不支持的能力行。
>   能力矩阵决策：MySQL 的「存储日志」归入 `space`（不单列 `log`）；`security`(用户安全) 仅 MySQL。
> - **已知小问题/未做**：
>   - 运维能力的后端模块开关粒度从「按引擎页」变为「按能力」，旧 per-engine module key 不再 1:1
>     命中能力行（能力行默认常显）；如需按能力开关，后端 `module_config` 增加 `ops.*` 能力 key 即可。
>   - 在某能力行已选实例后切换到不支持该能力的引擎，菜单项隐藏但内容区显示「暂不适用」空态（未做自动跳转）。
>   - 第 7 步「下线 LEGACY 兼容层」待深链/收藏稳定后再做。

---

## 1. 现状根因（一句话）

乱象来自**两套组织逻辑混用**：主体按「功能域」分组，但「运维」按**数据库引擎**分成 4 棵
平行子树，而横切能力（诊断 / 告警 / 报告 / 恢复）又各自散落在多个组里重复出现。
于是出现：两套 key 命名、四引擎子菜单不齐、三处「恢复/诊断/报告」、「中心」一词通胀。

## 2. 两条主设计原则（其余都由此推导）

### 原则 A：能力优先，引擎自适应（Capability-first）
把 Oracle / MySQL / PostgreSQL / SQLServer 四棵引擎子树**塌缩为一个「数据库运维」组**。
子页（空间 / 日志 / 会话 / 对象 / 高可用 / 治理…）与引擎无关，根据右上角选中实例的
`db_type` 自适应渲染对应面板。

- **扩展收益**：新增第 5 种数据库 = 扩 `dbTypes` 数组 + 补页面适配器，**不新增任何菜单子树**。
- 顺带消灭「两套 key 命名」「对象/空间在运维与性能间重复」两个老问题。
- 平台本就有右上角实例选择器 + `dbTypes` 过滤，天然契合这条路线。

### 原则 B：菜单数据化，路由退化为注册表（Config-driven）
菜单项从「只有 key/label/icon」升级为**声明式配置**，新增字段把现在散落在 router 里的隐式
契约显式化：

| 字段 | 作用 | 取代了今天的 |
|---|---|---|
| `dbTypes` | 引擎过滤 | 现有 dbTypes（保留） |
| `module` | 模块开关（启用/禁用） | disabledModules（保留） |
| `roles` | RBAC 可见性（未来） | 无 |
| `data` | 数据依赖契约：`none` / `list` / `instance` | **AppPageRouter 的 4 张 props 表 + 11 个特判** |
| `order` / `badge` | 排序、角标（beta/new） | 无 |

`data` 是关键：把页面「需不需要实例」写进配置后，router 变成**单一注册表查找** + 按 `data`
注入 props，不再有 4 张表和一长串 `activeKey === ...`。新增页 = 加一条数据，不碰路由代码。

## 3. 顶级结构（按运维生命周期排列）

> 叙事主线：接入 → 观测 → 体检 → 分析 → 处置 → 保护 → 沉淀 → 对接 → 管理

| # | 顶级组 | key 域 | 职责边界（与谁不重叠） |
|---|---|---|---|
| 0 | 工作台 | `home` | 全局总览、风险聚合；只读入口 |
| 1 | 资产管理 | `asset` | 实例/主机/凭据的台账与接入；不含任何分析 |
| 2 | 监控告警 | `monitor` | **实时**观测与告警；周期性体检归巡检、外部数据源归集成 |
| 3 | 巡检 | `inspect` | **周期性**健康体检与基线对比；产出巡检报告 |
| 4 | 性能 | `perf` | 性能指标的深度分析（SQL/索引/锁/AWR/容量/画像） |
| 5 | 智能诊断 | `diagnose` | AI/规则/端到端**根因**；消费 perf+inspect+monitor 数据 |
| 6 | 数据库运维 | `ops` | 变更与管理动作（空间/日志/会话/对象/高可用/治理…），引擎自适应 |
| 7 | 备份与恢复 | `backup` | **有计划的**备份与从备份还原 |
| 8 | 数据抢救 | `rescue` | **无备份的**误删/损坏应急抢救（取证式恢复） |
| 9 | 报告中心 | `report` | 跨模块报告的统一汇集/订阅/导出 |
| 10 | 集成中心 | `integration` | 对接外部系统：采集源、监控、通知、工单、生态 |
| 11 | 系统管理 | `system` | 平台自身配置：用户/权限/参数/AI 配置/调度 |

边界澄清三处老冲突：
- **诊断**：`perf`（性能问题分析）≠ `diagnose`（跨域根因，AI/规则）≠ `inspect`（周期体检）。三者按"手段+时机"互斥。
- **恢复**：`backup`（有备份→还原）≠ `rescue`（无备份→抢救）。两组都不再叫「恢复中心」。
- **报告/集成**：报告统一进 `report`；外部数据源/通知/工单从监控里拆到 `integration`，`monitor` 只留告警与可观测。

## 4. 完整菜单树 + 新旧 key 对照

> `data` 列：`none`=无需实例 / `list`=只用实例列表 / `instance`=需选中实例。`engines` 留空=全引擎。

### 0. 工作台 `home`
| 新 key | 标签 | data | 旧 key |
|---|---|---|---|
| `home.dashboard` | 工作台总览 | list | `dashboard` |

### 1. 资产管理 `asset`
| 新 key | 标签 | data | 旧 key |
|---|---|---|---|
| `asset.instances` | 数据库实例 | list | `instances` |
| `asset.hosts` | 主机 / SSH 资产 | none | `sshAssets` |
| `asset.credentials` *(预留)* | 凭据管理 | none | — |

### 2. 监控告警 `monitor`
| 新 key | 标签 | data | 旧 key |
|---|---|---|---|
| `monitor.alert.unified` | 综合告警 | list | `unifiedAlerts` |
| `monitor.alert.db` | 数据库告警 | instance | `alertCenter` |
| `monitor.observability` | 可观测监控 | none | `observabilityCenter` |

### 3. 巡检 `inspect`
| 新 key | 标签 | data | 旧 key |
|---|---|---|---|
| `inspect.overview` | 巡检总览 | instance | `inspectionOverview` |
| `inspect.run` | 一键巡检 | instance | `inspectionCenter` |
| `inspect.tasks` | 巡检任务 | instance | `inspectionTasks` |
| `inspect.reports` | 巡检报告 | none | `inspectionReports` |
| `inspect.compare` | 历史对比 | instance | `inspectionCompare` |
| `inspect.policies` | 巡检策略 | instance | `inspectionPolicies` |
| `inspect.templates` | 巡检模板 | instance | `inspectionTemplates` |

### 4. 性能 `perf`
| 新 key | 标签 | data | engines | 旧 key |
|---|---|---|---|---|
| `perf.overview` | 性能总览 | list | | `performanceOverview` |
| `perf.sql` | SQL 画像 | instance | | `sqlProfile` |
| `perf.slowsql` | 慢 SQL 分析 | instance | | `slowSqlAnalysis` |
| `perf.lock` | 锁等待分析 | instance | | `lockAnalysis` |
| `perf.index` | 索引分析 | instance | | `indexAnalysis` |
| `perf.table` | 表画像 | instance | | `tableProfile` |
| `perf.database` | 数据库画像 | instance | | `databaseProfile` |
| `perf.baseline` | 性能基线 | instance | | `perfBaseline` |
| `perf.trend` | 问题趋势 | instance | | `problemTrend` |
| `perf.capacity` | 容量分析 | instance | oracle | `capacity` |
| `perf.optimization` | 优化建议 | instance | | `optimizationCenter` |
| `perf.awr.report` | AWR 综合 | instance | oracle | `awrReport` |
| `perf.awr.compare` | AWR 对比 | instance | oracle | `awrCompare` |
| `perf.awr.collect` | AWR 自动采集 | list | oracle | `awrAutoCollect` |

### 5. 智能诊断 `diagnose`
| 新 key | 标签 | data | 旧 key |
|---|---|---|---|
| `diagnose.smart` | 端到端智能诊断 | instance | `diagnosisCenter` |
| `diagnose.ai` | AI 诊断助手 | instance | `ai` |
| `diagnose.rule` | 规则诊断 | instance | `ruleDiagnosis` |

> 默认落地页改为 `diagnose.smart`（沿用现默认），且它现在位于一个职责清晰的组里，而非埋在 AI 杂项中。

### 6. 数据库运维 `ops`（引擎自适应，单组取代四子树）
| 新 key | 标签 | data | engines | 旧 key（按引擎合并） |
|---|---|---|---|---|
| `ops.overview` | 运行概览 | instance | 全 | oracleOverview / mysqlOverview / ops.{pg,ss}.overview |
| `ops.space` | 空间管理 | instance | 全 | oracleSpace / ops.{pg,ss}.space / (mysqlStorage 拆分) |
| `ops.log` | 日志管理 | instance | 全 | oracleLogArchive / ops.pg.wal / ops.ss.logs / (mysql binlog) |
| `ops.session` | 会话与事务 | instance | 全 | oracleSession / mysqlInnoDBTx / ops.{pg,ss}.sessionTransaction |
| `ops.object` | 对象管理 | instance | 全 | oracleObject / mysqlTableIndex / ops.{pg,ss}.objectStats |
| `ops.security` | 用户与权限 | instance | 全 | mysqlUserSecurity / (Oracle 用户管理页) |
| `ops.parameter` | 参数管理 | instance | 全 | (Oracle 参数页等) |
| `ops.ha` | 高可用 | instance | 全 | oracleHA / mysqlReplHA / ops.{pg,ss}.ha |
| `ops.governance` | 数据治理 | instance | 全 | oracleGovernance / mysqlGovernance / ops.{pg,ss}.governance |

> 引擎差异由**页面内部**按 `db_type` 切换面板（如 `ops.log` 在 Oracle 显示归档日志、MySQL 显示 binlog、
> PG 显示 WAL）。MySQL 现有的「存储日志」合并项拆回 `ops.space` + `ops.log` 两个标准能力。

### 7. 备份与恢复 `backup`
| 新 key | 标签 | data | 旧 key |
|---|---|---|---|
| `backup.overview` | 备份总览 | instance | `backupOverview` |
| `backup.policy` | 备份策略 | instance | `backupPolicies` |
| `backup.task` | 备份任务 | instance | `backupTasks` |
| `backup.history` | 备份历史 | instance | `backupHistory` |
| `backup.file` | 备份文件 | instance | `backupFiles` |
| `backup.restore` | 备份还原 | instance | `backupRecoveryCenter` |
| `backup.validate` | 备份验证 | instance | `backupValidation` |
| `backup.drill` | 恢复演练 | instance | `backupDrill` |
| `backup.alert` | 备份告警 | instance | `backupAlerts` |
| `backup.report` | 备份报告 | instance | `backupReports` |

### 8. 数据抢救 `rescue`（原「数据库恢复中心」）
| 新 key | 标签 | data | 旧 key |
|---|---|---|---|
| `rescue.overview` | 抢救总览 | none | `storageRescueOverview` |
| `rescue.agents` | 远程 Agent | none | `storageRescueAgents` |
| `rescue.tasks` | 抢救任务 | none | `storageRescueTasks` |
| `rescue.reports` | 抢救报告 | none | `storageRescueReports` |
| `rescue.approvals` | 风险审批 | none | `storageRescueApprovals` |

### 9. 报告中心 `report`
| 新 key | 标签 | data | engines | 旧 key |
|---|---|---|---|---|
| `report.unified` | 统一报告 | none | | `unifiedReports` |
| `report.awr` | AWR 报告 | instance | oracle | `awrReports` |
| `report.alert` | 告警报告 | none | | `alertReports` |

> 巡检/备份/抢救报告仍在各自模块内可达；报告中心通过**报告注册表**把它们聚合为统一检索/订阅入口。

### 10. 集成中心 `integration`
| 新 key | 标签 | data | 旧 key |
|---|---|---|---|
| `integration.zabbix` | Zabbix 联动 | list | `zabbixIntegration` |
| `integration.prometheus` | Prometheus 告警 | list | `prometheusIntegration` |
| `integration.notify` | 通知渠道 | none | `notifyManagement` |
| `integration.itsm` | 工单（ITSM） | none | `itsmManagement` |

> T-051 至 T-060 完成后，跨域“生态适配”汇总页已退役；各能力由通知、工单、CMDB、可观测、备份、数据治理、自动化审批和企业认证页面直接承载。

### 11. 系统管理 `system`
| 新 key | 标签 | data | 旧 key |
|---|---|---|---|
| `system.user` | 用户管理 | none | `userManagement` |
| `system.params` | 系统参数 | none | `systemParams` |
| `system.ai` | AI 配置 | none | `aiConfig` |
| `system.schedule` | 调度任务 | list | `schedule` |
| `system.role` *(预留)* | 角色权限 | none | — |
| `system.module` *(预留)* | 模块开关 | none | — |
| `system.license` *(预留)* | 许可证 | none | — |

> 全量覆盖核对：现有 70+ 叶子节点逐一映射，无遗漏（`schedule` 归入 `system.schedule`）。

## 5. 命名规约（停止「中心」通胀）

1. **顶级组**：2–4 字领域名词；「中心」只保留给真正的聚合枢纽（报告中心 / 集成中心），其余去掉。
2. **子项**：具体名词短语，不再叠加「中心」（SQL 画像中心 → SQL 画像；优化建议中心 → 优化建议）。
3. **同义动词唯一化**：「恢复」只用于 `backup.restore`；抢救用「抢救」；体检用「巡检」；根因用「诊断」。
4. **标签即职责**：用户看标签就能判断归属，无需点进去试。

## 6. key 命名空间规约（稳定、可推导）

- 统一格式：`<domain>.<module>[.<page>]`，全小写、点号分隔。
- 一个 key 的**前缀即归属**：`ops.*` 必属运维、`perf.awr.*` 必属性能-AWR。router 可按前缀批量路由。
- 废弃现有「扁平驼峰 vs 点号」两套混用；旧 key 经第 8 节映射表一次性迁移。
- key 一旦发布即视为契约：改组只调 `label`，**不轻易改 key**（避免破坏埋点/收藏/深链）。

## 7. 扩展模型（未来怎么加东西，都不动结构）

| 扩展场景 | 操作 | 是否动菜单结构 |
|---|---|---|
| 新增数据库引擎（如 DB2/达梦） | 给相关 `ops.*` 项的 `dbTypes` 追加引擎 + 写页面适配器 | **否** |
| 新增运维能力（如「连接池管理」） | `ops` 下加一行 `ops.pool`，自动对所有支持引擎生效 | 仅加 1 行 |
| 新增诊断类型 | `diagnose` 或 `perf` 下加子项 | 仅加 1 行 |
| 新增外部集成 | `integration` 下加子项 | 仅加 1 行 |
| 新增报告类型 | 向报告注册表注册，自动出现在报告中心 | **否** |
| 引入 RBAC | 给菜单项填 `roles`，过滤器按角色裁剪 | **否** |
| 模块灰度/Beta | 填 `module`（开关）/ `badge: "beta"` | **否** |

## 8. 迁移步骤（落地时执行，分阶段、可回滚）

1. **配置化 menuConfig**：把 `MENU_CONFIG` 升级为带 `dbTypes/module/roles/data/order` 的声明式数据。
2. **建组件注册表**：`COMPONENT_REGISTRY[newKey] = Component`，替代 AppPageRouter 的 4 张表 + 特判。
3. **router 退化**：`renderRoute(activeKey)` = 查注册表 + 按 `data` 注入 props（none/list/instance）。
4. **旧→新 key 映射层**：加 `LEGACY_KEY_MAP`（第 4 节对照表），兼容旧深链/收藏，过渡期双向解析。
5. **运维四子树合一**：把 4 个引擎页改造成按 `db_type` 自适应的 9 个能力页（工作量最大，可最后做）。
6. **默认落地页**：`activeKey` 默认值改 `diagnose.smart`。
7. **下线兼容层**：稳定后移除 `LEGACY_KEY_MAP`。

> 1–4、6 为低风险纯结构调整；5 涉及页面合并，建议单独立项并逐引擎验证。

## 9. 与旧设计的关键差异小结

- 运维：**4 引擎子树 → 1 能力组**（扩展性核心）。
- 路由：**11 特判 + 4 props 表 → 注册表 + `data` 声明**。
- 命名：去「中心」通胀、同义动词唯一化、标签即职责。
- 边界：诊断/恢复/报告/集成 四处重叠各自归位，顶级组 MECE。
- key：**单一 `<域>.<模块>.<页>` 命名空间**，前缀即归属。
