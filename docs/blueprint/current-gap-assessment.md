# 当前蓝图差距

核对日期：2026-10-08。范围：`mesdbtools` 当前工作区与 `orchestrator-enterprise/docs/database-operations-blueprint.html` 的 F01-F27 功能要求。

2026-10-20 最新细化：[全功能源码盘点](functional-inventory.md)和[详细待开发清单](pending-development-backlog.md)已覆盖 F01-F26 主要操作族及菜单映射。本文保留模块视图/历史修复；发生冲突时以新盘点的具体源码依据为准，尤其：巡检仍有旧/诊断直调旁路、冻结配置执行未闭合；PG 差分函数不等于真实采集接线；通知/ITSM 有具体配置未消费和结果核验缺口，不是整体仅待实测。模块数不变，MES 仍暂缓。

2026-10-15 专项补充：诊断真值、实例身份、AWR 时效、状态升级/断采/重复链、可信内核链事故关联及模型传输边界的修复见[优化记录](../archive/acceptance/2026-10-15-model-architecture-optimization.md)。本批不改变模块完成数；F06 持久 worker、F21 普通监控/Snapshot 状态桥接、F25 全链 fencing 和 F19 外发授权/盲测仍开放，未部署验收。

2026-10-19 补充：本表的"仍有差距"列属**问题登记**，逐项编号、归宿与严重度统一见[已知问题台账](../known-issues.md)（ISS-01…ISS-52）；本表保留模块视角，两处描述同一事实时必须同步改。本轮修复了已建未接线与静默错误一类（ISS-01…ISS-15），功能缺口仍未补齐。

本次读取蓝图、对齐计划、后端入口与服务、前端路由与相关页面、Agent 采集与执行代码。分支为 `codex/menu-optimization`，HEAD 为 `9475158`；大量实现仍在未提交工作区，不能用 HEAD 代表现状。

## 判断口径

- **主链具备**：指定功能已有入口、处理与结果路径；整个蓝图模块仍可能有缺项。
- **部分实现**：已有子能力，但缺执行器、接线、适配或产品入口。
- **待实测**：源码存在，本次未调用外部系统，无法证明现场可用。
- **暂缓**：MES 交互按此前范围继续暂缓。

按模块主链划分，7 项主链具备、19 项部分实现、1 项暂缓。这个数量不是功能完成率，也不是生产验收率；不按文件数、接口数或测试数量计算百分比。

## F01-F27 对照

下表的状态评价源码实现。客户部署、真实外部系统和故障演练的验收状态另列。

