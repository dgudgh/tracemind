# 架构取舍、外部输入与回退边界

版本：0.3，2026-10-26。以下是 S1 已同步的开发基线，业务实现和业务案例仍全部 planned；没有把待提供的厂商版本、介质、授权、生产根交接或客户窗口当成已确认事实。用户明确改变基线时，先更新此文件、任务依赖和案例，再改实现。0.2 审核中“全部设计缺口已闭合”的表述已被本轮复核修正，不再代表当前结论。

## D01：原生四引擎也采用独立 Provider 包

Oracle / MySQL / PostgreSQL / SQL Server 与扩展引擎使用同一 SDK、签名包、进程运行时、版本绑定和回退机制。产品首装必须自带四个经过认证的包及离线依赖；“原生”不意味着把适配实现编进核心，也不意味着客户需要自行安装四次。

- 原生 engine ID 和发布者受保护。原生目录条目不能由卸包删除。
- 允许清理无引用的旧包；active、last_known_good、回退基线、在途/待对账任务引用的包不能卸载。`last_known_good` 是按平台/SDK/OS 认证格保留的合格摘要集合，最后一个可用原生包不能卸载；停用与卸载分别授权、分别记账。
- 更新失败保留或回切已经验收的旧包，不在主程序再维护一套静默只读降级实现。
- 首装没有旧包时，Provider 启动失败即显示该引擎不可用及原因，保留管理和修复入口；不能宣布“原生四引擎首装验收通过”，也不能伪造只读能力成功。
- T02b 只验源码分离和原行为对照；T02c 才验四个签名制品的进程接管、独立更新和回退。过渡兼容桥必须有清单和删除条件，不能永久绕开 Provider。

## D02：平台保证相邻一个合格版本的代码回退

平台首期保证当前版本 P 与上一合格版本 P-1 的兼容窗口；不承诺直接回退 P-2。保留的任务执行版本可以更老，其保留由任务引用决定，与平台 API 回退窗口分开。

1. expand 仅增加 P/P-1 都能安全使用的结构；backfill 可断点续跑、幂等、带校验。
2. 必须用**未经改造的实际 P-1 制品**同时读写验证。T14a[platform-pminus1-ready] 提供原始制品/摘要、真实 Schema/脱敏数据快照和合格 release 记录。若盘点为 `no_qualified_pminus1`，触发 **T05b/CORE** 单独开发、交付并认证桥接基线 B，T14a 仅核验登记，不能承担或隐藏平台开发。T05b 只依赖 SDK/Registry 合同与 core-ready，先冻结旧行为及 additive 合同，不依赖 T05a 全任务；T05a 完整验收在此分支硬依赖 T05b。之后用未经改造的 B 验候选 B+1；B 首次落地是维护式安装，legacy 迁移不能当成热回退。已有合格旧版时，凭实际资格记录将 T05b 登记为 `not_required_by_gate`，不把未执行案例算通过。旧制品仍写旧字段，P 兼容读取旧写入并保持兼容形态；新语义使用可保留的附加结构或并发保护，不能让旧 writer 静默覆盖。
3. 回退只切代码、路由和符合兼容图的任务入口；新增共享表、字段和业务记录保留，不执行破坏性 down migration。
4. 仍处于 P/P-1 保证窗口的结构不得 contract。删除旧结构要另立迁移发布，确认新的当前版本及其直接前一版都不依赖旧结构、全部旧 writer/任务退出并通过兼容测试后才允许。不能临时取消承诺的 P-1 回退来使一次发布通过。
5. P-1 不能无损读写新状态时，发布预检拒绝为热更新候选，先交付桥接版本；已有副作用不会因代码回退自动撤销。
6. T05a 输出 `migrator-contract`（revision/checksum、前后置校验、`migration_required`、可续跑与 reconcile 条件、P/P-1 范围）；T08 对此建立硬依赖并负责部署启动接线。managed-hot 下只有专用 migrator 可以执行 bootstrap/DDL，API/Worker 只校验 Schema 和 receipt。
7. 多启动器复用数据库级锁，只保证一个有效迁移执行者，不承诺 MySQL 非事务 DDL 在崩溃后“恰好一次”。部分 DDL 先核对实际结构：已完成幂等步骤跳过，不确定状态进入 `reconcile_required`；hot 回退禁止 down migration 和自动共享库 restore，维护式灾难恢复走独立授权入口。

