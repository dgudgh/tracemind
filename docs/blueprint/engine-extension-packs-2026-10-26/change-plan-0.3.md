# 0.2 → 0.3 具体变更计划

日期：2026-10-26。状态：**S1 已应用为 0.3 基线；S2–S4 待实施**。本文件保留本轮 7 项增量变更的决策依据；当前任务/合同/案例/追踪表以主目录 v3/0.3 为准。下文的产品实现工作包仍 planned。机器变更记录见 [change-plan-0.3.json](change-plan-0.3.json)。

交付分两步：先以一个完整变更集同步任务/合同/案例/检查器，形成 0.3 开发基线；再按下列归属实现代码和测试。已有 0.2 文档检查不能作为这些变更的实现或验收证据。M0 可以继续，M1 的底座与真实 DM 两条出口分别记账。

## 1. 任务依赖的确切改动

以下列表为完整硬依赖，不是只列被替换的一项。`[输出]` 是独立产物门禁，不要求提供方整个任务结束。

| 任务 | 0.3 硬依赖 | 改动目的 |
| --- | --- | --- |
| T03 | T01；正式生产交接另需 E07 | test-trust-ready 与 production-trust-accepted 独立签发 |
| T04 | T01、T02；T03[test-trust-ready]、T14a[runtime-ready] | 开发运行时不等待生产根实际交接 |
| T13a | T04、T05；T03[test-trust-ready] | 最小工具使用已认证测试签名合同 |
| T06a-core（取代 T06a 的事务部分） | T04、T05、T13a；T03[test-trust-ready] | 删除 T11；签名 fake Provider 的实际进程制品可验单节点事务 |
| T06a-real（新增） | T06a-core、T11；T14a[dm-ready] | 专门承担真实 DM 驱动、业务连续性和故障回退 |
| T07 | T05、T06a-core | 角色拆分不等 DM |
| T06b | T06a-core、T07 | 多执行节点不等 DM；真实引擎最终矩阵后置 |
| T13b | T05、T06a-core、T13a | 完整包管理复用 core 事务 |
| T02c | T02b、T04、T05、T06a-core、T13b；T14a[native-ready 对应格] | 原生逐引擎接管不等 DM |
| T10 | T02、T04、T05、T06a-core | 先验 catalog/Host，真实引擎页面在完整链复验 |
| T12 | T01、T02、T04、T05、T13a；T14a[kingbase-ready] | 删除 T10，金仓驱动包与 UI 可并行 |
| T08 | T05a、T07、T13a；T03[test-trust-ready] | 明确接入 T05a 的 migrator，不隐藏实现输入 |
| T09 | T05、T05a、T07、T08、T13b、T06a-core | 删除 T06b；单机平台蓝绿不等 Provider 多节点 |
| T14b | T02c、T04、T05a、T06a-core、T06a-real、T06b、T09、T10、T11、T12、T13b；T03[test-trust-ready]、发布范围所需 C70 叶实例 | 最终六引擎、多节点与制品矩阵在这里汇合；production 渠道另需 E07 |
| T15a | T09、T10；T14b[release-matrix-ready:test] | 预生产不要求生产根实际交接或 T14b 整任务结束 |
| T15b | T15a；T14b[production-release-ready] | 正式客户交付使用已验证的生产签名封装 |

T09 另消费三个精确输出：T04[`single-provider-capacity-v0`]、T06a-core[`dual-generation-capacity-v0`]、T08[`platform-ab-budget-v0`]。这些输出必须与本次受测制品、OS/CPU、运行时和负载 profile 匹配。

T05a、条件任务 T05b、P/P-1 回退、权限/审批/任务绑定规则沿用 0.2。本次不扩大目标数据库写操作范围。

## 2. 七张实施变更单

### CP01 / P1：解除单机平台与多节点 Provider 的绑定

主责 CORE，联合验收 QA/REL。

1. T09 依赖由 `T06b` 改为 `T06a-core`；C37 仍唯一归 T06b。
2. C66 改为 **L2 单机组合事务**：实际平台 P/P-1 制品与签名 fake Provider V/V+1，测试两种升级顺序、第二轴失败、回切和版本绑定。
3. 原 C66 的真实引擎 L3 不丢弃：新增 **C84 / T14b**，按六引擎的真实制品/目标格验组合更新、失败和回退。
4. C27 的冻结负载改消费 CP05 的 v0；T14b 从其协作方改为下游复验，不能成为 T09 的输入。