| 编号 | 模块 | 源码现状 | 仍有差距 | 核对入口 |
| --- | --- | --- | --- | --- |
| F01 | 工作台与运营总览 | 主链具备：指标、风险与工作队列 | 组织、环境、负责人等筛选未完整进入查询；多个来源仍全表读取后在内存处理，大规模成本待测 | [operations_dashboard.py](../../backend/app/services/operations_dashboard.py) `build_operations_dashboard` |
| F02 | 五层资产与 CMDB | 主链具备：发现、校正、关系及审计；前端已使用存储关系绘图 | 外部 CMDB 冲突、复杂级联及混合拓扑需现场验证；关系来源和真实探测覆盖需要继续核对 | [normalized_service.py](../../backend/app/cmdb/normalized_service.py)、[AssetTopology.js](../../frontend/src/AssetTopology.js) |
| F03 | 实例接入与凭据 | 主链具备：四引擎接入与连接分派 | 四引擎目标版本、最小权限、证书拒绝和网络异常矩阵待实测。Oracle Wallet 参数和 SQL Server encryption 参数已有注入，旧表中“未见”过时；参数存在不等于 TLS 已验收 | [oracle_connection.py](../../backend/app/collectors/oracle/oracle_connection.py)、[sqlserver_connection.py](../../backend/app/collectors/sqlserver/sqlserver_connection.py) |
| F04 | 集群与高可用 | 部分实现：拓扑、健康诊断及只读处置方案 | 缺平台审批任务到独立 HA 引擎、实时状态、执行结果回写的闭环；节点替换/退役及真实 RTO/RPO 验收仍需补齐 | [ha_diagnostics.py](../../backend/app/cmdb/ha_diagnostics.py) `_action_readiness` |
| F05 | 巡检模板与指标库 | 部分实现：默认/自定义模板、交换；四类路径均已落版本行；**版本列表/详情/恢复 API 已交付**（恢复带 `expected_revision` 乐观锁） | 前端页面入口仍缺；模板签发验签未接入。版本表缺失时写入降级跳过历史，因此版本能力还依赖部署迁移 | [policy_service.py](../../backend/app/services/inspection/policy_service.py) `_capture_history`、[template_versions.py](../../backend/app/services/inspection/template_versions.py)、[inspection.py](../../backend/app/api/v1/inspection.py) |
| F06 | 巡检执行与报告 | 部分实现：真实巡检、失败语义、证据与报告；**定时与手动入口均已走持久队列**（含租约续期），AST 断言禁止再直连 runner | 步骤进度、取消、任务级超时、重试与中断恢复未形成统一链路 | [inspection.py](../../backend/app/api/v1/inspection.py) `api_run_inspection`、[inspection_scheduler.py](../../backend/app/tasks/inspection_scheduler.py)、[jobs.py](../../backend/app/services/inspection/jobs.py) |
| F07 | 指标监控与容量 | 部分实现：采集、趋势、预测及告警；**已具备持续时间与恢复滞回**（`sustained_minutes`/`recovery_margin_pct`，默认保持原行为） | PulseWatch 融合没有本次集成验收证据 | [capacity_policy.py](../../backend/app/models/capacity_policy.py)、[capacity_monitor.py](../../backend/app/services/capacity_monitor.py) |
| F08 | 统一告警与事件 | 部分实现：生命周期、维护抑制；到期失败动作已自动重排 | 重排只把 failed 改回 pending，实际执行交给外部 worker；当前仓库未形成通用动作执行闭环，跨模块幂等与领取所有权仍需验收 | [alert_lifecycle.py](../../backend/app/services/alert_lifecycle.py) `retry_due_actions`、[alert_action_scheduler.py](../../backend/app/tasks/alert_action_scheduler.py) |
| F09 | 可观测与日志/APM | 部分实现：外部源查询和采集、手动质量评估与保留处理；**质量评估与保留已有周期消费者**（保留默认只出计划，需显式开启执行） | 日志/Trace 真实事件时间的新鲜度覆盖不完整。已有 observability 调度不代表质量调度已接通 | [data_quality_service.py](../../backend/app/services/observability/data_quality_service.py)、[observability_scheduler.py](../../backend/app/tasks/observability_scheduler.py) |
| F10 | 性能分析与优化 | 部分实现：基线、慢 SQL、SQL/表/索引画像及历史 | 专用 SQL 实时画像采集器、表画像和索引画像仍主要是 Oracle/MySQL；PG/SQL Server 需补齐。离线慢 SQL 记录接入不等于专用实时画像具备 | [perf_profile_collector.py](../../backend/app/services/perf_profile_collector.py) `SQL_PROFILE_LIVE_ENGINES`、`collect_table_profile`、`collect_index_findings` |
| F11 | AWR 与深度报告 | 部分实现：四引擎报告、历史、比较；Oracle 原生窗口与 PG 部分差分 | 非 AWR 指定历史窗口明确拒绝；MySQL 累计差分及 SQL Server Query Store 区间过滤仍需实现，不能把累计统计标成最近一小时 | [workload_report.py](../../backend/app/services/workload_report.py) `collect_report`、[workload_window.py](../../backend/app/services/workload_window.py) |
| F12 | 会话、事务与阻塞 | 部分实现：查询与处置方案 | PG/SQL Server 终止会话仍有占位符方案；目标绑定、审批执行与结果复验未接通 | [generic_db_ops.py](../../backend/app/services/generic_db_ops.py) |
| F13 | 空间、对象、参数与日志 | 部分实现：查询、配置基线、漂移、整改；数据库回滚执行器已注册 | 非参数对象维护、扩容及日志治理仍有人工方案；具体引擎和动作需逐项声明支持范围并实测 | [config_governance.py](../../backend/app/services/config_governance.py)、[automation/service.py](../../backend/app/automation/service.py) |
| F14 | 数据治理与归档 | 部分实现：导出、加密、文件验证和对象存储 | 恢复测试只做解密、解析、哈希和行数核对，未导入目标库；迁移/分区生命周期仍有方案；源清理禁用 | [data_governance_execution.py](../../backend/app/services/data_governance_execution.py) `run_recovery_test` |
| F15 | 备份管理 | 部分实现：RMAN、mysqldump、pg_dump 和策略执行 | SQL Server 真实执行驱动未接；XtraBackup、basebackup/WAL 等覆盖不足；保留清理、备份链保护和完整加密恢复链待补 | [backup_center.py](../../backend/app/services/backup_center.py) `get_operation_capability`、`EXECUTABLE_TYPES` |
| F16 | 恢复、校验与演练 | 部分实现：制品绑定与校验、恢复方案 | MySQL/PG 逻辑恢复因缺独立隔离目标被阻断；实际跨实例恢复、PITR、业务一致性、失败回退及真实 RTO/RPO 缺闭环；Oracle validate 不能计作恢复 | [backup_center.py](../../backend/app/services/backup_center.py) `LOGICAL_RESTORE_BLOCK_REASON`、[oracle_recovery.py](../../backend/app/services/oracle_recovery.py) |
| F17 | 数据抢救 | 部分实现：案件、授权、任务领取/回报协议和证据治理 | 缺实际领取任务并执行离线扫描、预览、导出的 worker；现有 Incident Agent 未接该协议。外置工具交付情况本次未核实 | [recovery/router.py](../../backend/app/modules/recovery/router.py)、[agent/internal/application/app.go](../../agent/internal/application/app.go) |
| F18 | 自动化审批与执行 | 部分实现：审批、SSH/Ansible、目标锁、取消及专用数据库回滚 | 数据库执行器目前为配置回滚专用，不能视为任意数据库动作通用执行器；缺步骤级暂停续跑、状态和实时输出 | [automation/service.py](../../backend/app/automation/service.py)、[database.py](../../backend/app/automation/executors/database.py) |
| F19 | AI 与规则诊断 | 主链具备：诊断编排、证据引用校验、规则降级；评测成本已计入 token 用量 | 规则评测直接聚合输入 cause_code，不能证明独立盲测准确率；任务级外发授权、全局调用预算和真实本地模型效果待补/实测 | [ai_orchestrator.py](../../backend/app/services/diagnosis/ai_orchestrator.py)、[diagnosis_evaluation.py](../../backend/app/services/diagnosis_evaluation.py) `_rule_predictions` |
| F20 | Incident Snapshot | 主链具备：基础采集、环形缓存、冻结、分片上传、续传与平台分析 | 千台规模、断网恢复、资源占用和存储成本需实际验收；基础线程信息不等于连续调用栈追踪 | [incident_snapshot/ingest.py](../../backend/app/incident_snapshot/ingest.py)、[snapshot/package.go](../../agent/internal/snapshot/package.go) |
| F21 | State Intelligence | 部分实现：识别、防抖、滞回、模式、回放及 eBPF 状态桥接；2026-10-15 已接通可信内核链的模式到事故/Context Pack 关联 | 普通监控/Snapshot 尚无完整自动输入桥接；手工/旧状态无可信来源时不补猜事故归属；真实 Agent 端到端、密集窗口规模和失联恢复仍待验收 | [state_engine/service.py](../../backend/app/state_engine/service.py) `_match_persisted_patterns`、[kernel/service.py](../../backend/app/kernel_observability/service.py) |
| F22 | 深度现场重建 | 部分实现：现场采集页面、审批、探针、Linux jcmd 动态打印及 Context Pack | 缺连续线程栈追踪、Windows ETW/WCT；深采集制品上传、解析、符号化与 AI 原文引用链不完整；本地产物引用仍返回 partial | [StateInvestigationPage.jsx](../../frontend/src/StateInvestigationPage.jsx)、[scene/service.py](../../backend/app/scene_reconstruction/service.py) `handle_command_ack`、[platform_windows.go](../../agent/internal/scene/platform_windows.go) |
| F23 | Linux eBPF | 部分实现：页面、策略预算、loader ready、动态 hook 与部分 CO-RE | ready 有 attach 数量和 core_sensors，但 loaded_sensors 仍来自请求集合，未按实际完整 attach 集合确认；USDT、用户态符号和完整容器关联需补；实机长稳 Gate 待验收 | [kernelobs/manager.go](../../agent/internal/kernelobs/manager.go)、[main_linux.go](../../agent/cmd/tracemind-ebpf-loader/main_linux.go) |
| F24 | 通知与 ITSM | 主链具备：通道、工单适配、订阅受众分发、通知重试 | 企业通知/工单字段、认证、回调、评论/关闭和升级规则仍需按实际接入系统逐项验收 | [notify/service.py](../../backend/app/notify/service.py) `dispatch_report_subscription`、`consume_due_notifications`、[itsm/service.py](../../backend/app/itsm/service.py) |
| F25 | 报告、审计与调度 | 主链具备：报告目录/签名/订阅、审计、调度租约与 epoch | epoch 仅在调度派发边界校验，未覆盖所有业务提交及外部副作用；任务类别仍串行；报告归档状态/URI 不等于冷热存储迁移 | [master_scheduler.py](../../backend/app/tasks/master_scheduler.py) `run_due_tasks`、[report_center.py](../../backend/app/services/report_center.py) |
| F26 | 平台管理与版本交付 | 部分实现：权限、认证、Secrets；原生/容器驱动、签名补丁、增量迁移、检查点与恢复 CLI；**Release Center 只读 API 已交付**；**共享配置已做三方合并**（本地改动优先、冲突上报、写前备份） | 无前端页面（ISS-41），install/rollback 有意不开放；多节点/Agent 灰度、兼容升级图、干净机安装及断电恢复待补/验收 | [release_manager.py](../../backend/app/runtime/release_manager.py)、[CLI](../../scripts/release_manager.py)、[发布设计](../operations/release-lifecycle.md) |
| F27 | MES 嵌入与业务联动 | 暂缓 | SSO/票据、业务上下文、角色映射和事件回写继续排除本期 | 原蓝图 F27、此前用户范围约定 |