SDK 的兼容轴单独命名 S：宿主认证当前 S 及上一兼容线 S-1，S-2 默认拒绝；必须按显式兼容表判断，不能根据版本数字猜测。可忽略的 optional 字段允许扩展，未知 mandatory 语义在激活前拒绝；不通过静默降级绕过权限、身份或副作用合同。

## D03：本机 IPC 默认不开放 TCP

Linux/macOS 用 Unix domain socket，Windows 用本机命名管道。文件权限/ACL、OS 对端身份、Supervisor 启动记录与每次启动的一次性双向认证一起约束宿主和 Provider；只校验 socket 路径或 PID 都不够。不得宣称同 UID 下的恶意进程已经被普通文件权限完全隔离。Windows 实现必须拒绝远程管道客户端。

普通业务和更新控制分开端点。默认禁用 TCP；未来若采用回环 TCP，必须另测双向 TLS、证书签发/吊销和本机绑定，不能先裸开端口再补安全。帧、方法、租借凭据、资源和重启策略见 [运行时合同](contracts.md)。独立进程提供依赖和故障隔离，受信任 Provider 仍须遵守宿主权限及数据库最小权限。

## D04：首批国产引擎先形成一套真实认证组合

首个工程验证目标采用 **Linux x86_64 候选组合**；具体 DM / KingbaseES 服务端版本、edition、兼容模式、驱动和 native ABI **尚未提供，不写成已支持**。T14a 在 T11/T12 真实认证前负责冻结这些输入；没有介质和合法使用条件就保留依赖状态，不能用模拟驱动或大量 skip 结项。

既有四引擎已承诺的 OS/CPU 组合必须先从实际产品制品/交付记录盘点，不能借首个国产组合缩减旧支持范围。Windows amd64、macOS arm64 及其他组合逐格认证；未认证格只显示未认证，不宣传“全平台可用”。驱动有权使用与有权随包再分发分别核验；不具备再分发权时，采用经过审核的本机离线导入流程和精确摘要，不私自附带厂商客户端。

| 外部输入 | 提供/落实责任 | 开发侧责任与门禁 | 当前状态 |
| --- | --- | --- | --- |
| E01 DM 完整版本/edition/兼容模式及测试目标 | 产品/环境负责人协调用户或厂商 | ENG 录入实际矩阵，QA 验证强身份；T11 真实认证前冻结 | awaiting_external_input |
| E02 KingbaseES 完整版本/edition/兼容模式及测试目标 | 产品/环境负责人协调用户或厂商 | ENG / QA 同上；T12 真实认证前冻结 | awaiting_external_input |
| E03 驱动/native client 安装介质与使用/再分发许可 | REL 负责人向厂商/许可证持有人取得依据；用户提供已有合法介质时由 REL 核验 | 锁定哈希、ABI、许可证/SBOM；缺一格就不能认证该制品 | awaiting_external_input |
| E04 OS/CPU 构建机、六引擎实验库、签名设施、资源配额 | OPS / QA 负责搭建，REL 负责签名设备或等价受控离线签名环境；产品负责人确认需保留的原生矩阵 | T14a 输出资源和访问清单、隔离及清理方案；所需组合按阶段准入 | planned |
| E06 上一合格平台制品及 Schema/数据快照 | REL 提供原始制品/资格记录，OPS/QA 提供脱敏快照；没有时触发 T05b，由 CORE 开发、REL/QA 独立认证桥接 B | T14a[platform-pminus1-ready] 是 T05a 的输入门禁；没有则 T05b 为条件硬依赖，不能靠模拟旧版代替 | awaiting_baseline |
| E05 客户现场窗口、网络、范围账号和现场验收人 | 客户/环境负责人提供，OPS 对接 | T15b 独立准入；只读验收也须有明确目标和授权范围 | awaiting_external_input |
| E07 生产信任根交接 | OPS 保管生产离线根/HSM 或等价介质与恢复份，REL 提供已测 payload 摘要/来源/用途，SEC 独立批准 | `production-trust-accepted` 只接受公开交接字段；生产私钥不入 DEV/CI/build；T15b 前还须通过同 payload 生产封装/verifier/install smoke | awaiting_external_input |

