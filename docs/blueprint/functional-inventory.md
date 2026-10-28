# 全功能源码盘点

核对日期：2026-10-20。项目：`mesdbtools`。用途：回答“现有功能有哪些、真实做到哪一步、哪些可以复用为功能卡片”，为[详细待开发清单](pending-development-backlog.md)和[编排实施计划 C01-C16](capability-card-task-plan.md)提供依据。

## 1. 基线与证据边界

- 读取当前工作区，分支 `codex/menu-optimization`，核对时 HEAD `a904301`；大量并行改动尚未提交，HEAD 不能代表本次盘点的全部源码。未暂存、还原、移动或删除这些改动。
- 覆盖 F01-F26 的主要操作族，检查前端路由/请求、API、服务或 Agent、存储模型及回读入口；F27 MES 交互仍暂缓。操作族不是按钮数，也不是每一条 SQL、每一行代码或所有运行分支的完整审计。
- 目录入口参考 [API 注册表](../../backend/app/api/v1/registry.py)、[页面路由](../../frontend/src/AppPageRouter.jsx)、[菜单基线](../../contracts/baselines/frontend-menu-v1.json)。菜单契约校验通过，当前有 72 个叶子入口；有入口不等于有执行器、权限正确或已经验收。
- 本轮仅阅读源码和维护文档，没有修改业务代码、API、表结构或视觉；没有连接现场数据库、模型、Agent、HA、通知/工单或对象存储，没有采集、迁移、重启、部署、故障注入或长稳测试。
- 没有逐路由验证认证、动作权限与资产范围的完整组合。已有鉴权设施应复用，但不能据文件存在声称所有操作已满足最小权限。具体运行验收见待开发清单的 V01-V06。

状态词：**已有路径**表示读到了所列调用/结果链；**部分/未接线**表示已有组件但缺闭环；**仅方案**不表示真实执行；**外置/CLI**是既定边界；**待实测**不等于缺代码。以下全部属于源码证据，不作线上完成率。

## 2. 当前总体判断

1. 已有资产、巡检、性能、备份、诊断、快照、审批与报告代码，应按具体操作复用，不应重写成第二套 AI 执行系统。
2. 统一功能卡片、类型化任务清单、服务端计划校验/冻结、跨模块持久批次和全链控制权尚未形成。15 条运维能力描述与 72 个菜单入口都不是可直接执行的统一卡片注册表。
3. 小助手不只是导航：[assistantActions.js](../../frontend/src/modules/assistantActions.js) 已直接调用多类查询/采集；但按页面 key 和前端条件分派，进度多在浏览器，未统一绑定任务、证据、报告和可恢复结果。只有部分路径显式传递 `executionResult`，不能宣称所有功能“打开功能”都会还原刚才结果。
4. 手动、定时和若干专业页巡检已接持久作业设施，但 `jobs.submit` 仍在请求中领取并执行到终态；小助手全库巡检仍是浏览器串行循环。持久作业行不等于独立后台 worker 或持久批次。
5. 另有 `diagnosis/orchestrator.py::run_diagnosis` 直接调用 `run_inspection`，不能把“所有巡检入口均已队列化”写成当前事实。
6. 恢复、HA、会话写动作、深度采集等支持范围并不一致。明确阻断、仅生成方案和外部执行器未交付不能显示成“执行成功”。

## 3. 操作族与调用链

表中按操作族归并相同链路，不重复列每个按钮。文件中的符号名用于复查，避免把易漂移行号当作唯一证据；待开发编号统一指向[详细清单](pending-development-backlog.md)。

### 3.1 工作台、资产、监控与企业集成