文档修改：`development-tasks.md` 的 T09/M3；`decisions.md` D05；`contracts.md` 平台热更新；`test-cases.md` C27/C37/C66/C84；两份 JSON 的任务、需求映射、协作和里程碑。

退出条件：在投影依赖图中移除 DM、金仓、多节点环境后，满足其余底座输入的 T09 单机案例仍可调度；C84/C37 保持未验收，不能因 core 通过被代签。

### CP02 / P1：T06a 拆 core / real，解除阶段门中的 DM 依赖

主责 CORE（core）/ENG（real），联合验收 QA/REL。

**T06a-core 工作包：**

- W1：交付由测试根签名、走正式 Supervisor/RPC 路径的 fake Provider V/V+1；目标副作用限隔离测试账本，禁止访问客户目标。
- W2：实现 stage/readiness/generation CAS/drain/recover；冻结排队、执行中和待对账任务的版本引用。
- W3：覆盖切流前后崩溃、重放、陈旧 epoch、吊销、超时排空；实际进程退出不能伪写任务成功。
- W4：产出双 generation 容量 v0 和 core 专属证据，不声明任何厂商驱动已认证。

**T06a-real 工作包：**

- W1：消费 T11 的真实 DM V，自备一个经审核源码实际不同的兼容候选 V+1，输出 `dm-vv1-pair`；不能只改版本号或摘要。两包均完成签名、驱动来源和身份核验。
- W2：在 dm-ready 对应目标运行只读任务负载，验真实连接、结果回读与业务连续性。
- W3：复验真实驱动在取消/进程终止/切流前后故障中的连接及未知效果语义；提交 L3 证据。

**案例归属：**C21、C23 → T06a-core / L1+L2；C22 → T06a-real / L3；新增 C79 → T06a-real / L3，承接原 C23 的真实故障与回退。C47 仍验原生真实更新。T12/C46 只验金仓首包能力，另新增 W5 产出真实、源码不同的 `kingbase-vv1-pair`；金仓热切流归 C84，避免把 core 隐性加进金仓首包依赖。C84 每个引擎实例都必须消费对应真实 V/V+1 pair，不用一对 fake 包替代六种驱动。

**阶段门联动：**

- 新增 M1-base：T04/T05a/T05/T13a 及 T03[test-trust-ready]、所需 runtime/core/P-1 输入完成，可进入 M2-core、T07/T08 和 M3-single；不要求 T03 整任务或生产根交接完成。
- M1-DM：T11 + dm-ready，独立证明 DM 首包；T06a-real 仅等这条线及 core。
- M2-core：T06a-core/T07/T13b，供单机平台与各引擎消费；T06b、原生接管、金仓分别推进。
- 最终六引擎发布/现场门继续汇总实际格，不把 core 完成写成整体 M2/M3 完成。

**UI 联动：**C39 改 L1/L2 的 catalog/fake/实际 Host 证据；G10 不再要求“已通过 M1 的 DM”。真实引擎页面、模型到真实结果的 L3 留 C48。T12 删除 T10 硬依赖，在 C48/C84 与 UI 汇合。

后续代码落点（新建）：`runtime/engine_host/hot_update.py`（相对 `backend`）、`tests/test_engine_hot_update_core.py`（相对 `backend`）、`tests/test_engine_hot_update_dm_live.py`（相对 `backend`）、`backend/tests/engine_packs/fixtures/signed_provider/`。这些路径目前是计划，不表示已有实现。

退出条件：令 dm-ready=`waiting_external`，T06a-core、T07、T06b、T13b、T02c 对应原生格、T10、T08/T09 的依赖闭包均不含 T11/T06a-real；DM 的 C22/C79 仍等待真实输入。

### CP03 / P1：C70 改为可独立消费的环境叶实例

主责 QA/OPS，REL 核验介质许可及 P-1 来源。