这些是外部依赖及内部基线准备登记，不表示正在连接或已经获准改动客户库。任务开工登记时把责任角色落实到具体姓名与日期；不能由开发者臆填版本或授权。C70 按 `runtime/core/pminus1/dm/kingbase/native` 六个 variant，以及精确 cell/context 分别签署；执行键至少为 `(case_id, variant_id, cell_id, requirement_context_digest)`。父案例不计通过，T14a 整任务未结束也不阻止已签叶输出被消费者精确使用。

T14a 只交付 cell catalog、介质/许可/环境/资源事实及叶输出，不签 `all-advertised-cells-ready`。REL/T14b 必须在 run 前冻结 `release_scope_manifest` 和 required cells，不能按已有环境倒推发布范围；声称“六引擎全矩阵”时必须覆盖真实 Oracle、MySQL、PostgreSQL、SQL Server、DM、KingbaseES 及声明的 OS/CPU/ABI/profile/context，只有 MySQL 或其他局部 scope 不得冒充全矩阵。required cell 的 skipped、not_run、unsupported 或缺证据不能计为通过。容量是否足够由 T14b/C67 按冻结 profile 实测。

## D05：core / 真实 Provider 分线，再做单机平台蓝绿；多节点分层验收

T06a-core 用测试根签名 fake V/V+1 走正式 Supervisor/RPC，在隔离测试账本证明一台节点上的 generation、任务固定、故障恢复和 L1/L2；它不等待 DM。T06a-real 独立消费 T11 的真实 DM V 和一个经审核、源码实际不同的 V+1，证明 L3 连接、切流及未知效果对账。T07 分离 API/Scheduler/Worker 后，T06b 再证明多执行节点的 membership_epoch、租约与 generation 路由；T06b 不依赖真实 DM，也不是 T09 单机平台的前置。

阶段门拆为 `M1-base` 与 `M1-DM`：前者消费 T04/T05a/T05/T13a、T03[`test-trust-ready`] 及所需 runtime/core/P-1 叶输出，不等待 T03 整任务或生产根；后者只证明 T11 + `dm-ready`。`M2-core` 由 T06a-core/T07/T13b 形成，供 T08/T09、T10 和各引擎后续组合消费。DM、原生、金仓和多节点各走自身格，最终发布再汇总，不能以 core 完成代签整体完成。

T10 早期目录/UI 只消费 catalog、签名 fake 和实际 Host 的 L1/L2，不依赖 DM 或 T06a-real；真实引擎页面、模型到真实结果的 L3 留后续组合案例。T12 首包不依赖 T10 或 T06a-core，只交付 KingbaseES 真实首包及源码实际不同的 `kingbase-vv1-pair` 给 C84；金仓真实组合切流在 C84 与 core/UI/多节点及容量 profile 汇合。

C84 与 C67、C70、C73、C83 一样是 case family，父节点不可直接执行或计为 passed。它必须按冻结范围为真实六引擎及 OS/CPU/driver-native ABI/load profile/context 逐 `real-combination` leaf 实例化并输出 `qualified-real-combination`；每个 leaf 精确消费该格真实且源码实质不同的 V/V+1 pair、同 selector 的 C67 `qualified-capacity-profile`，并验证组合顺序、第二轴失败、回退和范围所需的 T06b 多节点证据。局部 leaf 可以先通过且不要求提供方整任务关闭，但消费时必须验证该输出自身的 selector、passed 状态、证据、联合验收及全部前置，不能只认一条 receipt。

平台业务热更新首发采用固定 Router 加同机 A/B 业务进程，要求资源同时容纳两版、共享元数据库满足 P/P-1 合同。T09 硬依赖 T05a、T08、T06a-core/T13b，但不依赖 T06b、T06a-real、DM 或金仓。此路径不包含主机故障容灾。平台多节点滚动发布不能拿 T06b 的 Provider 多节点证据替代，若列为发布支持范围，需追加独立任务和案例。

