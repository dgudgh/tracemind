# 蓝图对齐计划（F01-F27）

本文件是**当前状态**载体，随代码更新。用途与闭环见 [README](README.md)。

2026-10-08 更新：当前逐项状态已同步[当前差距表](current-gap-assessment.md)与
[HTML 蓝图](../../../orchestrator-enterprise/docs/database-operations-blueprint.html)。27 项中
7 项主链具备、19 项部分实现、1 项 MES 暂缓；不代表功能完成率或现场验收率。
下文 09-22/09-23 记录保留为历史，旧结论不得覆盖当前表。

下一代控制规划见[AI 原生与五平衡架构](ai-native-control-architecture.md)，工作包 B01-B06
复用现有 F 模块，不新建第二套任务系统；尚未形成统一控制闭环，不改变下表完成度。

2026-10-20 补充跨模块门禁：[软件四项质量底线与验收契约](software-quality-contract.md)。
先补正确性/接管与全链所有权，再完成异步快慢路径、故障恢复及负载/168 小时长稳验收；建议阈值未实测，不改变模块完成数。

2026-10-20 执行入口原则：[功能卡片与可选 AI 编排](capability-card-orchestration.md)。
先把手工卡片绑定已有能力并统一任务/结果，再接可选模型编排；固定的是流程与规则，不是忽略实时数据的固定结论。本项仍属规划。

全模块接入按[全功能编排任务清单 C01-C16](capability-card-task-plan.md)推进：先清单与巡检样板，再后台任务/人工控制，随后固定流程/可选 AI，最后逐能力接入与验收。统一运行机制未实施，不勾选完成。

2026-10-20 已形成 C01 [源码操作族盘点](functional-inventory.md)及[详细待开发清单](pending-development-backlog.md)：F01-F26 主要调用链与 72 个叶子入口逐组映射，按 U/M/V 列出证据、真实缺口、依赖和失败验收。旧表中的“全入口队列化/PG 差分完成/通知工单仅待实测”等过宽描述以新盘点的具体限定为准；不改变历史已修事实，不提高模块完成数。MES 继续排除。

基线：蓝图 `orchestrator-enterprise/docs/database-operations-blueprint.html`（F01-F27）。
上次全量对照：[2026-09-08 差距审计](../archive/plans/2026-09-08-blueprint-gap-audit.md)，
执行记录：[整改台账](../archive/plans/2026-09-08-blueprint-remediation-workboard.md)。

**历史轮次（2026-09-22）完成过全量复核。** 方法：按 F 编号分组逐个读当时工作区代码
（不是 HEAD——HEAD 仍是 9475158，落后两周），按符号定位而非行号；主代理对其中 6 处关键判断
做了独立复核验证。`证据` 列给出的位置可供复现。

**未接线 ≠ 未实现。** 本轮反复遇到一种形态：代码和测试都写好了，但生产路径没有调用它。
文件在、测试在、门禁全绿，功能却不生效。判定"具备"以**执行路径**为准，
此类一律记为"已建未接线"，不计入具备。

## 0. 口径映射：蓝图 → 平台

映射依据：`module_config.py` 的 category 与模块名、菜单树、以及 9-08 审计给出的代码入口。
空白表示平台侧**没有对应载体**——那本身就是对齐信息。

| F | 蓝图模块 | 模块开关（module_key） | 前端页面 | 后端/Agent 入口 |
| --- | --- | --- | --- | --- |
| F01 | 工作台与运营总览 | `home.dashboard` | 工作台 | `services/operations_dashboard.py` |
| F02 | 五层资产与 CMDB | `asset.center` | 资产管理 | `cmdb/normalized_service.py` |
| F03 | 实例接入与凭据 | —（无独立开关） | `InstanceDrawer.jsx` | `api/v1/instances.py` |
| F04 | 集群拓扑与高可用 | `ops.ha` | 高可用 | `cmdb/ha_diagnostics.py` |
| F05 | 巡检模板与指标库 | `inspect.templates`、`inspect.policies` | 巡检模板、巡检策略 | `services/inspection/policy_service.py` |
| F06 | 巡检执行与报告 | `inspect.overview/run/tasks/reports/compare` | 巡检总览、一键巡检、任务、报告、历史对比 | `services/inspection/runner.py` |
| F07 | 指标监控与容量告警 | `monitor.alert.db` | 容量告警 | `services/capacity_monitor.py` |
| F08 | 统一告警与事件 | `monitor.alert.unified` | 综合告警 | `api/v1/alert_events.py` |
| F09 | 可观测与日志/APM | `monitor.observability(.logs/.grafana/.apm)` | 可观测监控、日志关联、Grafana 模板、APM 链路 | `services/observability/data_quality_service.py` |
| F10 | 性能分析与优化 | `perf.overview/sql/slowsql/lock/index/table/baseline/trend/capacity/optimization` | 性能总览、SQL 画像、慢 SQL、锁等待、索引、表画像、基线、趋势、容量、优化建议 | `services/perf_profile_collector.py` |
| F11 | AWR 与引擎深度报告 | `perf.awr.report/history/compare/collect`、`perf.workload` | AWR 综合/历史/对比/自动采集、引擎深度报告 | `services/workload_report.py` |
| F12 | 运行、会话与事务 | `ops.overview`、`ops.session` | 运行概览、会话事务 | `services/generic_db_ops.py` |
| F13 | 空间、对象、参数与日志 | `ops.space`、`ops.object`、`ops.log`、`ops.configGovernance` | 空间管理、对象统计、日志管理、配置基线 | `services/config_governance.py` |
| F14 | 数据治理与归档 | `ops.governance`、`ops.governance.objectStorage` | 数据治理、归档对象存储 | `services/data_governance_execution.py` |
| F15 | 备份管理 | `backup.overview/policy/plan/platform` | 备份总览、策略、方案、平台接入 | `services/backup_center.py` |
| F16 | 恢复、校验与演练 | `backup.restore` | 恢复方案 | `services/oracle_recovery.py` |
| F17 | 数据抢救 | `rescue.*`（7 项） | 数据抢救（Tab 页） | `modules/recovery/service.py` |
| F18 | 自动化审批与执行 | `protection.automation` | 自动化审批 | `automation/service.py` |
| F19 | AI 与规则诊断 | `diagnose.smart`、`system.ai`、`system.aiEvaluation` | 智能诊断、AI 配置、诊断评测 | `services/diagnosis/orchestrator.py` |
| F20 | Incident Snapshot | `diagnose.snapshot.agents/policies`、`diagnose.incidents` | 现场 Agent、快照策略、事故中心 | `incident_snapshot/ingest.py`、`agent/` |
| F21 | State Intelligence | `diagnose.investigation` | 状态与现场 | `state_engine/service.py` |
| F22 | 深度现场重建 | `diagnose.investigation`（共享入口） | 状态与现场：现场采集、动态探针 Tab | `scene_reconstruction/service.py` |
| F23 | Linux eBPF 动态观测 | `diagnose.investigation`（共享入口） | 状态与现场：内核观测 Tab | `kernel_observability/`、`agent/internal/kernelobs/` |
| F24 | 通知与 ITSM | `integration.notify`、`integration.itsm` | 通知渠道、工单管理 | `notify/service.py`、`itsm/service.py` |
| F25 | 报告、审计与调度 | `report.unified`、`report.alert`、`system.schedule` | 统一报告、告警报告、调度任务 | `services/report_center.py` |
| F26 | 平台管理、企业安全与版本交付 | `system.user/identity/params`、`moduleConfig` | 用户管理、企业认证、系统参数、权限配置 | `runtime/release_manager.py` |
| F27 | MES 嵌入与业务联动 | —（本期暂缓） | — | — |