| variant_id | 输出 | 独立通过依据 | 主要消费者 |
| --- | --- | --- | --- |
| runtime | runtime-ready | 目标 OS/CPU 构建/启动资源、受控测试签名环境 | T04 |
| core | core-ready | 元数据库实验格、至少一个真实原生实验库、隔离与清理 | T05a/T05b |
| pminus1 | platform-pminus1-ready | 合格原始制品/Schema/脱敏快照；缺失则触发 T05b，C78 通过后再签发 | T05a/T09 |
| dm | dm-ready | DM 完整版本/模式、驱动/native 介质、使用/再分发许可、目标范围 | T11/T06a-real |
| kingbase | kingbase-ready | 金仓对应的同类证据 | T12 |
| native | native-ready | 对应 oracle/mysql/postgresql/sqlserver 的环境与介质证据 | T02c 对应引擎 |

具体改法：

1. C70 父节点只作分组，不作为可执行通过/失败门。增加 `variants/instances`，每个实例独立存 `status/executed/evidence/acceptance_roles`，主责继承 T14a。
2. 执行键至少为 `(case_id, variant_id, cell_id, requirement_context_digest)`。`cell_id` 来自冻结矩阵，包含 engine、OS/CPU、运行时/驱动 ABI、目标版本/模式等相关维度；不在计划中虚构现场版本。
3. 阻塞 fixture 必须附精确 `variant_id/cell_id/output` selector；消费者只读该叶实例，不检查 T14a 整任务或 C70 父节点完成。
4. `no_qualified_pminus1` 只记录基线缺失并触发 T05b，不等于 pminus1-ready。
5. 从 T14a 移走 `all-advertised-cells-ready`；由 T14b 根据本次发布冻结的 required cells 生成 `release-matrix-ready`，仅用于发布门。必需格的 skipped/not_run/unsupported/缺证据均不能汇总通过。

T14a 只提供 cell catalog 和叶实例事实。`release_scope_manifest` 由 REL/T14b 在 run 前冻结所宣称的范围，逐项列出 variant/cell/context/output selector；不能根据“当前有哪些可用环境”倒推需要支持哪些格，也不能把整个 catalog 当成已通过门禁。

修改文件：`test-case-ownership.json`、`traceability.json`、C70 表/操作稿、`validate_plan.py`。检查器增加叶键唯一性、fixture selector、输出级 readiness 和“DM 失败不影响 native 通过”的状态样例。

退出条件：同一份报告中可同时出现 dm=`waiting_external`、native.mysql=`passed`、runtime=`passed`；相应原生任务可进入下一步，而六引擎发布汇总仍不通过。

### CP04 / P2：migrator 接入部署与 readiness

主责 CORE；T05a 负责迁移内容/验证合同，T08 负责启动接线，T09 负责切流联动；联合验收 QA/OPS。

核查事实：当前 `backend/app/main.py` 的 lifespan 已调用 `initialize_schema()`；`migrations/locking.py` 已有数据库级锁；`runtime/release_manager.py` 还存在维护式流程的 down-migration/restore hook。这里要改的是蓝绿模式的执行角色与门禁，不能再加一条并行自动 DDL 路径。

- **T05a 新增输出** `migrator-contract`：迁移 revision/checksum、前置/后置校验、可续跑条件、`migration_required`、P/P-1 兼容范围。
- **T08 新增 W5**：启动器在启动候选 API 前调用专用 migrator role；复用现有数据库锁。API/Worker 在 managed-hot 模式只做 Schema 校验，不执行 DDL。初装也只由单一 migrator 做 bootstrap；保留显式维护入口。
- migrator 持久记录 operation/revision/checksum/checkpoint；多启动器可竞争，只有持锁者执行。这里保证一次有效执行者，不能承诺 MySQL 非事务 DDL 在崩溃后“恰好执行一次”。
- kill 后先核对实际结构与校验结果；已执行的幂等步骤跳过，不确定的部分 DDL 进入 reconcile_required，禁止盲重放或盲恢复备份。
- **T09 新增 W6**：候选 readiness 校验真实 migration receipt、schema revision/checksum 和兼容合同；失败/缺失时不接新流量，旧路由继续服务。
- hot 模式拒绝注册/调用 down-migration 与自动共享库 restore hook；代码回退只读校验并回路由。维护式灾难恢复保留独立授权入口。