Router/Supervisor 自身更新在 T08 单独验收监听接管或已有冗余入口。没有无中断接管路径时，相关基础制品标明 hot_update_eligible=false；不能把一次重启包装为热更新。首轮部署稳定底座与后续热更新是两份记录。当前正在运行的旧单进程系统尚未获得无中断升级能力。

容量门禁前移但不冒充最终矩阵：T04 输出 `single-provider-capacity-v0`，T06a-core 输出 `dual-generation-capacity-v0`，T08 输出 `platform-ab-budget-v0`，三者是 T09 的精确 profile 输入。fake 数据只证明 fake profile。T14b/C67 按冻结发布矩阵输出真实 `qualified-capacity-profile`；C84 必须按 engine/OS/CPU、driver/native ABI、Provider payload、load 与 context digest 精确消费，C67/T14b 不反向阻断 T09。

## D06：预生产和客户现场分开结项

T03 的 `test-trust-ready` 与真实 E07 `production-trust-accepted` 分开。DEV/CI/build 只可访问测试根；OPS 保管生产根，REL 提交签名用途与已测 payload 摘要，SEC 独立批准，且 SEC 不得与 REL 签名操作人、OPS 保管人或 build operator 为同一人。生产私钥、恢复密语或可导出材料不得进入仓库、日志或开发机。

`release-matrix-ready:test` 是对冻结发布范围的非直接执行派生收据，不能凭一个 passed receipt 成立。T14b 必须核验有效 required cells、每个 required engine 的真实 pair、逐格 C67、逐格 C84 `qualified-real-combination` leaf 与必要的 T06b 等上游证据；任一 required leaf 缺失、跳过、selector 不匹配、证据/联合验收不足或前置失效都阻止聚合签发。局部结果只代表局部范围，不能升级成六引擎全矩阵 ready；`production-release-ready` 同样只由完整前置派生，不能被直接执行或手工置通过。

T15a 负责受控预生产负载、失败和回退演练，只消费仍有效的 `release-matrix-ready:test`，不等待生产根仪式。T15b 单独负责客户现场首包，除 E05 等现场输入外还必须消费 `production-release-ready`：以有效 test 矩阵为前提，E07 已接受、对同一已测 payload 只增加生产签名封装、生产 verifier 成功、封装前后 payload digest 不变，并通过受控 verify/install smoke。交接后重构建/重组 payload 必须回到测试矩阵。T15a 通过不能关闭 T15b，也不能把实验库截图当成客户验收。

## D07：自研可重复性与第三方完整性分开判定

C73 固定为 `self-payload`、`third-party-integrity`、`incident-rebuild` 三个独立 variant。自研 Provider/Schema/脚本用锁定输入和规范化规则比较两个 clean builder 的 `self_payload_digest`；第三方 wheel/native client 作为 opaque artifact，记录真实来源、完整版本/ABI、原始 hash、签名状态、许可证和 SBOM，不承诺从厂商源码复现相同字节，但同一锁定 bytes 在 builder 间原始 hash 不得漂移。incident variant 保留 builder/release-key 泄露后的冻结、干净重建和重新背书。

payload 分类必须穷尽且互斥，每个文件恰属 `self-built` 或有来源证据的 `third-party-opaque`。未知 blob、重复分类或把自研不稳定输出冒充第三方一律拒绝。整包签名与完整性覆盖全部逐文件摘要，包括第三方二进制；自研可重复性失败与整包漏文件是不同失败，不能靠剔除第三方文件或可变外层字段规避。

D01–D07 已作为 0.3 文档开发基线；D04 的接入方法已定，但具体厂商版本、介质许可、实际矩阵、生产交接人员与客户窗口仍是外部待办。这里的“基线”是设计取舍，不是用户已签署所有版本支持范围或功能已实现。修订依据见[具体变更计划](change-plan-0.3.md)。

0.3 实例校验收口：C73 通过叶必须带内嵌 `oracle_receipt` 并经过对应 machine oracle；C84 派生 test/prod 收据只存 `output_instances`，禁止充当 executed 的业务实例。合成 fixture 的结构检查不替代未来真实构建、验签或组织交接。