映射说明（2026-10-08）：

- **F03 实例接入**没有模块开关与独立页面，只有抽屉组件。它是基础能力还是应纳管，需产品决定。
- **F22 / F23** 共用 `diagnose.investigation` 页面入口，已在 `AppPageRouter.jsx` 和
  `StateInvestigationPage.jsx` 接通。共享模块开关不代表没有页面；页面存在也不等于深采集制品链已完成。

## 1. 对齐状态（2026-10-08）

当前表评价源码路径，不表示当前服务已加载或客户验收完成。7 项主链具备、19 项部分实现、1 项暂缓。
详细验收限制与下一批顺序见[当前差距表](current-gap-assessment.md)。

| 编号 | 模块 | 源码现状 | 仍有差距 | 核对入口 |
| --- | --- | --- | --- | --- |
| F01 | 工作台与运营总览 | 主链具备：指标、风险与工作队列 | 组织、环境、负责人等筛选未完整进入查询；多个来源仍全表读取后在内存处理，大规模成本待测 | [operations_dashboard.py](../../backend/app/services/operations_dashboard.py) `build_operations_dashboard` |
| F02 | 五层资产与 CMDB | 主链具备：发现、校正、关系及审计；前端已使用存储关系绘图 | 外部 CMDB 冲突、复杂级联及混合拓扑需现场验证；关系来源和真实探测覆盖需要继续核对 | [normalized_service.py](../../backend/app/cmdb/normalized_service.py)、[AssetTopology.js](../../frontend/src/AssetTopology.js) |
| F03 | 实例接入与凭据 | 主链具备：四引擎接入与连接分派 | 四引擎目标版本、最小权限、证书拒绝和网络异常矩阵待实测。Oracle Wallet 参数和 SQL Server encryption 参数已有注入，旧表中“未见”过时；参数存在不等于 TLS 已验收 | [oracle_connection.py](../../backend/app/collectors/oracle/oracle_connection.py)、[sqlserver_connection.py](../../backend/app/collectors/sqlserver/sqlserver_connection.py) |
| F04 | 集群与高可用 | 部分实现：拓扑、健康诊断及只读处置方案 | 缺平台审批任务到独立 HA 引擎、实时状态、执行结果回写的闭环；节点替换/退役及真实 RTO/RPO 验收仍需补齐 | [ha_diagnostics.py](../../backend/app/cmdb/ha_diagnostics.py) `_action_readiness` |
| F05 | 巡检模板与指标库 | 部分实现：默认/自定义模板、交换；新建、编辑、克隆、删除四类路径均已落版本行 | 版本查询/恢复服务尚无对应业务 API 和页面入口（ISS-23）；模板签发验签未接入。版本表缺失时写入降级跳过历史，因此版本能力还依赖部署迁移 | [policy_service.py](../../backend/app/services/inspection/policy_service.py) `_capture_history`、[template_versions.py](../../backend/app/services/inspection/template_versions.py)、[inspection.py](../../backend/app/api/v1/inspection.py) |
| F06 | 巡检执行与报告 | 部分实现：真实巡检、失败语义、证据与报告；**定时、手动与隐式入口（诊断/规则/操作 AI/旧引擎页）均已走持久队列**（enqueue→claim→execute→commit_results，含租约续期），并由 AST 护栏禁止再直调 runner；**执行使用入队时冻结的模板修订**（ISS-53/54） | 运行步骤进度、取消、任务级超时、重试与中断恢复未形成统一链路；零结果失败任务在列表中的可见性未接 | [inspection.py](../../backend/app/api/v1/inspection.py) `api_run_inspection`、[inspection_scheduler.py](../../backend/app/tasks/inspection_scheduler.py)、[jobs.py](../../backend/app/services/inspection/jobs.py) |
| F07 | 指标监控与容量 | 部分实现：采集、趋势、预测及告警 | 容量策略缺持续时间与独立恢复阈值；PulseWatch 融合没有本次集成验收证据 | [capacity_policy.py](../../backend/app/models/capacity_policy.py)、[capacity_monitor.py](../../backend/app/services/capacity_monitor.py) |
| F08 | 统一告警与事件 | 部分实现：生命周期、维护抑制；到期失败动作已自动重排 | 重排只把 failed 改回 pending，实际执行交给外部 worker；当前仓库未形成通用动作执行闭环，跨模块幂等与领取所有权仍需验收 | [alert_lifecycle.py](../../backend/app/services/alert_lifecycle.py) `retry_due_actions`、[alert_action_scheduler.py](../../backend/app/tasks/alert_action_scheduler.py) |
| F09 | 可观测与日志/APM | 部分实现：外部源查询和采集、手动质量评估与保留处理 | 质量评估/保留没有周期消费者；日志/Trace 真实事件时间的新鲜度覆盖不完整。已有 observability 调度不代表质量调度已接通 | [data_quality_service.py](../../backend/app/services/observability/data_quality_service.py)、[observability_scheduler.py](../../backend/app/tasks/observability_scheduler.py) |
| F10 | 性能分析与优化 | 部分实现：基线、慢 SQL、SQL/表/索引画像及历史 | 专用 SQL 实时画像采集器、表画像和索引画像仍主要是 Oracle/MySQL；PG/SQL Server 需补齐。离线慢 SQL 记录接入不等于专用实时画像具备 | [perf_profile_collector.py](../../backend/app/services/perf_profile_collector.py) `SQL_PROFILE_LIVE_ENGINES`、`collect_table_profile`、`collect_index_findings` |
| F11 | AWR 与深度报告 | 部分实现：四引擎报告、历史、比较；Oracle 原生窗口与 PG 部分差分 | 非 AWR 指定历史窗口明确拒绝；MySQL 累计差分及 SQL Server Query Store 区间过滤仍需实现，不能把累计统计标成最近一小时 | [workload_report.py](../../backend/app/services/workload_report.py) `collect_report`、[workload_window.py](../../backend/app/services/workload_window.py) |
| F12 | 会话、事务与阻塞 | 部分实现：查询与处置方案 | PG/SQL Server 终止会话仍有占位符方案；目标绑定、审批执行与结果复验未接通 | [generic_db_ops.py](../../backend/app/services/generic_db_ops.py) |
| F13 | 空间、对象、参数与日志 | 部分实现：查询、配置基线、漂移、整改；数据库回滚执行器已注册 | 非参数对象维护、扩容及日志治理仍有人工方案；具体引擎和动作需逐项声明支持范围并实测 | [config_governance.py](../../backend/app/services/config_governance.py)、[automation/service.py](../../backend/app/automation/service.py) |
| F14 | 数据治理与归档 | 部分实现：导出、加密、文件验证和对象存储 | 恢复测试只做解密、解析、哈希和行数核对，未导入目标库；迁移/分区生命周期仍有方案；源清理禁用 | [data_governance_execution.py](../../backend/app/services/data_governance_execution.py) `run_recovery_test` |
| F15 | 备份管理 | 部分实现：RMAN、mysqldump、pg_dump 和策略执行 | SQL Server 真实执行驱动未接；XtraBackup、basebackup/WAL 等覆盖不足；保留清理、备份链保护和完整加密恢复链待补 | [backup_center.py](../../backend/app/services/backup_center.py) `get_operation_capability`、`EXECUTABLE_TYPES` |
| F16 | 恢复、校验与演练 | 部分实现：制品绑定与校验、恢复方案 | MySQL/PG 逻辑恢复因缺独立隔离目标被阻断；实际跨实例恢复、PITR、业务一致性、失败回退及真实 RTO/RPO 缺闭环；Oracle validate 不能计作恢复 | [backup_center.py](../../backend/app/services/backup_center.py) `LOGICAL_RESTORE_BLOCK_REASON`、[oracle_recovery.py](../../backend/app/services/oracle_recovery.py) |
| F17 | 数据抢救 | 部分实现：案件、授权、任务领取/回报协议和证据治理；**操作人与审批人取自 Bearer 身份，HIGH/DANGEROUS 禁止自批**（ISS-55） | 缺实际领取任务并执行离线扫描、预览、导出的 worker；现有 Incident Agent 未接该协议。镜像与实际输入强绑、原子领取/租约/离线 unknown 仍未做 | [recovery/router.py](../../backend/app/modules/recovery/router.py)、[agent/internal/application/app.go](../../agent/internal/application/app.go) |
| F18 | 自动化审批与执行 | 部分实现：审批、SSH/Ansible、目标锁、取消及专用数据库回滚；**场景采集的停止命令有 Agent 确认回写，『已发取消』不再等同『已停止』**（ISS-56） | 数据库执行器目前为配置回滚专用，不能视为任意数据库动作通用执行器；缺步骤级暂停续跑、状态和实时输出；其他域的 ACK 与业务完成差异未逐项核对 | [automation/service.py](../../backend/app/automation/service.py)、[database.py](../../backend/app/automation/executors/database.py) |
| F19 | AI 与规则诊断 | 主链具备：诊断编排、证据引用校验、规则降级；评测成本已计入 token 用量 | 规则评测直接聚合输入 cause_code，不能证明独立盲测准确率；任务级外发授权、全局调用预算和真实本地模型效果待补/实测 | [ai_orchestrator.py](../../backend/app/services/diagnosis/ai_orchestrator.py)、[diagnosis_evaluation.py](../../backend/app/services/diagnosis_evaluation.py) `_rule_predictions` |
| F20 | Incident Snapshot | 主链具备：基础采集、环形缓存、冻结、分片上传、续传与平台分析 | 千台规模、断网恢复、资源占用和存储成本需实际验收；基础线程信息不等于连续调用栈追踪 | [incident_snapshot/ingest.py](../../backend/app/incident_snapshot/ingest.py)、[snapshot/package.go](../../agent/internal/snapshot/package.go) |
| F21 | State Intelligence | 部分实现：识别、防抖、滞回、模式、回放及 eBPF 状态桥接 | 普通监控/Snapshot 尚无完整自动输入桥接；模式命中未写 incident_id，事故关联仍断 | [state_engine/service.py](../../backend/app/state_engine/service.py) `_match_persisted_patterns`、[kernel/service.py](../../backend/app/kernel_observability/service.py) |
| F22 | 深度现场重建 | 部分实现：现场采集页面、审批、探针、Linux jcmd 动态打印及 Context Pack | 缺连续线程栈追踪、Windows ETW/WCT；深采集制品上传、解析、符号化与 AI 原文引用链不完整；本地产物引用仍返回 partial | [StateInvestigationPage.jsx](../../frontend/src/StateInvestigationPage.jsx)、[scene/service.py](../../backend/app/scene_reconstruction/service.py) `handle_command_ack`、[platform_windows.go](../../agent/internal/scene/platform_windows.go) |
| F23 | Linux eBPF | 部分实现：页面、策略预算、loader ready、动态 hook 与部分 CO-RE | ready 有 attach 数量和 core_sensors，但 loaded_sensors 仍来自请求集合，未按实际完整 attach 集合确认；USDT、用户态符号和完整容器关联需补；实机长稳 Gate 待验收 | [kernelobs/manager.go](../../agent/internal/kernelobs/manager.go)、[main_linux.go](../../agent/cmd/tracemind-ebpf-loader/main_linux.go) |
| F24 | 通知与 ITSM | 主链具备：通道、工单适配、订阅受众分发、通知重试；**Jira 关闭改为回读确认，未证实的关闭不再连带关闭源告警**（ISS-57） | 企业通知/工单字段、认证、回调、评论和升级规则仍需按实际接入系统逐项验收；ServiceNow/ZenTao 关闭仍属『已接受未回读』 | [notify/service.py](../../backend/app/notify/service.py) `dispatch_report_subscription`、`consume_due_notifications`、[itsm/service.py](../../backend/app/itsm/service.py) |
| F25 | 报告、审计与调度 | 主链具备：报告目录/签名/订阅、审计、调度租约与 epoch | epoch 仅在调度派发边界校验，未覆盖所有业务提交及外部副作用；任务类别仍串行；报告归档状态/URI 不等于冷热存储迁移 | [master_scheduler.py](../../backend/app/tasks/master_scheduler.py) `run_due_tasks`、[report_center.py](../../backend/app/services/report_center.py) |
| F26 | 平台管理与版本交付 | 部分实现：权限、认证、Secrets；原生/容器驱动、签名补丁、增量迁移、检查点与恢复 CLI；**Release Center 只读 API 已交付**（状态/清单） | 无 Release Center 前端页面（ISS-41），install/rollback 有意不开放；配置仅保留/复制，未做三方合并（ISS-42）；多节点/Agent 灰度、兼容升级图、干净机安装及断电恢复待补/验收 | [release_manager.py](../../backend/app/runtime/release_manager.py)、[CLI](../../scripts/release_manager.py)、[发布设计](../operations/release-lifecycle.md) |
| F27 | MES 嵌入与业务联动 | 暂缓 | SSO/票据、业务上下文、角色映射和事件回写继续排除本期 | 原蓝图 F27、此前用户范围约定 |