## 必须纠正的能力声明

1. [通用能力目录](../../backend/app/services/operations_capabilities.py) 的 `protection.backup` 和 `protection.restore` 给四引擎统一设置 `maturity=ready`、`approval_required`；[备份中心](../../backend/app/services/backup_center.py) 却明确 SQL Server 仅生成方案、MySQL/PG restore/pitr 被阻断。两处声明存在确定的不一致，应让能力矩阵直接复用真实执行能力。
2. 模板版本“没有任何生产引用”已过时：四种修改路径已有 capture；但版本查看/恢复的用户入口仍未接通。留痕与可操作的版本管理应分别验收。
3. “告警动作到期无人消费”已过时：定时重排已接。它仍依赖外部执行 worker，不能据此宣称通知、工单、诊断等所有动作均已自动执行。
4. “F22/F23 没有前端入口”已过时：[AppPageRouter.jsx](../../frontend/src/AppPageRouter.jsx) 的 `diagnose.investigation` 已路由至状态与现场页，页内有现场采集、动态探针和内核观测 Tab。
5. 蓝图 HTML、Markdown 完成度视图和对齐计划已于 2026-10-08 同步本表，纠正模板留痕、数据库回滚执行器、AI 成本、发布检查点及页面入口等过时判断。功能要求和优先级保留；旧日期记录只用于追溯，不能代替当前复核。
6. 第 1 条的备份/恢复成熟度冲突**已于 2026-10-19 修复**（ISS-20）：成熟度改为从 `backup_center.get_operation_capability` 派生，并新增 `blocked` 状态。能力一致性门禁仍然只核对入口引用，不核对引擎执行能力——所以它依旧不会自动发现这类冲突，防的是文档，不是门禁。
7. "无 Release Center API" 已过时：只读 API 于 2026-10-19 交付，前端页面仍缺（ISS-41）。
8. "队列尚未接入" 只对一半成立：定时入口已入队，手动 API 未入队（ISS-21）。**半吊子接线比未接线更难发现**，因为它会让整条链路看起来已经改完。