实际拟修改：`run.py`、`backend/app/main.py`、`backend/app/runtime/exe_launcher.py`、`backend/app/runtime/database_migrations.py`、`backend/app/runtime/release_manager.py`、`backend/app/migrations/{runner,locking,initialization}.py`、`backend/app/services/platform_observability.py`、`scripts/migrate_schema.py`。

新增拟实现：`runtime/hot_update_coordinator.py`（相对 `backend`）、`tests/test_migration_startup_gate.py`（相对 `backend`）。回归现有 `test_deployment_and_migrations.py`、`test_release_manager.py`、`test_migration_registration.py`。

案例：新增 **C82 / T08** 验单 migrator、多启动、失败 readiness、回退不 down；C38 加入“获锁前后、部分 DDL、校验后回执前、回执后 green ready 前”的 kill 点。T08 用启动接线验收，完整 P/P-1 切流在 T09 验收。

### CP05 / P2：把容量 v0 前移为 T09 的显式输入

主责 CORE，测量/冻结 QA/OPS。

| 产物 | 归属与案例 | 内容与范围 |
| --- | --- | --- |
| single-provider-capacity-v0 | T04 新增 W6；新增 C80 | 目标 OS 的单进程冷/热启动、RSS/子进程、连接、句柄、退出与排空；fake profile 只证明该 profile |
| dual-generation-capacity-v0 | T06a-core.W4；新增 C81 | V/V+1 并存、旧任务排空、队列与连接、进程数/内存峰值、连续切流后泄漏 |
| platform-ab-budget-v0 | T08 新增 W6；扩 C82 的资源预检 | 实际平台 A/B、Router/Worker 和本次 Provider profile 的联合资源预算；容量不足拒绝启动候选/接流 |
| 最终矩阵与峰值 | T14b/C67 | 六引擎真实驱动、多 OS/CPU、目标负载与多节点矩阵；不能倒过来成为 T09 的前置 |

拟新增 `measure_engine_capacity.py`（相对 `scripts`，拟新增），输出 versioned capacity manifest：profile、OS/CPU、制品/驱动摘要、样本、负载、启动/排空时长、RSS/进程/连接/句柄时序、阈值与 QA/OPS 冻结签署。

先做 calibration run，不计正式通过；再冻结预算并正式执行。C27/C66 使用匹配的 v0。若组合中使用真实原生/DM/金仓驱动，必须补该实际 profile 的数据，禁止用 fake RSS 代替；真实六引擎组合由 C84 等待对应数据。T09 的 readiness 还须检查当前空闲资源，历史报告不能替代本次资源预检。

C67 按冻结发布矩阵展开容量 profile 实例。C84 显式消费已通过的 C67[`qualified-capacity-profile`]，逐项匹配 engine/OS/CPU、driver/native ABI、Provider payload 摘要、负载与 context digest；不匹配则 not_ready。这是 T14b 内部的案例级前置，不把 T14b 整任务反向接到 T09。C84 同时消费 T06b 多节点证据，联合验收为 QA/OPS/REL。

退出条件：未提供匹配 v0、超资源配额或报告摘要不符，T09 在切流前拒绝；这不要求完成 T14b 的全矩阵。

### CP06 / P2：生产信任根交接与职责分离

主责 REL；生产保管 OPS；新增 SEC（独立生产安全责任人）批准，QA 验证。具体人员在交接工单登记；SEC 不得与 REL 签名操作人、OPS 保管人或 build operator 是同一人，同一人不能独自完成保管、批准和恢复。S1 同步机器角色表和验收人约束。

- **T03 新增 W6** `production-trust-handoff`：测试根/生产根分开 namespace、key ID、channel 和信任目录。开发/CI 只持测试根材料，不访问生产私钥；正式发布 verifier 拒绝测试根签名。
- OPS 保管生产离线根/HSM 或等价介质及受控恢复份；REL 提供制品摘要、来源与签名请求；独立安全责任人批准根/授权用途/轮换/吊销。
- 根门限泄露时由安全责任人宣布事件和批准带外重建，OPS 执行根仪式与客户端重锚定，REL 重新核验/背书包摘要；沿用 C58 独立演练与审计。
- 交接只登记公钥指纹、门限、用途、角色/审批、介质托管证明、离线恢复步骤和结果；不把私钥、恢复密语或可导出生产材料写进仓库/日志/开发机。