<details>
<summary>2026-09-22 历史快照（旧判断不作为当前状态）</summary>

| F | 模块 | 上次结论(9-08) | 本轮状态(09-22) | 差异 | 本轮证据 / 复核结论 |
| --- | --- | --- | --- | --- | --- |
| F01 | 工作台与运营总览 | 主链路具备 | 主链路具备 | 一致 | `api/v1/system.py:get_dashboard_stats` → `operations_dashboard.build_operations_dashboard`(:126)，实查 6 张表。入参无组织/负责人筛选，`env_type` 只回显不过滤 |
| F02 | 五层资产与 CMDB | 主链路具备 | 主链路具备 | 已改善 | `normalized_service.apply_correction` 含 `split_cluster`(:1280)/`merge`(:1312) 且写审计；新增 `cmdb/replication.py`、`topology_relations.py` 已接入，`replicates_to` 区分 `role_derived`/manual（D06 部分改善）。外部 CMDB 冲突仍待实机 |
| F03 | 实例接入与凭据 | 主链路具备 | 主链路具备 | 已改善 | `services/connection_security.py` 为新增（HEAD 无）；`mysql_connection.py:98-119` 注入 ssl_ca/cert/key，`postgresql_connection.py:36` 设 verify-full。**仅 MySQL/PG 命中**，Oracle/SQL Server 未见。目标版本/最小权限仍待实机 |
| F04 | 集群与高可用 | 部分实现 | 部分实现 | 一致 | `ha_diagnostics._action_readiness`(:172) 返回 `mode="read_only_plan"`，动作全为 plan_only/manual_runbook 并明示不执行切换；平台到独立 HA 引擎无审批桥接与结果回写 |
| F05 | 巡检模板与指标库 | 部分实现 | 部分实现 | **已建未接线** | 新增 `models/inspection_template_version.py` + `services/inspection/template_versions.py`（`capture_version`:162/`restore_version`:234），但**仅被迁移脚本引用，业务零调用**（`policy_service` 增删改克隆不产生版本行）；仍无 sign/verify 符号 |
| F06 | 巡检执行与报告 | 部分实现 | 部分实现 | **已建未接线** + D04 已修 | 新增 `models/inspection_job.py` + `services/inspection/jobs.py`（enqueue/cancel/claim/heartbeat/reap，含 lease/cancel_requested/attempt），**仅被测试引用**（`test_inspection_job_target_snapshot.py:17`）；生产路径 `tasks/inspection_scheduler.py:40` 仍同步调 `runner.run_unified_inspection`。D04（失败报告计成功）已修：`inspection_scheduler.py:43-58` 按 `task_status` 汇总 |
| F07 | 指标监控与容量 | 部分实现 | 部分实现 | 一致 | `DBCapacityPolicy` 无持续时间与独立恢复阈值字段；`_resolve_capacity_alerts`(:359) 仅按是否仍在 active_keys 置 closed，无滞回。采集/预测主链完整 |
| F08 | 告警与事件 | 部分实现 | 部分实现 | 一致 | 生命周期/维护窗/SLA/动作记录齐（:481/:509/:579/:646/:682）。断点：`DBAlertActionLog.next_retry_time`（models/alert_event.py:73）**有写无消费**——`tasks/` 下无扫描者，`retry_action` 只把状态回置 pending |
| F09 | 多源可观测与数据质量 | 部分实现 | 部分实现 | 一致 | `evaluate_data_quality`(:202)/`run_retention`(:301) 仅被 `api/v1/observability.py:355/363/397` 手动调用；`master_scheduler.run_due_tasks` 的 10 个 job 中无质量评估/保留消费者，策略模型也无频率字段 |
| F10 | 性能分析与优化 | 部分实现 | 部分实现 | 已改善 | `_collect_object_snapshot` 无采集器时返回 `status="unsupported"` 并附明确文案，不再是静默空值。但 collectors 仍只有 `oracle`/`mysql` 两键——**如实声明不支持，不等于已支持** |
| F11 | AWR 与四引擎深度报告 | 部分实现 | 部分实现 | 已改善 | `collect_report`(:852) 对非 AWR 源指定时间窗直接 `raise ValueError("unsupported: 不能用当前累计值代替指定时间窗")`，D07 实质处理；新增 `workload_window.py:apply_window`/`_pg_difference` 做 PG 差分。**仍缺**：`window_minutes` 仍不传 mysql/pg/sqlserver 采集器，MySQL 累计差分与统计重置未见 |
| F12 | 会话、事务与阻塞 | 查询/方案主链路具备 | 部分实现 | 一致（口径收紧） | `generic_db_ops.py` 返回 `plan_only=True`；PG `kill_session` 仍为 `SELECT pg_terminate_backend(<pid>);`、SQL Server 仍 `KILL <session_id>;`——**占位符未绑定**。查询主链具备，处置仍无实际执行 |
| F13 | 空间、对象、参数与日志 | 部分实现 | 部分实现 | **D05 已修** | `automation/service.py:48` 已注册 `"database": DatabaseRollbackExecutor(db)`，回滚经 `prepare_rollback`(:438)→`prepare_fields` 走通（主代理已复核确认）。非参数对象治理仍为人工方案 |
| F14 | 数据治理与归档 | 部分实现 | 部分实现 | 一致 | `data_governance_execution.run_recovery_test` 全流程为下载→`_sha256_file`→`_read_archive_rows`→`_sample_hash`→行数核对，**无目标库连接或 import**。恢复 CLI 在 `scripts/` 下仍未找到 |
| F15 | 备份管理 | 部分执行主链路具备 | 部分实现 | 已改善 | `_sqlserver_script` 已生成 bcp/schema/full/diff/log/copy_only/validate 脚本，但 `get_operation_capability` 对 sqlserver 仍 `executable=False`（:546 文案：仅生成方案，真实执行驱动未验收）。`retention_preview` 仍硬编码 `delete_executed: False` |
| F16 | 恢复、校验与演练 | 部分实现 | 部分实现 | **D01 已修** + 危险路径已阻断 | MySQL/PG 的 restore/pitr 现直接 `status="blocked"`（`LOGICAL_RESTORE_BLOCK_REASON`），`_recoverability`(:304) 把历史同实例 probe 排除出可恢复性证据。探测库名改 `mg_restore_{uuid4().hex}`(:1376/:1459，主代理已复核确认)。**仍缺**：跨实例隔离恢复、实际 PITR、业务一致性 |
| F17 | 数据抢救 | 治理主链路具备 | 部分实现 | 一致 | `backend/app/tasks/*.py` 无 recovery 消费入口，`master_scheduler` 未调度；`agent/internal/application/app.go` grep `recovery` 零命中。仅有模型与治理 CRUD，**无实际驱动扫描/预览/导出的 worker** |
| F18 | 自动化审批与执行 | 部分实现 | 部分实现 | **D05 已修** | `automation/executors/database.py` 新增 `DatabaseRollbackExecutor`（`prepare_fields`/`validate_replay`/`finalize`），已注册。**仍缺**：`service.py` 无 step/pause/resume/stream/progress 符号，`automation/` 下无步骤模型；执行结束才一次性写 `ExecutionOutput` |
| F19 | AI、规则与诊断评测 | 主链路具备 | 主链路具备 | 一致 | `api/v1/ai.py:19` → `diagnosis/orchestrator.run_diagnosis`(:150) → `evidence_chain.build_evidence_chain`(:523) → `ai_orchestrator`，链路完整。未修：`_rule_predictions`(:361) 仍按输入已有 `cause_code` 聚合（非 RCA 盲测），`estimated_cost=0.0`(:696) 仍写零 |
| F20 | Incident Snapshot | 主链路具备 | 主链路具备 | 已改善 | `incident_snapshot/ingest.py` 的 `initialize_upload`(:161)/`store_chunk`(:236)/`resume_upload`(:136)/`complete_upload`(:1226)/`analyze_snapshot`(:798) 齐备——断网重传已有实现。千台规模与负载成本仍无验收数据 |
| F21 | 状态智能引擎 | 部分实现 | 部分实现 | 已改善 | `kernel_observability/service.py:764` 已调 `state_engine.service.ingest_observation`（eBPF 桥接已通）。但全仓**只有这一个调用方**，普通监控/Snapshot 无桥接入口；`_match_persisted_patterns`(:832) 构造 `FaultPatternMatch` 时仍未传 `incident_id`（字段存在但恒空） |
| F22 | 深度现场重建 | 部分实现 | 部分实现 | 已改善 | `scene/manager.go:197 ApplyProbe` → `platform_linux.go:160 platformProbe` 真实执行 `procfs.open_files`、`jcmd Thread.print`（Linux 动态打印已实现）。未修：`platform_windows.go:47` 仍硬编码 `stack_quality: "unavailable_without_etw"`；**引用链仍断**（无上传接收路由、无符号化代码，`service.py:595` 显式把有制品引用的任务置 `partial`） |
| F23 | Linux eBPF | 部分实现 | 部分实现 | **空成功已修** | `kernelobs/runtime.go:16 noopRuntime.Start` 非 E0 已返回错误（主代理已复核确认）；`runtime_linux.go:35` 阻塞等 loader `ready`（10s 超时），`main_linux.go:462` 无一 hook attach 时报错退出。未修：`loaded_sensors` 仍回显请求值而非真实 attach 集合；USDT/用户态符号/容器关联未见；CO-RE 实机与长稳 Gate 无证据 |
| F24 | 通知与 ITSM | 部分实现 | 主链路具备 | **D03 已修** | `notify/service.py:570 dispatch_report_subscription` 已按 `subscription.channels/recipients` 路由（主代理已复核确认）；`consume_due_notifications`(:766) + `tasks/notification_scheduler.py` 构成定时重试消费者。剩余为企业系统实机契约验收，属验收而非功能缺失 |
| F25 | 报告、审计与调度 | 主链路具备 | 主链路具备 | **D03 已修** | `report_center._dispatch_subscriptions`(:1147) 在制品生成后调 notify 分发；签名验真 `sign_artifact/verify_artifact`(:160/:174)、`execute_schedule`(:1017)、`claim_scheduler_lease`(:1362) 齐备。`archive_artifact`(:718) 仅改状态与 URI，无冷热迁移 |
| F26 | 平台管理、企业安全与版本交付 | 主链路具备 | 部分实现 | 已改善 | `release_manager.py` 新增 `inspect_transaction`/`transaction_history`/`recover`(:1315-1478) 与 `install_patch`(:2129)——持久化升级事务已具备。但 `ReleaseManager` 仅被 `runtime/database_migrations.py:18` 导入，**无 Release Center API/前端入口**；`_bootstrap_shared_config`(:1227) 只拷模板不是配置合并 |
| F27 | MES 嵌入与业务联动 | 本期暂缓 | 暂缓 | 不适用 | 9-08 已明确排除本期，未复核 |