## 补齐顺序与验收

| 批次 | 范围 | 交付与验收条件 |
| --- | --- | --- |
| P0-1 | F06/F25 持久任务与调度 | 手动、小助手、定时统一入队；独立 worker、步骤进度、取消、超时、心跳及中断恢复；重复请求不重复执行，晚到旧 owner 不提交结果；断网/重启后列表、详情、报告一致 |
| P0-2 | F15/F16 备份恢复 | 引擎/动作能力声明一致；独立恢复目标身份校验、实际导入/PITR、恢复链核验、业务抽样与 RTO/RPO；失败不影响源实例，形成真实演练报告 |
| P0-3 | F04/F18 高可用与自动化 | 审批后的平台任务调用 HA 引擎，进度与结果回写；每步目标、所有权、超时和验证条件明确；实际切换/接管后验证角色、复制边和业务连接 |
| P0-4 | F05/F08/F09 | 模板版本 API/页面/恢复及签发验签；告警动作执行 worker；数据质量与保留周期任务；取消/失败/重试有证据 |
| P1-1 | F10/F11/F12/F13 | PG/SQL Server 专用画像；各引擎真实区间统计及重置语义；具体会话处置与对象维护执行/复验，逐引擎声明范围 |
| P1-2 | F26 客户交付 | Release Center、配置合并、兼容图、多节点/Agent 灰度；原生与容器模式各做安装、升级、补丁、失败回滚、断电恢复验收 |
| P1-3 | F19/F20/F24 | 独立盲测与真实本地模型验证；Agent 规模/断网/资源测试；企业通知与 ITSM 实机合同测试 |
| P2 | F14/F21/F22/F23 | 归档真实恢复；监控/Snapshot 到状态及事故关联；深采集上传/解析/符号化；eBPF 实际 attach 集合和长期运行验证 |