完整 30 个操作族的前端/API/服务/模型/回读证据及失败边界见[分项 A](../archive/acceptance/2026-10-20-functional-inventory-a.md#4-操作族矩阵30-行)。下表归并导航，不将同一页面当成单一权限能力。

| 蓝图/操作族 | 已核对路径与结果 | 当前缺口/待办 |
| --- | --- | --- |
| F01 KPI/SLO/风险、个人待办/下钻、关闭容量告警（A01-A02） | [EditionDashboard](../../frontend/src/DatabaseAggregatePages.jsx) → `/system/dashboard-stats` → [operations_dashboard](../../backend/app/services/operations_dashboard.py)，聚合已存告警/巡检/审批等，非实时重采 | 缺环境/组织/负责人筛选与部分查询下推；下钻丢实体 ID，关闭容量后清零综合告警计数不成立。M01；ISS-43。 |
| F02 五层清单/树/详情/关系拓扑、校正/合并/拆分（A03/A06） | [CmdbAssetPage](../../frontend/src/CmdbAssetPage.jsx) → [CMDB API](../../backend/app/cmdb/api.py) → [normalized_service](../../backend/app/cmdb/normalized_service.py)/[AssetTopology](../../frontend/src/AssetTopology.js)；存储关系/校正前后值及审计已有 | 前端已使用保存关系，不再列“未核对消费”；复杂混合拓扑/并发校正/级联待 V01，受限用户附加管理请求失败见 M03。 |
| F02 外部源/导入/同步批次、发现预览/真实探测（A04-A05） | [cmdb/service.py](../../backend/app/cmdb/service.py) `upsert_assets`、`apply_internal_discovery`；源/资产/批次与规范化投影；发现探测配置库和 SSH 主机，不是网段扫描 | JSON 导入不等于根据 endpoint 主动拉取；源同步/投影分段提交，范围与新增成员预算未形成统一冻结批次。M02/U06-U08。 |
| F03 连接 CRUD/停用/退役、未保存/强制实时测试、主机概览（A07-A08） | [InstanceDrawer](../../frontend/src/InstanceDrawer.jsx) → [instances API](../../backend/app/api/v1/instances.py) → [instances service](../../backend/app/services/instances.py)/四驱动，DBInstance/凭据审计/资产状态 | 已有真实测试，不是只读旧值；连接通不证明全部权限/架构正确，资产状态写失败可能仅日志告警；四引擎版本/TLS/超时矩阵待 V01，动作/范围接线 M03。 |
| F03/F26 凭据封装/替换/重包、证书绑定（A09） | 受控秘密引用和凭据/证书审计已有；MySQL/PG TLS、Oracle Wallet、SQL Server encryption 接线可见 | 不是远端自动改密码；SQL Server 证书能力仍显式受限，`trust_server_certificate` 旧语义需核对，不沿用“所有字段均显式拒绝”的宽结论。M03/M32、V01。 |
| F07 容量采集/日快照、摘要/趋势/预测、策略/持续触发/滞回（A10-A12） | [capacity_monitor](../../backend/app/services/capacity_monitor.py)、[capacity_scheduler](../../backend/app/tasks/capacity_scheduler.py) → 对象日快照/容量策略/告警；已有持续时间和恢复滞回 | GET 可隐式实时 IO/创建策略；手工导入可能绕持续发布条件；UI 未接持续/滞回，日统计不是每次运行历史。M07/U06；ISS-25 是局部实现，不整体推翻。 |
| F07/F10 性能指标/基线/保留（A13） | [perf_baseline](../../backend/app/services/perf_baseline.py) → DBPerfMetric/Policy，周期消费者和保留已有 | 当前值缺新鲜度、零项失败提示和累计/速率单位需纠正；不是缺周期采集。M41。 |
| F08 接入归并/生命周期/维护/SLA、动作台账/回报/重排、规则、报表/证据/诊断（A14-A17） | [alert_events API](../../backend/app/api/v1/alert_events.py)、[alert_lifecycle](../../backend/app/services/alert_lifecycle.py)、[alert_rules](../../backend/app/services/alert_rules.py)；Event/ActionLog/Lifecycle/SourceLink/规则有存储/读取 | 动作 pending 无真实领取执行闭环；规则启停不进入容量判定；SLA 主要由读取推进；来源恢复/报告覆盖需统一。M08；ISS-24。 |
| F09 源/绑定/映射/联动健康、采集/周期任务、新旧监控接入（A18-A20） | [ObservabilityCenterSections](../../frontend/src/ObservabilityCenterSections.jsx) → [observability API](../../backend/app/api/v1/observability.py)/[collector_runner](../../backend/app/services/observability/collector_runner.py)/周期任务；Source/Binding/Metric/CollectTask/Log 已写 | 预算/禁用源复查不完整，partial 被任务写 success；旧 Zabbix/Prometheus 配置不自动成为新源；采集仍同步，部分编辑 API 未接界面。M09/U06-U08。 |
| F09 日志/时间线/APM/SQL 证据（A21-A22） | [log_query](../../backend/app/services/observability/log_query.py)、[apm_adapters](../../backend/app/services/observability/apm_adapters.py)；日志四类适配，实时 Trace 查询目前 Jaeger/Zipkin；QueryAudit 存摘要 | 助手缺源和起止参数；时间线主要排序所传事件，不是自动跨源复原；SkyWalking/OTel 解析不等于实时查询。原文未全面存储是边界，最小结果回读待补。M09/U08。 |
| F09 质量/历史/保留、指标、Grafana/Exporter（A23-A24） | [data_quality_service](../../backend/app/services/observability/data_quality_service.py)/[data_quality_scheduler](../../backend/app/tasks/data_quality_scheduler.py) 已接；MetricHistory/质量快照/日志；Grafana 导出 JSON | 周期 UI/删除预览冻结未接齐，逻辑分层不等于物理迁移；总览以限量列表长度当总数，查询窗口/分页不足。Exporter 只登记/探活，远端安装外置。M09/M28。 |
| F24 通知渠道/测试、策略/模板/收件人、发送/重试/订阅（A25-A27） | [WorkflowManagementPages](../../frontend/src/WorkflowManagementPages.jsx) → [notify API](../../backend/app/notify/api.py)/[service](../../backend/app/notify/service.py) → Event/Log/Throttle；已调用真实适配器与周期重试 | SMTP/SMS 测试缺目标；模板/time_filter 未消费，配置页面不完整；sent 不等于送达，unknown 缺对账且日志多态不准。M37、V05；ISS-12 的已修事实保留。 |
| F24 ITSM 配置/测试/映射、来源/手工建单/同步/评论/关闭、规则/回调（A28-A30） | [itsm API](../../backend/app/itsm/api.py)/[service](../../backend/app/itsm/service.py) → Config/Ticket/SyncLog；真实适配器及幂等基础存在，页面仅接部分管理/详情 | 映射/自动规则无执行消费；认证/测试声明过宽；Jira 关闭未核实，pending/回调归属/乱序对账不足。M38-M40；不能全部仅标“待实测”。 |

### 3.2 巡检、性能与数据库运维

完整 30 个操作族与未通读的细项范围见[分项 C](../archive/acceptance/2026-10-20-functional-inventory-c.md)。其中盘点 C01-C30 是该快照内行号，不是开发计划 C01-C16。

| 蓝图/操作族 | 已核对路径与结果 | 当前缺口/待办 |
| --- | --- | --- |
| F05 模板 CRUD/克隆/默认/分配、自定义指标、交换、版本（C01-C04） | [InspectionPages](../../frontend/src/InspectionPages.jsx) → [inspection API](../../backend/app/api/v1/inspection.py) → [policy_service](../../backend/app/services/inspection/policy_service.py)/[template_versions](../../backend/app/services/inspection/template_versions.py)/[custom_metric_service](../../backend/app/services/inspection/custom_metric_service.py)；策略/版本与正式 findings 持久化 | Oracle 多 profile、其他三引擎和主机模板已有；历史 API 已有而 UI 未接，发行签名未接；自定义指标安全预算需按驱动验证。M05/M06、V01。 |
| F06 单实例/主机/定时、助手全库、列表详情/导出、对比、旧入口（C05-C10） | [dispatch](../../backend/app/services/inspection/dispatch.py)/[jobs](../../backend/app/services/inspection/jobs.py) → [runner](../../backend/app/services/inspection/runner.py) → [result_writer](../../backend/app/services/inspection/result_writer.py)/[report_builder](../../backend/app/services/inspection/report_builder.py)，权威结果 DBCheckResult，MD/HTML/Word 路径已有 | 浏览器批次/同步作业、job 控制回读/回收、申请人幂等和实际冻结模板仍有断点；诊断及旧引擎/AI 入口直调旁路，跨模板对比缺覆盖门禁。U05-U08/M05/M06。 |
| F10 总览/数据库画像、建议/方案/转单（C11） | [performance API](../../backend/app/api/v1/performance.py) → [suggestion_aggregator](../../backend/app/services/suggestion_aggregator.py)，DBSuggestion 汇总已有证据 | 建议状态不证明优化已做，转单不证明外部建单完整；多为方案不执行，关联 M13/M40。 |
| F10 SQL 画像/计划风险/历史比较、表画像/索引（C12-C14） | [perf_profile_collector](../../backend/app/services/perf_profile_collector.py) 写 Profile/指标 history/Table/Index；[sql_profiles API](../../backend/app/api/v1/sql_profiles.py) 与计划解析/风险/建议读取已有 | O/M 专用实时已有，PG/SS 缺采集；DBSqlPlan/Risk/Advice 未找到本仓生产 writer，不能用指标 history 充当真实计划。M10；ISS-33。 |
| F10 慢 SQL 实时/历史/采集/单条模型报告（C15-C16） | [slow_sql service](../../backend/app/services/slow_sql.py) 确有 DBSlowSQL 写入；[OracleSlowSqlPage](../../frontend/src/OracleSlowSqlPage.jsx)/MySQL 专业页；[slow_sql_ai](../../backend/app/services/slow_sql_ai.py) 真调用模型并存 AIReport | 专用 MySQL collect 与统一持久 collect 含义不同；Oracle cursor/digest 累计不等于窗口；PG/SS 专用采集缺。单条 Oracle 模型步骤与 MySQL 专用规则摘要不得混称。M10/M11、U08。 |
| F10 基线/日周月均值/问题趋势（C17） | [perf_baseline](../../backend/app/services/perf_baseline.py) 四引擎采集/保留/基线真实存在 | 多项速率累计除 uptime，PG tuple 速率不能叫 QPS；缺时间/覆盖/重置语义。M41。 |
| F11 AWR 原生导出/上传/下载/删除、本地分析/对比、可选 AI、定时（C18-C21） | [oracle_awr API](../../backend/app/api/v1/oracle_awr.py)/[oracle_awr service](../../backend/app/services/oracle_awr.py)/[awr_collect](../../backend/app/services/awr_collect.py) → AwrReport 与文件，本地规则结果已有 | 起止时间写入与深度报告严格读取不闭环；阶段失败、许可/外发门禁和模型结果持久回读待补。M11；本地规则分析不需要模型。 |
| F11 四引擎深度报告/窗口/列表详情/比较（C22） | [WorkloadReportPage](../../frontend/src/WorkloadReportPage.jsx) → [workload_report](../../backend/app/services/workload_report.py)/[workload_window](../../backend/app/services/workload_window.py) → DBWorkloadReport | 非 AWR 历史窗口继续拒绝；MySQL 差分/Query Store 区间缺，PG 差分函数已有但真实 collector 不产必要 counter_snapshot。M11；ISS-15/30。 |
| F12 会话/事务/锁/等待/阻塞与处置方案（C23-C24） | Oracle/MySQL 专业 API；PG/SS [generic_db_ops](../../backend/app/services/generic_db_ops.py) 基础读取；kill 多为脚本/占位方案 | PG/SS 细项别名未等价实现；终止会话缺目标绑定审批/真实执行复验；OS kill 保留人工/CLI。M12；ISS-26。 |
| F13 空间/文件/Temp/Undo/对象/统计/分区（C25-C26） | 专业 Oracle/MySQL 页面和 API、PG/SS 通用中心；多数只读和维护/扩容方案，部分历史另存 | PG/SS bloat/索引/碎片等名义接口复用基础表，不算细项已实现；原生 Oracle 每个细项 SQL/脚本本轮未全部通读。M13；ISS-34。 |
| F13 参数/配置基线/快照/漂移、整改/验证/回滚（C27-C28） | [config_governance](../../backend/app/services/config_governance.py) → 配置模型/AutomationPlan/Approval/Run；正向四引擎参数操作有执行 | 回滚执行器仅 MySQL 受限数值/布尔且无需重启的绑定计划，不是四引擎任意 SQL；中途失败不保证全回滚。M13/U07。 |
| F13 Redo/Archive/FRA/Binlog/Relay/WAL/事务日志、旧 SSH 部署（C29-C30） | 专业日志 API/方案；[Oracle API](../../backend/app/api/v1/oracle.py) 和 [ssh_executor](../../backend/app/services/ssh_executor.py) 另有真实远端文件部署/执行 | 不能概称所有操作都只产方案：`run=false` 部署也写文件。模型禁止传 command/script/remote_path；SQL Server Error Log/VLF 等别名未等价实现。M13，旧路径权限未作完整安全审计。 |

### 3.3 保护、集群、自动化与平台管理

完整 29 个操作族、四引擎支持矩阵与逐项证据见[分项 B](../archive/acceptance/2026-10-20-functional-inventory-b.md#3-逐操作族盘点29-行)。

| 蓝图/操作族 | 已核对路径与结果 | 当前缺口/待办 |
| --- | --- | --- |
| F04 HA 事实/风险/快照、切换检查、节点/切换/恢复动作（B01-B03） | CMDB/专业 HA 页 → [ha_diagnostics](../../backend/app/cmdb/ha_diagnostics.py)/各引擎服务，Cluster/HealthSnapshot 可回读 | 健康诊断/方案已有，不等于实时全节点探测或已执行切换；缺审批→独立 HA 操作→进度/角色验证桥。Patroni REST 尚非已接适配器。M04；ISS-29。 |
| F14 策略/候选、归档/迁移/分区方案（B04-B05） | [DataGovernancePage](../../frontend/src/DataGovernancePage.jsx) → [data_governance API](../../backend/app/api/v1/data_governance.py)/candidates/plans；Policy/Candidate/Job 可回读 | 扫描真连库，方案不等于迁移/分区执行，未见策略周期归档 worker；清源继续禁用。M14/U06。 |
| F14 预检/审批/加密导出、存储/保留/恢复测试（B06-B08） | [data_governance_execution](../../backend/app/services/data_governance_execution.py) 四引擎只读一致性 CSV 导出/AES-GCM/NAS-S3 写入，Execution/Artifact/RecoveryTest 存储 | 真实导出已有；恢复测试仍只读解密/hash/行数，未导入；独立传输计划/provider hold/删除消费者未接完整。M14/M35；不能写成归档均无执行。 |
| F15 目录/方案/预检、手动备份/验证（B09-B11） | [BackupRecoveryCenterPage](../../frontend/src/BackupRecoveryCenterPage.jsx) → [backup_center](../../backend/app/services/backup_center.py)/[backup_operations](../../backend/app/services/backup_operations.py)，Operation/Audit、制品/hash/源验证关系已有 | O/M/PG 部分动作真实 SSH 执行，SS 执行集为空；校验不是恢复；运行中取消拒绝，持久详情/审批/取消 UI 未全接。M15/M36、U06/U07。 |
| F15 周期/重试/保留、外部备份同步/健康/汇总（B12-B13） | [backup_policies](../../backend/app/services/backup_policies.py)/[backup_platform](../../backend/app/services/backup_platform.py)，Policy/Audit/ExternalRun/Drill 等 | 已有条件领取/owner/stale unknown 隔离，不是完全无控制；保留只预览，外部同步/抽检记录不等于外部恢复执行。M15/M16；V06。 |
| F16 准备度、演练策略、独立恢复/PITR（B14-B16） | `get_recovery_readiness`、[restore_drill_policies](../../backend/app/services/restore_drill_policies.py) → 策略/Operation/证据投影 | 独立恢复目标/链/业务校验闭环未具备；MySQL/PG restore/pitr 正确 blocked，Oracle restore_validate 是备份集验证而不是真恢复。M16；ISS-27。 |
| F17 案件/镜像/证据链/分片上传（B17） | [StorageRescuePage](../../frontend/src/StorageRescuePage.jsx) → [recovery router](../../backend/app/modules/recovery/router.py)/[governance_service](../../backend/app/modules/recovery/governance_service.py) → Case/SourceImage/Custody/Upload | 基础平台治理和上传协议已有；镜像“验证”用上报值，不证明实际读取输入或 OS 只读。UI 上传接线不足，镜像与执行输入强绑定待补。M17。 |
| F17 Agent/工具授权、白名单任务/审批/进度/报告（B18-B19） | [recovery service](../../backend/app/modules/recovery/service.py) → Task/Approval/Progress/Artifact/报告；Agent 传输显式服务账号授权和一对一绑定已有 | 外置算法保留；人类审批人从 payload 取值、领取/attempt/取消对账不闭环。不能因为 Agent 鉴权已有而忽略人类批准绑定。M17；ISS-17/48 边界保留。 |
| F18 方案/双人批准、单段执行/取消/回滚（B20-B22） | [AutomationCenterPage](../../frontend/src/AutomationCenterPage.jsx) → [automation service](../../backend/app/automation/service.py)/SSH/Ansible/受限数据库回滚 → Plan/Approval/Run/Output/锁/审计 | 人工脚本审批已有，但不能让 AI 动态写脚本；缺步骤模型/暂停续跑/实时输出，本地进程组退出不证明远端已停。M18/U07；ISS-32。 |
| F26 本地账户/密码/MFA/会话、角色/组织/范围/审计（B23-B24） | 用户/安全页面 → users/auth/security API → User/AuthSession/Role/Scope/SecurityAudit | 后端动作/范围有实现，非仅菜单隐藏；全租户硬隔离、敏感动作重认证、多进程 MFA 等仍需逐项核验。U03/M03、V01/V05。 |
| F26 企业源/组映射/身份生命周期、参数/模块/初始化/许可（B25-B26） | [identity API](../../backend/app/identity/api.py)/[service](../../backend/app/identity/service.py)，LDAP/OIDC 真实链；平台参数/初始化也有代码 | 企业源/映射读改停用和身份管理未完整产品化；离职处理函数未见生产调用，企业 MFA 策略需冻结。初始化/重启是高权副作用不交模型。M31/U03。 |
| F26 Secrets、只读发布 API 与 CLI 升级（B27-B29） | [core/secrets](../../backend/app/core/secrets.py)、[release_center API](../../backend/app/api/v1/release_center.py)、[release_manager](../../backend/app/runtime/release_manager.py)/[CLI](../../scripts/release_manager.py) | envelope/env/Vault、原生/容器、签名补丁/备份迁移/配置三方合并/检查点已有；缺发布只读 UI、事务恢复 CLI 接线、灰度控制与完整秘密轮换范围。M30/M32-M34，现场 V06。 |

### 3.4 诊断、现场、状态、内核与统一报告

| 操作族 | 入口、处理与存储/回读依据 | 当前事实与缺口 |
| --- | --- | --- |
| D01 F19 诊断上下文、规则与根因候选 | [diagnosis_center.py](../../backend/app/api/v1/diagnosis_center.py) → [orchestrator.py](../../backend/app/services/diagnosis/orchestrator.py) `run_diagnosis` → [诊断模型](../../backend/app/models/diagnosis_center.py)、事故详情 | 已有多源证据、规则候选、时间线、证据质量与可选 AI；完整流程同步，内含巡检直调旁路。候选不等于已证实根因。U05/U06、V01。 |
| D02 F19 小助手识别、直接调用与进度 | [DiagnosisCenterPage.jsx](../../frontend/src/DiagnosisCenterPage.jsx) → [assistantInspection.js](../../frontend/src/modules/assistantInspection.js)、[assistantActions.js](../../frontend/src/modules/assistantActions.js)；`route_intent` 可选模型分诊 | 巡检有本地确定性识别，其他功能已有前端直接调用；通用模型主要选页面，不输出受校验多步骤计划。通用进度/结果多在 React 状态；前端超时不等于后台停止。U01-U12。 |
| D03 F19 模型配置、调用和降级 | [ai_config.py](../../backend/app/services/ai_config.py)、[ai_orchestrator.py](../../backend/app/services/diagnosis/ai_orchestrator.py) `orchestrate`；诊断 AI 结果随事故保存 | 已有单选 provider、结构化合同/引用验证、规则降级与调用门禁；不自动跨 provider 发送证据。仍缺任务级外发批准、全局预算、本地模型现场质量证据。M19、V05；ISS-39。 |
| D04 F19 评测数据集、运行与成本 | [DiagnosisEvaluationPage.jsx](../../frontend/src/DiagnosisEvaluationPage.jsx) → [diagnosis_evaluation.py API](../../backend/app/api/v1/diagnosis_evaluation.py) → [评测服务](../../backend/app/services/diagnosis_evaluation.py) `run_evaluation`、`_rule_channel_basis` | 数据集/冻结/克隆、case 和运行结果有存储/回读，token 成本有状态；按输入 `cause_code` 聚合的规则评测已标 `label_assisted`，并非独立盲测。评测循环同步，模型效果未实测。M20、U06、V05。 |
| D05 F20 事故列表、详情与快照入口 | [IncidentSnapshotPages.jsx](../../frontend/src/IncidentSnapshotPages.jsx) → [queries.py](../../backend/app/incident_snapshot/queries.py) `list_incidents`、`incident_snapshot_context` | 事故、最新快照、资产和根因可回读；列表先 `.all()`，逐事故查快照/根因/资产，再 Python 分页。存在无界读取及逐行查询，尚未做规模压测。M21。 |
| D06 F20 Agent 身份、心跳与命令 | [incident_snapshot/service.py](../../backend/app/incident_snapshot/service.py)、[security.py](../../backend/app/incident_snapshot/security.py)、[Agent app.go](../../agent/internal/application/app.go) | 已有注册身份、签名命令、nonce、过期、领取/重投和 ACK；命令轮询默认 3 秒，主循环亦执行采集，不能据此承诺毫秒级实际停止。U07、M22、V02/V03。 |
| D07 F20 触发策略与基础快照 | [快照 API](../../backend/app/incident_snapshot/api.py) → `create_capture` → `IncidentSnapshot`/`IncidentAgentCommand`；[Agent snapshot](../../agent/internal/snapshot/package.go) | 已有策略、事故时间窗、缓存冻结、命令与快照记录；`create_capture` 可同步采集关联数据库状态后才返回，不是全部快速受理。采集开销/断网恢复/大规模待实测。U06、M22、V03。 |
| D08 F20 分片上传、续传、规范化与分析 | [ingest.py](../../backend/app/incident_snapshot/ingest.py) `initialize_upload`/`store_chunk`/`resume_upload`/`complete_upload` → [模型](../../backend/app/incident_snapshot/models.py)/[storage.py](../../backend/app/incident_snapshot/storage.py) | 校验归属、分片数、字节数、manifest；落指标/事件/进程/服务/数据库状态后分析，缺流可 `partial`。完成上传内同步规范化/分析；真实重传、并发、存储压力与千台规模未验收。U06、M22、V03。 |
| D09 F21 状态定义、观测与时间线 | [state_engine/api.py](../../backend/app/state_engine/api.py) → [service.py](../../backend/app/state_engine/service.py) `ingest_observation`、`create_definition_version` → [状态模型](../../backend/app/state_engine/models.py) | 已有识别、版本、防抖/滞回、质量未知、事件幂等和时间线；普通监控/Snapshot 未完整自动桥接。前端目前以定义/时间线读取为主，版本管理/观测回放编辑未完整产品化。M23/M24；ISS-36。 |
| D10 F21 模式匹配、回放与事故关联 | 同上 `replay`、`_match_persisted_patterns`；[状态与现场页](../../frontend/src/StateInvestigationPage.jsx) | 已有模式测试/匹配持久化与可信内核链事故关联；手工/旧状态缺可信来源时不猜事故。回放 API 不等于可操作的回放页面，更不等于原始现场已回放。M23/M24、V04。 |
| D11 F22 线程/内存/Dump 采集 | [scene API](../../backend/app/scene_reconstruction/api.py) → [scene service](../../backend/app/scene_reconstruction/service.py) `create_capture_job`/`approve_capture_job`/`cancel_capture_job` → CaptureJob/签名命令 → [Agent scene](../../agent/internal/scene/platform_linux.go) | 有采集、审批和取消请求；但 Agent `CancelCapture` 发 context cancel 后即回执，不等实际退出，后端 ACK 未接 `cancel_capture` 的业务停止确认。这是 U07 接线缺口，不只是延迟待测。jcmd 已有，连续栈/ETW/WCT 缺；深制品外存边界保留。M25；ISS-16/37。 |
| D12 F22 动态探针租约 | `create_probe_lease`/`remove_probe_lease` → ProbeLease/Agent 命令；[scene models](../../backend/app/scene_reconstruction/models.py) | 模板化探针、租期、移除和回执已有；实际目标进程/工具版本、TTL 后清理和取消确认待现场验收，不宣称任意进程均可动态打印。U07、M25、V04。 |
| D13 F22 Context Pack | `generate_context_pack`/`get_context_pack` → SceneContextPack；[前端 API](../../frontend/src/modules/stateInvestigationApi.js) | 有保存、来源冻结和脱敏/敏感查看控制；前端包装目前只生成，展示状态在页面内，历史包重新打开需核对/接线。上下文是证据摘要，不是“AI 亲临现场”的等价证明。M24、U08。 |
| D14 F23 能力、策略与租约升降级 | [kernel API](../../backend/app/kernel_observability/api.py) → [service.py](../../backend/app/kernel_observability/service.py) → [kernel models](../../backend/app/kernel_observability/models.py) → [manager.go](../../agent/internal/kernelobs/manager.go) | 有 E0-E4、预算、策略、能力/健康读取和命令；前端有查看/测试/升降级，策略创建 API 不等于已有完整管理表单。Agent 实际 attach 集合协议仍不完整。M26/M27。 |
| D15 F23 事件/栈、可信状态桥及停止 | `ingest_events`/`_event_state_identity`/`ingest_stacks`/`set_kill_switch`；[loader](../../agent/cmd/tracemind-ebpf-loader/main_linux.go) | 已有归属校验、质量/丢失记录、去重、事务性状态写入和停止命令；只有带合法 `state_*` 指标的事件触发状态识别，不能推定所有原生事件均已自动转状态。USDT、完整用户栈/容器关联仍缺。M23/M26/M27；停止延迟待测。 |
| D16 F25 报告目录、下载、签名与附件 | [UnifiedReportCenterPage.jsx](../../frontend/src/UnifiedReportCenterPage.jsx) → [report_center API](../../backend/app/api/v1/report_center.py) → [report_center.py](../../backend/app/services/report_center.py) → [报告模型](../../backend/app/models/report_center.py) | 已有跨来源目录、内容/签名、下载、附件和审计；统一任务清单与所有业务结果的持久映射尚未建立。不能以统一报告页面存在证明所有功能已出报告。U08/M28。 |
| D17 F25 报告归档、保留与冻结 | `archive_artifact`/`apply_retention`/`set_legal_hold` | 保留/冻结和归档元数据已有；归档只写状态/时间/URI，没有完成真实冷热存储搬迁、校验、检索和迁移失败恢复。M28；ISS-38。 |
| D18 F25 报告计划与订阅 | `execute_schedule`/`run_due_report_schedules`/`_dispatch_subscriptions` → ReportScheduleRun/Artifact/投递；[SchedulePage.jsx](../../frontend/src/SchedulePage.jsx) | 有幂等/owner 检查、误点处理、订阅路由与结果；执行和多个任务类别仍串行，外部投递与报告生成结果需要分开核验。U06/U07/M29、V02/V06。 |
| D19 F25 总调度、租约与业务真值 | [master_scheduler.py](../../backend/app/tasks/master_scheduler.py) `run_due_tasks` → report_center 租约/dispatch；[调度 API](../../backend/app/api/v1/scheduler.py) | 有独立心跳、epoch 和业务结果聚合；仅在每类函数派发前/批次结束检查，不向全部业务提交/外部副作用传递 fence。不是全链双主隔离。U07/M29；ISS-31。 |
| D20 F19 规则诊断 CRUD、运行与结果转单/推送 | [诊断中心规则入口](../../frontend/src/DiagnosisCenterPage.jsx)；`/diagnosis/rules` CRUD、`/diagnosis/run`、`/diagnosis/results` | [diagnosis_rules.py](../../backend/app/api/v1/diagnosis_rules.py) → [rule_engine.py](../../backend/app/services/rule_engine.py) `run_rule_diagnosis` → DiagnosisRule/DiagnosisResult/DiagnosisEvidence；转单/动作方案/Zabbix 载荷均为 `plan_only` | 规则编辑无版本与审批；运行同步执行并复用巡检 dispatch，阻塞请求；结果列表只有 limit、无游标；`plan_only` 不代表外部工单/告警已送达。U05/U08、M20。 |
| D21 F25/C02-C11 统一任务内核 API：能力目录、计划/修订/审批、批次/准入、执行控制、固定流程与 AI 候选 | [AssistantProposalPanel](../../frontend/src/AssistantProposalPanel.jsx)、任务清单/批次/流程工作台；`/assistant/capabilities`、`/plans*`、`/batches*`、`/executions/*/control`、`/workflows*` | [assistant.py](../../backend/app/api/v1/assistant.py) → [capability_registry.py](../../backend/app/services/capability_registry.py)、[plan_service.py](../../backend/app/services/plan_service.py)、[plan_queue.py](../../backend/app/services/plan_queue.py)、[plan_control.py](../../backend/app/services/plan_control.py)、[workflow_service.py](../../backend/app/services/workflow_service.py) → DBCapabilityPlan/Batch/Execution/Preparation/ControlCommand/Proposal | 能力可见性按当前动作权限与资产范围过滤；计划/批次/receipt 均可按原 key 回读；控制命令需 owner 与 durable execution；候选恒 `accepted=false`，采用后仍走原 prepare/审批 | 计划执行只覆盖已接 handler（`inspection.run.execute` 仍受发布门禁）；远端停止/接管/对账未闭环；真实模型与出站 payload 未验收；77 条声明不等于 77 个执行器。C02-C14、V01。 |

## 4. 72 个菜单入口的归属核对

本节只防止模块被漏列，不表示每个入口均完成执行卡片化。共用页面的多个入口仍分别列出；F03 接入在资产/实例详情内，F22/F23 在共享状态现场页，F26 发布 CLI 无菜单不等于缺实现。

| 蓝图归属 | 叶子入口 key |
| --- | --- |
| F01 | `home.dashboard` |
| F02/F03 | `asset.center` |
| F04/F12/F13 | `ops.ha`、`ops.matrix`、`ops.configGovernance`、`ops.overview`、`ops.log`、`ops.session`、`ops.space`、`ops.object`、`ops.security` |
| F05/F06 | `inspect.overview`、`inspect.run`、`inspect.tasks`、`inspect.reports`、`inspect.compare`、`inspect.policies`、`inspect.templates` |
| F07/F08 | `monitor.alert.db`、`monitor.alert.unified` |
| F09 | `monitor.observability`、`monitor.observability.logs`、`monitor.observability.grafana`、`monitor.observability.apm`、`integration.zabbix`、`integration.prometheus` |
| F10/F11 | `perf.overview`、`perf.workload`、`perf.sql`、`perf.slowsql`、`perf.lock`、`perf.index`、`perf.table`、`perf.optimization`、`perf.baseline`、`perf.trend`、`perf.capacity`、`perf.awr.report`、`perf.awr.history`、`perf.awr.compare`、`perf.awr.collect` |
| F14 | `ops.governance`、`ops.governance.objectStorage` |
| F15/F16 | `backup.overview`、`backup.policy`、`backup.plan`、`backup.restore`、`backup.report`、`backup.platform` |
| F17 | `rescue.overview`、`rescue.cases`、`rescue.tools`、`rescue.agents`、`rescue.tasks`、`rescue.reports`、`rescue.approvals` |
| F18 | `protection.automation` |
| F19 | `diagnose.smart`、`system.ai`、`system.aiEvaluation` |
| F20 | `diagnose.incidents`、`diagnose.snapshot.agents`、`diagnose.snapshot.policies` |
| F21/F22/F23 | `diagnose.investigation` |
| F24 | `integration.notify`、`integration.itsm` |
| F25 | `report.unified`、`report.alert`、`system.schedule` |
| F26 | `system.user`、`system.identity`、`system.params` |

## 5. 不应重复开发或误报缺失的边界

- 不将功能页面、模块开关、能力说明目录和可执行卡片四种粒度混用。
- 不重新登记已修的能力成熟度派生、手动巡检 API 接队列、模板版本 API、容量防抖、质量周期消费者和配置三方合并为“缺实现”；它们的剩余 UI/worker/部署/故障验收独立列项。
- 抢救算法在外置执行端、深采集制品留 Agent/外部、安装与回滚走 CLI 是已定边界。需要的是安全交接和真实回执，不是把命令或任意文件执行入口加进 Web/模型。
- 恢复源保护、未支持引擎、SQL Server 证书能力拒绝等阻断应保留；“所有功能通用”不意味着忽略引擎、许可、权限、资源和运行环境差异。

## 6. 审查限制与接续

这是逐操作族源码盘点，不是全面安全审计或现场验收。未逐个执行 API 权限组合，未打开每个页面/移动视口，未逐条执行引擎 SQL/脚本，未验证历史迁移是否已加载，未核验客户安装包或远程服务。未由本轮证明的问题不得预先勾选。

缺口统一进入[详细待开发清单](pending-development-backlog.md)，保留原 ISS 编号作关联，不重置已知问题的历史状态。C01 的源码操作族盘点阶段有本文件作为产出；C02-C16 的统一机制仍待开发/验收，不能因盘点完成提升模块完成率。