</details>

## 1.1 已知缺陷现状（9-08 标记的 D01-D07）

| 编号 | 缺陷 | 现状 | 证据 |
| --- | --- | --- | --- |
| D01 | 恢复探测在源实例删除固定名临时库，可能误删同名库 | **已修** | `backup_center.py:1376/:1459` 改为 `mg_restore_{uuid4().hex}`；且 mysql/pg 的 restore/pitr 已 `blocked`，该路径不可达 |
| D02 | 升级锁仅凭文件年龄回收，并发长升级可能被另一个更新器进入 | **已修（2026-09-22 变异验证）** | `_lock` 改用 `flock`/`msvcrt` 非阻塞 OS 锁；`lock_timeout_seconds` 退化为兼容保留，`release_manager.py:1211` 注释自陈"elapsed time never grants ownership of a live lock"。变异验证：①恢复"按文件年龄回收"→ 3 个用例红；②抽掉 OS 锁本体 → 6 个用例红 |

## 1.05 2026-09-22 修复回合

本轮不再是"登记待决"，而是动代码。每项都配了**变异验证**——把修复撤销或改坏，确认测试真的变红；
否则"测试通过"只证明它没报错，不证明它守住了属性。

| # | 原形态 | 处置 | 变异验证结果 |
| --- | --- | --- | --- |
| `DBAlertActionLog.next_retry_time`（原 §2.1 #3） | 有写无消费 | **已接线**：新增 `alert_lifecycle.retry_due_actions`（到期的 failed → 回 pending 并清除到期戳），经 `tasks/alert_action_scheduler.py` 挂入 `master_scheduler`；`GET /alert-events/actions` 增加 `action_status` 过滤，外部执行端可凭 `pending` 领取 | 双闸门同时放宽到期判定 → 1 红；不清除到期戳 → 1 红；从注册表摘除 → 1 红 |
| `template_versions`（原 §2.1 #2） | 已建未接线 **＋ 真崩溃** | **比登记时更严重**：`restore_version` 以 `expected_revision=` 等 5 个关键字调 `update_policy()`，而该函数签名只收 3 个参数 → 任何版本回滚必抛 `TypeError`。已给 `update_policy` 补上版本参数，并在提交前 `capture_version` | 复现测试先红（TypeError 原文进测试注释）；撤销 capture → 1 红 |
| 版本历史拖垮编辑（本轮修复引入） | 自我保护 | 首版让 `update_policy` 无条件写版本行，导致未执行 v20260908_03 迁移的库在**所有**模板编辑上报 `no such table: inspection_template_version`。改为 `version_history_available()` 探测，缺表时告警跳过 | 去掉降级 → 1 红（`test_edit_survives_missing_history_table`） |