交付：新增拟文档 `operations/engine-signing-root-handoff.md`（相对 `docs`，拟新增） 与公开字段的交接模板；新增 **C83 / T03**，分两个实例记账：

1. `rehearsal` / L1+L2：用测试/演练根验隔离、越权签名拒绝、REL/OPS/安全职责和带外恢复；与包格式/验签等技术案例共同签发 T03[`test-trust-ready`]。
2. `production-handoff`：仅接受真实公开交接记录，由 OPS/安全独立签署，初始 `waiting_external`；新增 E07 / `production-trust-accepted`，记录 REL 的制品摘要/用途、OPS 托管证明、独立安全批准、生产公钥指纹及恢复流程引用。演练通过不能给 E07 置通过。

早期 T04/T13a/T06a-core/T08 消费 test-trust-ready，不依赖 T03 整任务。T14b 的矩阵输出带 channel：测试根可产生 `release-matrix-ready:test`。`production-release-ready` 还必须满足 E07、对同一已测 payload 仅增加生产签名封装、生产公钥 verifier 成功、封装前后 payload digest 不变、受控 verify/install smoke 通过；禁止交接后重构建/重组字节而绕过测试。正式渠道签名/发布工具强制检查这些条件。T15a 预生产消费 test 矩阵，不等待生产仪式；T15b 正式客户交付需 production-release-ready。技术演练不冒充真实生产根已交接。

### CP07 / P2：限定可重复构建的对象

主责 REL/构建工程，联合验收 QA/REL；厂商来源和许可由 REL 核验。

- T03.W2/W4、合同 §8–9、C73 改为三种独立 variant：`self-payload`、`third-party-integrity`、`incident-rebuild`。保留 builder/release-key 泄露处置，不能在拆可重复性时删掉安全演练。
- 自研 Provider/Schema/脚本等，以锁定输入和规范化规则比较两个 clean builder 的 `self_payload_digest`。
- 第三方 wheel/native client 作为 opaque artifact：记录厂商/仓库来源、完整版本/ABI、原始 hash、签名验证状态、许可证及 SBOM；不承诺从厂商源码重新编译成相同字节。上游无签名时明确 recorded-unsigned，须走来源核验策略，不能伪填 verified。
- 同一组锁定第三方 bytes 在两个 builder 中原样导入；原始 hash 必须相同。原厂 bytes 变化需新包/新依赖锁和重新认证，不能以“不可重现”为由接受漂移。
- 整包签名继续覆盖全部逐文件摘要，包括第三方二进制。自研可重复性与整包完整性是两个判定，不从验签范围排除第三方文件。
- payload 文件分类必须穷尽且互斥：每个文件恰属 `self-built` 或有真实上游来源证据的 `third-party-opaque`。未知 blob、重复分类、把自研不稳定输出冒充第三方均拒绝。签名/时间等允许变化的外层字段单列并解释，不混入 self_payload_digest，也不从整包完整性清单漏掉。

修改：包 manifest/SBOM 合同、T03 构建工具（拟 `engine_pack.py`（相对 `scripts`，拟新增））、`scripts/release_gate.py`、C73/其操作稿及实例记录。退出条件：自研差异阻断；第三方错 hash/不明来源阻断；合法 opaque client 不因不能重编译而被错误判失败。

## 3. 应用顺序和改动文件

| 批次 | 具体操作 | 完成依据 |
| --- | --- | --- |
| S1 开发基线补丁 | 先按 CP02 拆任务，再改 CP01 的依赖和阶段门；按 CP03 引入叶实例。一个变更集同步下列 9 个计划文件，改为 0.3 | 投影图无环；移除 DM/multi-node 输入仍能排出 core/单机链；旧 ID 有明确映射 |
| S2 M0/M1 合同与底座 | CP04/05/06/07 先写合同和执行器输入；实现 T04 单 Provider v0、T03 测试根/构建分类、T05a migrator 合同 | 对应新案例及原安全/迁移回归；生产根仅在正式签发前交接 |
| S3 M2-core 与单机平台 | T06a-core/v0 → T07/T13b → T08 migrator+平台预算 → T09 单机/组合事务 | C21/C23/C80–C82/C27/C38/C66；均不等待 DM 或 T06b |
| S4 并行引擎和最终矩阵 | T06a-real/DM、T02c/原生、T12/金仓、T06b/多节点各走自身格；T14b 汇总，随后预生产/现场 | C22/C79/C47/C46/C37/C67/C71/C84/C48/C72，缺格不得宣传全矩阵通过 |