优先级表示建议实施顺序；F07/F01 的阈值、筛选与查询成本可作为独立小批穿插。MES 交互不进入上述批次。

## 验证状态

- 本次已做源码与执行入口对照；未执行业务采集、备份恢复、故障注入、远程部署或平台迁移。
- 旧隔离回归证据不代表当前代码、数据库结构、已加载服务和客户环境一致。本次不沿用旧测试数量作为验收结果。
- 真实四引擎端到端、CI、当前安装包、外部 CMDB/通知/ITSM、本地模型和多机故障仍需单独验收；未取得本次证据的能力不得写“现场完成”。
- 本次更新蓝图 HTML、Markdown 完成度视图、对齐计划和文档入口；未修改业务源码、重启服务或提交 Git。
- 2026-10-08 文档同步复核：HTML 与两份 Markdown 当前 F 表均为 27 项，状态与缺口一致；本期优先级为 P0 18/P1 5/P2 3，排除 F27。文档链接、HTML 本地链接/锚点和格式检查通过。浏览器因本地 file 协议策略拒绝打开，未完成视觉复核；未用其他方式绕过策略。
- 文档链接/锚点检查、能力目录的路由/模块键一致性检查通过。后者只核对入口引用，不检查引擎执行能力，因此不会发现上述备份/恢复成熟度声明冲突。