两点教训（写进 §4 注意事项）：

1. **新增一个 job 会让三处测试资产同时过期。** 两个测试文件各自维护不同的 job 打桩列表，
   其中一处还依赖"打桩顺序 == 注册表顺序"这个巧合——两者顺序本就不同，只是恰好让
   `started[-1]` 落在 notification 上。现已改为按 `patcher.temp_original` 的模块+限定名定位，
   计数断言改为跟随 `due_job_functions()`，另补一条成员断言堵住"计数跟随"看不见的删除操作。
2. **接线必须定义到"有人消费"为止。** `retry_due_actions` 把状态翻回 pending 本身毫无用处：
   若外部执行端拉取不到 pending，它就只是一台自娱自乐的状态翻转机。
   外部领取通道（`action_status` 过滤）因此是这次接线的一部分，不是可选项。
| D03 | 报告订阅的渠道/收件人未参与实际分发；通知重试缺定时消费者 | **已修** | `notify/service.py:570` + `:766` + `tasks/notification_scheduler.py` |
| D04 | 巡检返回失败报告时，定时策略仍可能记录成功 | **已修** | `tasks/inspection_scheduler.py:43-58` 按 `task_status` 汇总 |
| D05 | 配置整改回滚用 `database` 执行器，但默认自动化执行器无该实现 | **已修** | `automation/service.py:48` 注册 `DatabaseRollbackExecutor`（主代理已复核确认） |
| D06 | 拓扑连线由角色推导而非真实复制边 | 部分改善 | 新增 `cmdb/replication.py`、`topology_relations.py` 区分 `role_derived`/manual；前端是否采用**未复核** |
| D07 | AWR 指定时间窗未进入采集器，用累计值冒充窗口值 | **已修** | `workload_report.py:852` 对非 AWR 源直接 `raise ValueError` |