S1 同步文件：`README.md`、`development-tasks.md`、`decisions.md`、`contracts.md`、`test-cases.md`、`test-procedures.md`、`test-case-ownership.json`、`traceability.json`、`validate_plan.py`；并更新上级蓝图索引与 `review-resolution.md` 的本轮记录。

追踪格式从 v2 升为 v3，以保存 case family / variant / instance / output selector；迁移保留原有案例与历史映射，不把父状态复制成所有叶实例通过。检查器改动须包括：`EP-T06a-core/real` 的 ID/Markdown 解析、替换对旧 T06a 的硬编码、需求 EP-R05/R08 等映射、叶实例执行键、精确 fixture selector、输出级就绪而非 T14a 整任务就绪、父案例不计通过、发布汇总由 T14b 产生。

原 T06a ID 只留历史映射，不能同时作为可调度任务。C70/C73 保留案例家族 ID，实例分别关闭；每个执行实例仍只有一个主责任务。新增 C79–C84 分别为真实故障、单 Provider v0、双 generation v0、迁移启动门禁、生产根交接和真实组合矩阵。

旧案例协作也逐条迁移：C01/C24/C25/C26/C29/C30/C36/C37/C39/C47/C66 的 T06a 引用改 core；C48 改 real；C67 同时引用 core/real。C39 的 T11 改非阻塞下游复验；C48 通过精确的 test 矩阵输出消费 T14b。所有阻塞 T14a fixture 增加对应 C70 的 variant/cell/context/output selector，具体替换清单已列入 JSON，不能只做任务 ID 字符串替换。

## 4. 本轮计划验收与未来实施验收

S1 现已实际同步主目录 v3 文件与检查器；后续工作包和所有业务实例仍待执行。以下投影资料是应用前历史输入，不作为主目录 S1 验收或业务通过记录。

应用前应用前应用前应用前已保存 [投影检查报告](../../../reports/engine-extension-change-plan-0.3-20261026/projection-validation.json) 与 [可重跑的检查器](../../../reports/engine-extension-change-plan-0.3-20261026/check_projection.py)。检查在内存中套用拟改关系，核对 0.2 文件摘要保持、任务/案例主责、阻塞协作、环境输出与无 DM/多节点/生产交接时的 core 路径；不写回 0.2，不执行业务代码。当前主目录 S1 检查与应用 journal 另存 [S1 证据目录](../../../reports/engine-extension-s1-20261026/)，原变更单 bytes 保存在 [0.2 快照](../../../reports/engine-extension-s1-20261026/snapshot-v0.2/)；历史投影检查器针对原件使用，不对当前已应用的 0.3 重放。

S1 检查器须以合成规划数据验证以下拒绝/分格语义；S2–S4 仍须以真实制品/目标执行对应业务案例：

1. DM 格等待时，M1-base、T06a-core、T07、T06b、对应原生接管、T10/T08/T09 仍可按已有输入调度。
2. T06b 未通过时，T09 单机可验；T14b 全矩阵仍不能关闭。
3. C70 的两个不同格可分别 passed/failed，输出消费者及发布汇总判定正确。
4. 无 migrator receipt/部分 DDL/竞争启动时拒绝候选接流；回退从不调用 down/自动共享库 restore。
5. 无匹配 v0/资源不足拒绝切流；fake 数据不能认证真实驱动容量。
6. 正式 verifier 拒绝测试根；生产私钥不进入开发/构建环境，生产交接缺失阻止正式签发。
7. 自研可重复性、第三方完整性、事件重建分别有结果；opaque 二进制不被误设为必须源码重现。

实际 Provider/平台/密钥及真实目标用例仍待实现和执行；S1 合成检查不替代它们。真实版本、介质许可、生产交接人员与客户窗口按 E 项落实，不编造这些输入。