## 1.06 2026-09-23 修复回合

本轮继续动代码。两项修复 + **一项修复过程中自己引入并当场抓出的回归**，全部配变异验证。

| # | 原形态 | 处置 | 变异验证结果 |
| --- | --- | --- | --- |
| F19 `estimated_cost`（§2.1 #8） | 硬编码零 | 两层都存在该字段、`prompt_tokens`/`completion_tokens` 也都在记，但 `case` 写死 `0.0`、`run.estimated_cost` **从未被赋值**。现按 token 计算并落库；新增 `cost_status` 区分四种情况：`calculated`（命中价格表）／`calculated_with_default_rate`（回落默认单价）／`usage_unreported`（provider 没报用量，**不是免费的 0**）／`call_failed`／`not_applicable`（rule 模式确实没调用） | 成本写回零 → 3 红；抽掉 run 汇总 → 2 红；未报用量也标 `calculated` → 1 红；调用失败不标记 → 1 红；默认单价伪装精确 → 2 红 |
| 模板版本只覆盖 `update`（§2.1 #2） | 三条路径缺版本行 | `create` 落 `create`；`clone` 落 `clone` 并记 `source_policy_id`+`source_revision`（血缘）；`delete` 由新增 `policy_service.delete_policy` 落 `delete` 墓碑（版本表 `policy_id` 本就无外键，设计上允许历史比行活得久） | 分别摘除 create／clone／delete 的捕获 → 各 1 红 |
| `version_history_available()` 回滚未提交事务 | **本轮自己引入** | 该函数经 `sa_inspect(db.get_bind())` 探测，走 engine 会**另开一条连接**，关闭时把共享连接上未提交的事务回滚掉——`create_policy` 是先 flush 再探测，于是待插入的行被丢弃，commit 时炸 `StaleDataError: expected to update 1 row; 0 matched`。改为 `db.connection()` 用会话自己的连接 | 改回 `db.get_bind()` → 2 红（并复现 `StaleDataError`） |

### 1.06 的教训

1. **"先 flush 再探测"是一个陷阱组合。** 同一句探测在 `update_policy` 里安然无恙（行早已提交，UPDATE 仍匹配得到），
   移到 `create_policy` 就炸。这解释了为什么它在上一轮没被发现——不是没测，是**没在"有待提交写入"的位置测过**。
2. **23 个既有测试同时变红，不要急着去改测试。** 第一反应若是"测试资产过期"，就会把真缺陷改写成期望值。
   这里的正确顺序是：先做对照实验（摘掉自己的改动看是否恢复），确认是自己引入，再往下挖根因。
3. **变异必须语法正确才叫证据。** 本轮有一次把注释插进了未闭合的括号里，测试确实报错——但那是 `SyntaxError`，
   证明不了任何东西，只能重做。

## 1.07 2026-10-19 修复回合（巡检队列接线）

| # | 原形态 | 处置 | 变异验证结果 |
| --- | --- | --- | --- |
| `services/inspection/jobs.py`（§2.1 #1） | **已建未接线**：302 行队列（enqueue/cancel/claim/heartbeat/commit_results）零生产引用，定时巡检直接同步调 `run_unified_inspection` | **已接线**。定时巡检改走队列：`enqueue → claim → execute → commit_results`；`jobs.execute` 负责认领后跑完并落到终态，`lease_guard` 用独立会话续租 | 队列只调 `collect_findings` 而跳过阈值 → 1 红；调度绕过队列直调 runner → 1 红；抽掉 `lease_guard` → 1 红 |

### 接线时必须一起搬的三样东西

1. **阈值绑定与策略上下文**。`run_inspection` 在采集之后做 `_apply_policy_thresholds` 与 `_attach_policy_context`，
   这两步改的是 findings 本身（95% 会从 normal 变成超阈）。已抽成 `runner.prepare_findings`，直连与队列共用一份，
   并由 `test_queue_path_applies_policy_thresholds` 端到端守住——只复用采集器的实现会让这个测试红。
2. **租约**。`LEASE_SECONDS = 45`，而巡检经常跑超过 45 秒。没有 `lease_guard`，`commit_results` 会以"租约过期"拒绝结果，
   运维看到的是"失败"，但巡检其实成功了——这比真失败更糟，因为它对结果撒了谎。
3. **host 模板的 db_type**。host 作用域的策略会用合成目标执行，`jobs.db_type_for` 必须与 runner 的判定一致，
   否则 `enqueue` 直接以"实例与巡检数据库类型不匹配"拒收。

### 代价

- 16 个既有测试挂在旧接缝（`run_unified_inspection`）上。逐条确认意图后改到新接缝（`jobs.execute`），
  **没有删除任何一条断言**——"调度按执行状态而非报告有无判定"这一条尤其保留。
- `task_id` 由时间戳改为 job id：已确认全仓无按 task_id 解析时间、无按 task_id 排序，属安全变更。

## 1.08 2026-10-19 收口回合（#4/#5/#8 与两项边界决定）

| # | 原形态 | 处置 |
| --- | --- | --- |
| §2.1 #4 Release Center | 升级事务完整实现但无任何入口 | 新增只读 API `GET /api/v1/release-center/status`（活动版本、历史、未完成事务、阻塞状态）与 `GET /releases/{id}/manifest`。**有意不开放 install/rollback**：那等于把"执行请求指向的包"暴露成远程代码执行，签名私钥在运维机上而非 Web 进程内。变更操作留在安装/升级 CLI |
| §2.1 #5 SQL Server TLS | `certificate_profile_id`/`trust_server_certificate` 被静默忽略 | 改为**显式拒绝**：pymssql 只提供 `encryption` 级别，没有 CA/客户端证书/主机名校验。带着证书配置连接会让人以为通道被钉住，实际只是加密——拒绝优于降级。Oracle 的 wallet 路径复核后确认本就已接线，原登记"未见"不成立 |
| §2.1 #8 盲测口径 | 规则通道复述标注答案，分数却当盲测用 | 新增 `rule_channel_basis`（`label_assisted`/`blind`），按 case 记录并在 run 详情聚合。**没有**伪造一份关键词词表去把分数救回 0.8——那只是把作弊换个形式；真实盲测需要独立原因词表，明确列为未做 |

### 两项边界决定（不再挂问号）

- **F22 深采集引用链**：接受"制品留 Agent、平台只存引用"为终态，不补上传与符号化。
  火焰图/堆转储含进程内存内容，集中上传与最小暴露原则冲突。
- **F17 抢救 worker**：外置执行端交付，平台不内嵌。抢救依赖物理介质与厂商工具链，
  长期在线进程既无设备权限也不该持有高危写操作。平台侧只承诺任务下发、证据回传、结果复核。

## 1.09 2026-10-19 第二回合（按台账修 6 项）

台账[已知问题台账](../known-issues.md)建成后，按它逐项修。六项全部配变异验证；后端全量 **2759 passed / 0 failed**。

| # | 原形态 | 处置 | 变异验证 |
| --- | --- | --- | --- |
| ISS-20 | 能力目录自陈备份/恢复全引擎 `ready`，备份中心实际拒绝 | 改为**派生**：`protection_maturity()` 读 `backup_center.get_operation_capability`；新增 `blocked` 成熟度，可用性直落 `blocked`；catalog 里这两条 `maturity=None`，没有来源就报错 | 派生函数恒返回 ready → 4 红；去掉 blocked 可用性分支 → 1 红 |
| ISS-21 | 定时巡检已入队，手动 API 等四处仍直连 runner | 新增 `inspection/dispatch.py`，四处统一 `enqueue→claim→execute`；报告按 job id 取 | 新增 **AST 断言**（任何生产模块直接调 `run_unified_inspection` 即失败）；API 改回直连 → 1 红 |
| ISS-22 | 数据质量评估/保留只被手动 API 调用 | 新增周期消费者并挂注册表；策略补四个调度字段与迁移；**保留默认只出计划不删除** | 保留改为默认执行 → 1 红 |
| ISS-23 | 版本写了四类路径，没有任何读取入口 | 三个 API（列表/详情/恢复），恢复需 `expected_revision` 乐观锁；表缺失时降级 | 忽略 expected_revision（自动取当前）→ 1 红 |
| ISS-25 | 容量策略只有 `consecutive_failures`，一个样本开关告警 | 新增 `sustained_minutes`（未持续不发布，`notified_at` 为空）与 `recovery_margin_pct`（滞回带内不关闭）；默认值保持原行为 | 禁用滞回 → 1 红；禁用持续时间 → 1 红 |
| ISS-42 | 升级只复制一次配置，新版本新增项静默缺失 | `runtime/config_merge.py` 三方合并，base 为上次写入的模板快照；本地改动永远优先，冲突上报，写前备份 | 改回 copy-once → 3 红 |

### 本轮教训

1. **半吊子接线是自己上一轮造成的。** ISS-21 的四个调用点里有一个就是我上一轮刚接好的定时入口的兄弟分支。
   "修好一条链路"和"修好这类链路"的区别，就在于有没有反向枚举同形态调用点。现在这条由 AST 断言机器化。
2. **滞回判定不能重算告警类型。** 第一版 `_hold_keys` 对每个 normal 样本重算 alert_type，
   而"未超阈"按定义会被算成另一类，key 永远对不上原告警，滞回静默失效——测试红才发现。
   正确做法是以**已存在的告警**为基准，而不是以当前样本推导。
3. **契约基线必须手术式更新。** 本轮新增 3 个 operation、1 个 schema、3 张表变更；`--update` 会把他人漂移一起固化。
   改成只折叠自己这几项，并断言"除此之外零漂移"。另外基线文件是 `sort_keys` 序列化的，
   用错参数会把整份文件重排成 2.6 万行 diff。

## 1.10 2026-10-20 第三回合（按源码盘点工作包修 5 项）

上游是[全功能盘点](functional-inventory.md)与[详细待开发清单](pending-development-backlog.md)（59 个工作包）。
本轮只取用户点名的五项：巡检旁路（U05）、模板冻结（M06）、审批身份（M17①）、实际停止确认（U07）、结果/报告一致性（M39）。
后端全量 **2788 passed / 0 failed**，`check_all --fast` 全通过。

| 项 | 修复前 | 修复后 | 变异 |
| --- | --- | --- | --- |
| U05 | 六处 `run_inspection` 直调（诊断/规则/操作 AI/AI 接口/两个旧引擎页） | `dispatch.run_collecting_rows` 统一入队；AST 护栏同时禁两个符号 | 诊断改回直调 → 护栏红 |
| M06 | `execute` 读当前 policy，冻结快照只做完整性校验 | `jobs.frozen_policy(row)` + `resolve_execution_policy(frozen=True)` | 改回读当前 policy → 执行内容断言红 |
| M17① | 身份字段全部取自请求体 | `actor.py` 从 Bearer 解析，九端点挂依赖，自批拒绝 | 退回请求体 → 4 红；摘依赖 → 2 红 |
| U07 | `cancel_capture` 的 ACK 无分支，任务永停 dispatched | 仅确认后置 `cancelled`，未确认写明『可能仍在进行』 | 屏蔽分支 → 2 红 |
| M39 | Jira 无 transition 也返回 Closed，连带关源告警 | 提交后回读，五种 outcome，空状态不再补写 Closed | 回读恒返回 Closed → 3 红；补写 Closed → 1 红 |

### 本轮的三个教训

1. **『禁止直调』的护栏按符号枚举，就必然漏。** 上一轮只禁 `run_unified_inspection`，而真正被隐式调用者使用的是返回行的
   `run_inspection`——两个入口、一个语义。护栏应该禁止**能力**，而不是禁止某个名字；改完之后要把同族符号一次性收进集合。
2. **冻结快照存了不等于用了。** snapshot+hash 看起来像『已冻结』，但执行时按 id 重新解析，等于把冻结降级成『事后校验』。
   判断标准是：**执行输入是否真的来自快照**，而不是『快照是否存在且 hash 对得上』。
3. **ACK 与业务完成之间的那段路最容易空着。** 发命令、收命令都有代码，唯独『命令被确认后回写业务状态』没有分支。
   这类缺口测试全绿也看不出来，只能靠逐条对照命令类型与状态机。

## 2. 复核任务

- [x] T1 复核 F01-F09
- [x] T2 复核 F10-F18
- [x] T3 复核 F19-F26
- [x] T4 回填状态表
- [x] T5 更新 `docs/README.md` 能力全景表（挂 F 编号）——2026-09-22 完成
- [x] T6 接入门禁并跑全量回归——2026-09-22 完成（五个门禁接入 `check_all --fast`）

## 2.1 本轮暴露的后续项

复核过程中发现的、需要产品/架构决定的问题。**这些不是缺陷清单，是待决项**——
其中"已建未接线"一类尤其需要先决定是接线还是删除，否则代码会长期处于"存在但不生效"状态。

下表 10 项**已全部收口**（10-19）。编号与归宿统一登记在[已知问题台账](../known-issues.md)，
本表只保留收口结论，不再两处维护同一份清单——两处漂移本身就是一种文档缺陷。

| # | 项 | 形态 | 待决问题 |
| --- | --- | --- | --- |
| 1 | `services/inspection/jobs.py` | **已接线（10-19）** | 定时巡检改走持久化队列（enqueue→claim→execute→commit_results），租约续期、取消、重试、进度对定时任务生效。见 §1.07。F06 的"持久化任务队列"缺口关闭 |
| 2 | `services/inspection/template_versions.py` | **四类路径全部覆盖（09-23）** | 见 §1.06：`create`/`update`/`clone`/`delete` 均落版本行，clone 带来源血缘，delete 留墓碑。**同时修掉一个会静默丢数据的真缺陷**：`version_history_available()` 经 engine 探测会回滚调用方未提交的事务 |
| 3 | `DBAlertActionLog.next_retry_time` | **已接线（09-22）** | 新增 `retry_due_actions` 消费者并挂入 `master_scheduler`；`GET /alert-events/actions` 支持 `action_status` 过滤供外部执行端领取 |
| 4 | `runtime/release_manager.py` | **已收口（10-19）** | 只读 Release Center API 已交付（ISS-06）；install/rollback **有意不开放**——把"执行请求指向的包"经 Web 暴露等于远程代码执行。剩余无前端页面属缺口，见 ISS-41 |
| 5 | F03 Oracle/SQL Server TLS | **已收口（10-19）** | 原登记"另两个引擎未见"**证伪**（ISS-19）：Oracle wallet 已接线、SQL Server 已有 `encryption=` 分级。真正的缺口只剩 SQL Server 未接 PEM 证书材质，已改为**显式拒绝**（ISS-07）——拒绝优于降级：带证书配置却只做加密，会让人以为 CA 被钉住。补材质不在本期 |
| 6 | F22 深采集引用链 | **已定：接受为终态（10-19）** | 决定「制品留 Agent、平台只存引用」为设计边界，**不补**上传接收路由与符号化。理由：火焰图/堆转储体积大且含进程内存内容，上传到平台等于把主机内存数据集中化，与最小暴露原则冲突；引用链已能定位到制品与主机。`service.py` 的 `partial` 标注保留，不再作为待办 |
| 7 | F23 `loaded_sensors` | **复核后判为「平台侧无缺陷」，待 loader 侧配合** | 09-23 复查结论与登记时不同：Go 侧 `manager.go` 的四条失效路径（启动失败 `:279`、kill switch `:305`、过期 `:565`、loader 退出 `:498`）**都会清空 `state.Sensors`**，因此回显值不会在失败后仍谎报已加载。真正的缺口在 loader 二进制：它不上报实际 attach 集合（全仓 `grep -i loaded` 在 loader 协议里零命中），平台拿不到真实值。**要修得改 loader 协议，不是改平台** |
| 8 | F19 评测口径 | **成本已修（09-23）；盲测口径已披露（10-19）** | `estimated_cost` 曾硬编码 `0.0` 且 `run.estimated_cost` 从未被赋值，现按 token 计算并区分"算出来是 0"与"根本没算"（§1.06）。规则通道读输入 `cause_code` 导致准确率非盲测的问题，本轮**不再沉默**：run 记录 `rule_channel_basis`（`label_assisted`/`blind`），标注参与的分数不会被当作盲测结果（§1.08）。仍未补独立原因词表——见 §1.08 的理由 |
| 9 | ~~D02 升级锁~~ | **已闭环（09-22）** | 见 §1.1 与 §1.05：实现已是 OS 锁，且经两次变异验证非"看起来有" |
| 10 | F17 抢救 worker | **已定：外置交付，平台不内嵌（10-19）** | 决定由外置执行端交付，平台只保有任务编排与验收证据。理由：抢救依赖物理介质/挂载与厂商工具链，内嵌进 Web 进程既拿不到设备权限，也会把高危写操作放进长期在线进程。平台侧验收条件是「任务可下发、证据可回传、结果可复核」，不承诺 worker 本身 |

## 3. 复核方法（避免把快照当现状）

1. 以 9-08 审计给出的**代码入口**为起点，读当前工作区代码（不是 HEAD——HEAD 仍是 9475158，
   落后两周）。
2. 行号必然漂移，**不要按行号找，按符号找**（函数名/类名/路由）。
3. 判定"具备"要有执行路径：入口 → 服务 → 数据落库/返回，中间不能断。
   **代码存在但生产路径未调用 = 未接线，不计入具备。**
4. 判不出来就写**未复核**，不要降级成"部分实现"。

## 4. 动代码时的注意事项（2026-09-22 修复回合得出）

1. **接线要接到"有人消费"为止。** 把状态翻回 `pending`、写一行历史，都不算闭环——
   必须能指出谁读它、怎么读（API 过滤条件／定时任务／前端入口）。写不出这条链路，就等于没接。
2. **测试里的位置索引要能追到源头。** 一排补丁对象里用 `started[-1]` 取"最后一个任务"，
   取到的其实是"最后一个补丁"；两者顺序不一致时，断言会静默落在错误的函数上。
   改为按 `patcher.temp_original` 的模块+限定名定位；计数断言跟随注册表，
   并另配一条成员断言——因为"跟随注册表"的计数**看不见删除**（两边同减仍然相等）。
3. **给共享写入路径加新副作用前，先问缺半边会怎样。** 新增版本行会让同期所有调用方依赖那张表；
   未迁移的库不该因为历史功能不可用就被拒绝编辑主链路。
4. **变异必须写成语法正确的改动。** 有一次把内联替换改成了缩进错误，测试确实红了，
   但那是 `IndentationError`，证明不了任何东西，只能重做。
