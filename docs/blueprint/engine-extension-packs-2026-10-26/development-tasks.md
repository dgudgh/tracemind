# 开发任务、阶段交付与责任

版本：0.3，2026-10-26。S1 已同步有效计划；正式任务/案例机器基线仍为 planned，实际实现进度见下列勾选。岗位责任须在实际工单补具体人员、分支、日期、合同和输入版本；文档检查不能替代业务验收。

## 实现进度勾选（2026-10-27）

用户要求逐项完成后勾选。本页109个工作包现在逐项跟踪；`[x]` 仅表示该工作包代码与其适用本机验证已有实际证据，`[ ]` 表示未完成（包括部分实现、真实环境或外部材料不足）。总任务/阶段的正式验收仍按原退出条件和联合职责执行，不由勾选自动签发。当前本机证据范围为 Darwin/arm64；已完成 fake 的项不会被用于认证真实驱动或其他 OS。

截至本轮接手，以下已有工作包根据[执行链证据](/Users/zhaolongjie/codex/mesdbtools/reports/engine-extension-core-batch-01-20261026/codex-delivery/delivery.md)与[离线包证据](/Users/zhaolongjie/codex/mesdbtools/reports/engine-extension-offline-packs-20261026/delivery.md)核对后勾选。其余持续实现；明细状态与证据见[逐项进度](/Users/zhaolongjie/codex/mesdbtools/docs/blueprint/engine-extension-packs-2026-10-26/implementation-progress.json)。E01–E07 的真实介质、环境、P-1及签署未满足时保持未勾，不得填合成值。

2026-10-27 范围调整：按用户“先保留扩展位置，等后面在进行写功能”，DM/KingbaseES 具体功能（T11/T12）延期，保留标准接口；不勾选、不计通过，也不阻塞本轮核心开发。参见[范围记录](/Users/zhaolongjie/codex/mesdbtools/reports/engine-extension-completion-20261027/scope-update.md)。

## 责任、部分输出与阶段出口

ARCH＝架构，SDK＝接口，ENG＝引擎，CORE＝平台/迁移，REL＝发布与供应链，QA＝测试，OPS＝环境保管，UI＝前端，SEC＝独立生产安全批准。SEC 不与签名人/保管人/build operator 同人；实现者不能独自验收。

每条普通案例及每个可执行叶实例只有一个 primary_task。协作分 input_contract/fixture/downstream_retest/blocking_implementation；阻塞实现须在上游闭包，阻塞 fixture 必须精确到 output/variant/cell/context。case family 父节点不执行、不计通过。任务自己的完成不能由 CI/预生产代签。

输出门禁可在提供方整任务未完成时独立签发。T03[test-trust-ready] 不要求生产交接；T14a[plan] 不要求六种环境全就绪。引用 selector 的 context 未绑定、证据/联合验收缺失，均 not_ready，不能把通配或父状态当通过。

## 批次和里程碑

| 出口 | 必需任务 | 独立输出/条件 |
| --- | --- | --- |
| M0 | T01、T02、T02b | T14a[plan] |
| M1-base | T04、T05a、T05、T13a | T03[test-trust-ready]；E06 缺合格旧版时先完成 T05b；不等待 T11、T06a-real、E07 |
| M1-DM | T11 | T14a[dm-ready] |
| M2-core | T06a-core、T07、T13b | 按本任务入口 |
| M2-real | T06a-real、T06b、T02c、T10、T12 | 按本任务入口 |
| M3-single | T08、T09 | 不等待 T06b、T11、T06a-real、E07 |
| final-matrix | T14b | REL 冻结的 required cells 与实际 profile |
| M3-preprod | T15a | T14b[release-matrix-ready:test] |
| M3-field | T15b | T14b[production-release-ready] |

M1-base/M2-core/M3-single 不以 M1-DM 或 M2-real 整体完成为前提。实际 S2 依次准备 T03 测试信任/构建分类、T05a migrator-contract、T04 单进程 v0、T06a-core 双 generation v0；S3 由 core→角色/管理→启动迁移/平台预算→单机平台；S4 真实引擎与多节点并行后才汇总六引擎发布矩阵。

## 硬依赖与精确输出

| ID | 阶段 | 主责 / 评审 | 范围 | 硬依赖 / 输出门禁 |
| --- | --- | --- | --- | --- |
| EP-T01 | M0 | SDK / ARCH | SDK 合同与 conformance kit | 无 |
| EP-T02 | M0 | CORE / SDK | Registry 与原生兼容桥 | T01 |
| EP-T02b | M0 | ENG / CORE | 四原生源码抽取与行为基线 | T01、T02 |
| EP-T14a | M0 | QA / OPS | 测试环境、介质与许可证准入 | 无 |
| EP-T03 | M1-base | REL / ARCH | 签名包、构建与密钥生命周期 | T01 |
| EP-T05b | M1-base | CORE / REL | 缺失旧版时的桥接基线 bootstrap | T01、T02、T14a[core-ready] |
| EP-T05a | M1-base | CORE / QA | Schema 迁移、回填与双版本读写 | T01、T02、T14a[platform-pminus1-ready]、T14a[core-ready]、E06.no_qualified_pminus1 → T05b |
| EP-T04 | M1-base | CORE / REL | Provider 本机进程运行时与安全 IPC | T01、T02、T03[test-trust-ready]、T14a[runtime-ready] |
| EP-T05 | M1-base | CORE / QA | 任务版本绑定、再鉴权与独立结果解码 | T01、T02、T05a |
| EP-T13a | M1-base | REL / OPS | 最小本机包管理与安全准入 | T04、T05、T03[test-trust-ready] |
| EP-T11 | M1-DM | ENG / QA | DM 真实只读首包闭环 | T01、T02、T04、T05、T13a、T14a[dm-ready] |
| EP-T07 | M2-core | CORE / OPS | API / Scheduler / Worker 执行角色拆分 | T05、T06a-core |
| EP-T06b | M2-real | CORE / OPS | 多执行节点 generation 与租约热更新 | T06a-core、T07 |
| EP-T13b | M2-core | REL / CORE | 完整本机生命周期与引用保护 | T05、T06a-core、T13a |
| EP-T02c | M2-real | ENG / CORE | 四原生制品接管与独立更新 | T02b、T04、T05、T06a-core、T13b、T14a[native-ready] |
| EP-T10 | M2-real | UI / CORE | Catalog 同源 UI 与能力桥 | T02、T04、T05、T06a-core |
| EP-T12 | M2-real | ENG / QA | KingbaseES 真实只读首包 | T01、T02、T04、T05、T13a、T14a[kingbase-ready] |
| EP-T08 | M3-single | CORE / OPS | 稳定 Router 与部署启动器 | T05a、T07、T13a、T03[test-trust-ready]、T05a[migrator-contract] |
| EP-T09 | M3-single | CORE / REL | 平台蓝绿与组合升级/回退 | T05、T05a、T07、T08、T13b、T06a-core、T04[single-provider-capacity-v0]、T06a-core[dual-generation-capacity-v0]、T08[platform-ab-budget-v0] |
| EP-T14b | final-matrix | QA / REL | CI、真实制品矩阵与容量基线 | T02c、T04、T05a、T06a-core、T06a-real、T06b、T09、T10、T11、T12、T13b、T03[test-trust-ready]、REL 冻结 required-cell selectors（非 T14a 整任务） |
| EP-T15a | M3-preprod | QA / OPS | 预生产负载、热更新与回退演练 | T09、T10、T14b[release-matrix-ready:test] |
| EP-T15b | M3-field | QA / OPS | 客户现场首包验收独立项目 | T15a、T14b[production-release-ready] |
| EP-T06a-core | M2-core | CORE / REL | 单节点 Provider 热事务 core（签名 fake / L1-L2） | T04、T05、T13a、T03[test-trust-ready] |
| EP-T06a-real | M2-real | ENG / QA | DM Provider 真实连续性（L3） | T06a-core、T11、T14a[dm-ready] |

T09 六项硬依赖是 T05/T05a/T07/T08/T13b/T06a-core；额外只消费三份匹配 v0，及 readiness 中的迁移回执/结构校验，不加 T06b/DM/生产根隐性条件。T08 的 T05a 实现与 migrator-contract 是明确输入。

T14a 只提供 cell 事实。REL/T14b 在运行前冻结 required cells/capacity profiles；同一实例 context 包含实际制品/版本、目标和 profile 摘要。T15a 只消费 release-matrix-ready:test；T15b 要求 production-release-ready（真实 E07、同 payload 生产封装/验签/受控安装 smoke），正式现场在封装就绪之后。

## 可拆单的工作包

### EP-T01 SDK 合同与 conformance kit

- [x] **EP-T01.W1**：冻结 descriptor、版本协商、身份/能力/参数/结果/错误 Schema；区分 optional、mandatory 与 namespaced 扩展。
- [x] **EP-T01.W2**：实现模板和脱敏 fake Provider：只承载合同夹具，不当作真实驱动验收。
- [x] **EP-T01.W3**：给出 S/S-1 接受、S-2 拒绝及显式协议协商向量；认证/副作用字段不能被降级删除。
- [x] **EP-T01.W4**：产出开发者最小接入教程、示例包源码、接口语义与负例执行器。

交付物：合同 Schema、SDK 模板、conformance 命令与报告、兼容矩阵。

退出条件：合同和模板能独立跑离线符合性检查；错误语义与宿主权限边界经 ARCH/QA 评审。

### EP-T02 Registry 与原生兼容桥

- [ ] **EP-T02.W1**：盘点四引擎现有 db_type/engine ID、能力 ID、API 路由、菜单及历史回执引用；建立兼容清单。
- [x] **EP-T02.W2**：实现 Registry 合并与原生身份/发布者保护；产品身份与 protocol/family 分开。
- [ ] **EP-T02.W3**：定义不可变 catalog 快照、generation 和不可用原因；兼容桥只转换已登记请求。
- [ ] **EP-T02.W4**：登记静态分支的迁移负责人及删除条件；不以增加 DomesticDB 数组项宣称支持新产品。

交付物：目录 Schema、兼容桥、旧路径清单和迁移核对表。

退出条件：四原生旧 ID/路由合同保持；未知产品及普通包冒名无法注册；目录未安装状态真实。

### EP-T02b 四原生源码抽取与行为基线

- [ ] **EP-T02b.W1**：分别抽出 Oracle/MySQL/PostgreSQL/SQL Server 的连接、识别、采集和能力实现到四个独立源码包；不得依赖平台业务模块的私有导入。
- [ ] **EP-T02b.W2**：逐能力记录迁移前输入/输出、错误、权限及副作用语义；同一脱敏夹具对照旧实现与 Provider。
- [ ] **EP-T02b.W3**：保留旧路由的宿主授权与任务桥；明确每个静态执行分支最终由哪个 Provider 接管。
- [ ] **EP-T02b.W4**：产出四包构建入口和依赖声明；M0 的 runner 只证明代码/合同等价，不冒充签名制品、驱动或热更新通过。

交付物：四 Provider 源码、行为基线、残余分支清单；运行时接管留 T02c。

退出条件：四包源码和行为对照齐备；主程序残余执行分支全部有 T02c 接管项。

### EP-T14a 测试环境、介质与许可证准入

- [ ] **EP-T14a.W1**：盘点既有矩阵并准备环境计划/cell catalog，资源与签名演练设施逐格记录；环境可用性不反推产品发布范围。
- [ ] **EP-T14a.W2**：C70 按 runtime/core/pminus1/dm/kingbase/native 六 variant 及 cell/context 独立签发，实例各记状态/证据/联合验收；native 按四产品与 OS/CPU 分 cell。
- [ ] **EP-T14a.W3**：分格提供 runtime-ready/core-ready、最小权限账号引用、隔离/清理与负载预算；提供方整任务未结束不妨碍已签叶输出被消费。
- [ ] **EP-T14a.W4**：盘点真实 P-1 原件/资格/Schema/脱敏快照；缺失只记 baseline_absent 并触发 T05b，C78 合格后再签 pminus1-ready，不能把缺失当通过。
- [ ] **EP-T14a.W5**：落实 E01–E04/E06 的实际介质、许可、版本/ABI和资源；缺失只阻断对应 cell。只产事实与 cell catalog，发布 required cells 由 REL/T14b 先冻结。

交付物：环境清单、许可证/SBOM 依据、构建机标签、数据隔离/清理方案及分格准入证据。

退出条件：各 ready 输出分别由 QA/OPS/REL 验证后开放对应引擎；一个引擎未就绪不阻断另一个已就绪格。

可独立签发输出：`cell-catalog`、`core-ready`、`dm-ready`、`kingbase-ready`、`native-ready`、`plan`、`platform-pminus1-ready`、`runtime-ready`。

### EP-T03 签名包、构建与密钥生命周期

- [x] **EP-T03.W1**：冻结 .tmengine 清单、规范化签名范围、文件摘要、包用途、OS/CPU/ABI 及离线依赖格式。
- [x] **EP-T03.W2**：实现安全解包/完整性验证与构建分类：自研 self-built 比较锁定输入的规范化 payload；第三方 opaque 锁来源/hash/SBOM/签名状态及许可，不承诺源码重编译；文件分类穷尽互斥且整包验签覆盖全部文件。
- [ ] **EP-T03.W3**：建立离线根与用途受限的发布签名密钥：生成/双人保管/轮换/吊销、离线信任根更新及反回滚。
- [x] **EP-T03.W4**：明确轮换后旧包的安装/回退规则、吊销不可自动恢复、构建机失陷后的冻结/重建/重新签名流程。
- [ ] **EP-T03.W5**：交付原生包随首装的组装合同，复用现有发布能力时隔离平台包与引擎包信任用途。
- [x] **EP-T03.W6**：交付 production-trust-handoff 合同、测试/生产 namespace 与 Key ID 隔离及公开字段模板；OPS 保管、独立 SEC 批准，C83 演练仅签 test-trust-ready，真实 E07 交接单独签 production-trust-accepted，不把生产私钥带入开发/CI。

交付物：打包/验签工具、密钥与信任根 runbook、SBOM、构建来源和重现性证据。 test-trust-ready、生产交接合同/公开字段模板、分离信任目录与 E07 准入规则。

退出条件：签名、解包、密钥和离线根故障向量通过；无合法驱动再分发依据的格不得产出宣称完整的客户制品。

可独立签发输出：`production-trust-accepted`、`test-trust-ready`。

### EP-T05b 缺失旧版时的桥接基线 bootstrap

- [ ] **EP-T05b.W1**：仅 E06 盘点为 no_qualified_pminus1 时触发；冻结当前 Schema/API/writer 行为及下版 additive 兼容合同，由 T05a 责任人审签合同，不依赖 T05a 全任务，也不执行未来 DDL。
- [ ] **EP-T05b.W2**：从当前单体生成桥接 release B：只修正妨碍兼容保留的旧写行为，保持既有业务；不加入 SDK/Provider 热更、Router、DM 或未来 B+1 功能。
- [ ] **EP-T05b.W3**：在真实元数据库实验格做当前→B 维护式安装、旧业务回归与数据对照；此过程不宣称无中断，也不授权修改现有客户部署。
- [ ] **EP-T05b.W4**：独立构建并冻结 B 的原始 bytes/digest、source/toolchain/SBOM、Schema/脱敏快照和资格记录；后续候选测试期间禁止给 B 打补丁，需变更则生成另一个基线并重新认证。
- [ ] **EP-T05b.W5**：由 QA/REL 联合确认资格后交 T14a 登记 platform-pminus1-ready；已有合格旧版时保留盘点依据，明确 not_required_by_gate，不写成测试 skip 通过。

交付物：条件触发工单、兼容合同、冻结桥接 B 制品/快照/资格证据；未触发时为合格旧版盘点依据。

退出条件：触发分支下 B 对原功能/数据不漂移、制品冻结和维护式边界通过；缺资格不得开放 T05a/T09 完整验收。

### EP-T05a Schema 迁移、回填与双版本读写

- [ ] **EP-T05a.W1**：由 CORE 迁移负责人独占平台库 DDL、revision 和唯一 migrator 租约；Provider/API 候选不能自行启动迁移。
- [ ] **EP-T05a.W2**：增加显式 engine_id 与执行版本字段，保留 db_type/API 兼容；只映射已知原生值，未知产品进入待校正，不能猜成某个国产库。
- [ ] **EP-T05a.W3**：对实例、任务、审批、回执进行可续跑回填；无可靠摘要证据的旧待执行任务冻结，重新 prepare/审批，不能填当前版本伪造历史。
- [ ] **EP-T05a.W4**：实现 P 对旧 writer 的 fallback/兼容写；对新语义采用附表或 CAS，验证未经修改 P-1 制品不会覆盖或丢数据。
- [ ] **EP-T05a.W5**：交付 expand/backfill 校验、迁移前备份/恢复演练计划与只退代码规则；contract 单独门禁，保留新表/字段/记录。
- [ ] **EP-T05a.W6**：输出 migrator-contract：revision/checksum、前后置校验、migration_required、可续跑/对账条件及 P/P-1 兼容范围；唯一执行者复用数据库锁，部分 MySQL DDL 不宣称恰好一次。

交付物：平台迁移脚本、映射及例外清单、回填/核对工具、兼容读写和恢复报告。

退出条件：真实 P/P-1 制品混合读写、重复回填、并发旧写、断点恢复及代码回退通过；未支持的元数据库组合明确不认证。

可独立签发输出：`migrator-contract`。

### EP-T04 Provider 本机进程运行时与安全 IPC

- [ ] **EP-T04.W1**：实现 Supervisor 与不可变运行目录，按平台创建 UDS/命名管道，校验 ACL、OS 对端和每次启动双向身份。
- [x] **EP-T04.W2**：实现受限帧/固定方法/消息 Schema、deadline/cancel、并发和队列上限，禁止动态反序列化及任意执行入口。
- [ ] **EP-T04.W3**：实现宿主凭据租借、TTL/撤销/审计；Provider 自管受信连接池并上报，撤销后合作关闭，超时终止 worker；TTL 不等于已交付数据库密码失效。
- [x] **EP-T04.W4**：实现退避重启、子进程回收、连接清理、崩溃隔离和资源上限；含副作用未知时交 T05 对账。
- [ ] **EP-T04.W5**：把四原生构建包纳入同一启动/自检路径；冻结部署不能依赖临时解包目录或开发机 venv。
- [ ] **EP-T04.W6**：采集 single-provider-capacity-v0：目标 OS/ABI 上冷/热启动、RSS/子进程、连接/句柄、退出与排空；先 calibration 再冻结正式 run，不以 fake profile 数据认证真实驱动。

交付物：Supervisor、IPC/broker、默认限制及运行诊断、原生包初始运行自检。

退出条件：实际目标 OS 制品能受控运行，伪造对端/超限/过期凭据/崩溃重启负例通过；进程隔离不冒充安全沙箱。

可独立签发输出：`single-provider-capacity-v0`。

### EP-T05 任务版本绑定、再鉴权与独立结果解码

- [ ] **EP-T05.W1**：把 engine、核心 worker release、Provider/runtime 摘要、SDK/task/result Schema 固定到持久任务及审批冻结摘要。
- [ ] **EP-T05.W2**：prepare/执行/恢复/verify/回执各阶段再鉴权和范围核验；operation/attempt/fence 幂等持久化。
- [ ] **EP-T05.W3**：版本缺失、被吊销或执行效果未知分别阻断/对账；重试不静默换最新版。
- [ ] **EP-T05.W4**：实现核心管理的标准结果快照及版本化 decoder；历史解码不加载 Provider 任意代码。
- [ ] **EP-T05.W5**：交付普通业务 CLI/测试客户端 submit/read-result，走平台登录与业务权限而非本机管理员包管理通道；为 M1 的连接/执行/回读提供明确入口。
- [ ] **EP-T05.W6**：迁移后的 legacy 历史结果可读，旧待执行任务的重新授权路径可审计。

交付物：版本绑定模型、业务 CLI、路由/对账协议、核心 decoder、任务守恒与幂等证据。

退出条件：任务、审批与回执固定同一执行身份；撤权生效、重复副作用被阻断；删除无执行引用 Provider 后标准结果仍可读。

### EP-T13a 最小本机包管理与安全准入

- [x] **EP-T13a.W1**：实现 list/inspect/verify/install/initial-activate/status 与未接流初激失败 recover；journal 持久化，命令幂等。
- [ ] **EP-T13a.W2**：本机管理员通过受保护管理 IPC 授权；服务验证 OS 身份、安装根、包摘要及完整输入，禁止远程命名管道客户端。
- [x] **EP-T13a.W3**：从受控导入区打开包、固定句柄与摘要，拒绝任意路径/符号链接/重解析与任意 executable，防止检查后置换。
- [x] **EP-T13a.W4**：管理操作写不可遗漏的审计：操作者、操作 ID、摘要、代次、结果及拒绝原因。
- [x] **EP-T13a.W5**：M1 的 rollback 只限未接流初激失败或隔离实验室离线版本回退；业务热回退由 T06a-core/T13b 提供。

交付物：最小 CLI、管理服务、安装 journal/recover、授权及审计报告。

退出条件：从合法离线包到初装/激活/运行身份可核对；并发/半安装可恢复，未授权和路径攻击被拒绝。

### EP-T11 DM 真实只读首包闭环

- [ ] **EP-T11.W1**：按 dm-ready 锁定合法驱动与独立运行时；实现 DM 强身份、兼容模式、权限和错误映射。
- [ ] **EP-T11.W2**：先开放一个只读能力（基础指标/会话查询），产出签名包，真实库验证 install→connect→invoke→持久结果→CLI/API 回读。
- [ ] **EP-T11.W3**：逐项补充已声明拓扑、指标/会话/锁和只读巡检/报告结果读取；未实现或缺证据的能力明确 unsupported。
- [ ] **EP-T11.W4**：不依赖 UI/AI 或热更新；不开放备份、切换、DDL 等未单独验收的写能力。

交付物：DM 包、真实目标报告、能力支持表、安装和结果回读证据。

退出条件：实际离线制品、驱动、DM 目标和持久结果全通；能力逐项认证，缺失关系/延迟不假健康。

### EP-T07 API / Scheduler / Worker 执行角色拆分

- [x] **EP-T07.W1**：拆分进程启动与角色配置；候选 API 不创建有效 Scheduler 或抢任务。
- [ ] **EP-T07.W2**：实现 Scheduler 租约、任务 claim、续租和 fencing；所有任务确认依赖持久回执。
- [ ] **EP-T07.W3**：运行版本化 Worker，旧任务恢复仍路由原核心/Provider 身份，不能被候选版本抢走。
- [ ] **EP-T07.W4**：覆盖接管前后故障、租约过期和跨版本排队/执行/对账任务守恒。

交付物：角色启动入口、租约/claim 协议、守恒计数和恢复报告。

退出条件：多角色同时运行不双消费/双执行；API 生命周期与旧 Worker 排空可独立控制。

### EP-T06b 多执行节点 generation 与租约热更新

- [ ] **EP-T06b.W1**：冻结 membership_epoch 节点快照与容量；新版本任务只派给已就绪相同摘要节点。
- [ ] **EP-T06b.W2**：处理节点加入/离开、网络分区、陈旧 generation/fence、慢节点和租约过期。
- [ ] **EP-T06b.W3**：回切和恢复核验每节点真实运行身份，旧任务引用不因节点替换而换版本。

交付物：多节点协议、节点状态/摘要快照、故障与路由证据。

退出条件：至少两执行节点故障演练通过；未预备节点无法接新代次，已过期节点不能提交有效副作用回执。

### EP-T13b 完整本机生命周期与引用保护

- [ ] **EP-T13b.W1**：扩展 upgrade/热 rollback/disable/uninstall/gc，复用 T06a-core 更新事务，继承本机身份和审计限制。
- [ ] **EP-T13b.W2**：卸载前检查 active/last_known_good/回退基线/在途/待对账任务；停用只阻止新调度。
- [ ] **EP-T13b.W3**：并发管理请求使用每引擎 operation lease 与 CAS，拒绝陈旧激活、双安装和越权重放。
- [ ] **EP-T13b.W4**：清理无执行引用的包后验证历史结果由核心 decoder 回读；平台控制与 Provider 控制权限不互相绕过。

交付物：完整 CLI、引用查询和 GC、恢复/拒绝审计报告。

退出条件：引用保护、并发管理和崩溃恢复通过；不删共享数据或仅因要回收磁盘而强杀未知效果任务。

### EP-T02c 四原生制品接管与独立更新

- [ ] **EP-T02c.W1**：逐个产出并签名四原生制品，首装组装默认携带；通过同一 Supervisor 和 Provider 身份运行。
- [ ] **EP-T02c.W2**：逐个切换旧业务路由，保留宿主权限/任务桥，删除清单中的主进程引擎执行分支。
- [ ] **EP-T02c.W3**：用实际旧行为基线、真实驱动和目标验证等价；对每个原生包独立升级/回退，核心与其他包摘要不变。 同时逐 engine/cell 输出 native-vv1-pair，锁定 V/V+1 实际 source payload 摘要、非 metadata-only diff 与可验证复核证据，供 C84 精确消费。
- [ ] **EP-T02c.W4**：验证首装失败不假成功、末个可用包受保护、禁止主进程静默 fallback。

交付物：四签名 Provider、路由迁移关闭表、制品/驱动/独立更新回归。

退出条件：四个原生包都可独立交付/更新/回退，所有残余分支关闭；仅完成 Registry 或 M0 抽取不能结项。

可独立签发输出：`native-vv1-pair`。

### EP-T10 Catalog 同源 UI 与能力桥

- [ ] **EP-T10.W1**：连接字段/目录/能力/专业页绑定同一 catalog_generation；缓存按 generation/权限上下文失效。
- [ ] **EP-T10.W2**：保留现有页面布局、旧路径和用户自定义卡片；显示支持/不可用原因，不能擅自重做入口。
- [ ] **EP-T10.W3**：AI 仅调用已登记且当前有权能力；无模型进入专业页时不触发采集/计划/执行。
- [ ] **EP-T10.W4**：切换实例、撤权、热更时丢弃陈旧目录响应，已有任务按原版本回读。

交付物：目录 API/缓存协议、页面最小接入改动、浏览器/API 证据。

退出条件：以 catalog/fake/实际 Host 的 L1/L2 证明 generation、权限/表单一致与无模型导航零执行；真实 DM/金仓/原生页面 L3 留 C48，不阻断早期 UI。

### EP-T12 KingbaseES 真实只读首包

- [ ] **EP-T12.W1**：按 kingbase-ready 验证 ksycopg2/libkci 候选及实际 ABI，不因协议兼容冒充 PostgreSQL。
- [ ] **EP-T12.W2**：实现产品/实例/集群强身份、兼容模式、权限、只读会话和能力参数/结果映射。
- [ ] **EP-T12.W3**：实际包验证连接、拓扑/指标/会话/锁、已声明巡检和报告结果回读；未声明能力保持不可用。
- [ ] **EP-T12.W4**：复用 SDK、包、目录和任务机制，不为金仓在宿主添加执行特例。
- [ ] **EP-T12.W5**：交付 kingbase-vv1-pair：真实驱动 V 与经审核源码实际不同的兼容 V+1、差异/来源/签名和能力证据；C46 仍验首包，真实组合切流由 C84 验证，不在首包任务暗加 core/UI 依赖。

交付物：KingbaseES 包、支持矩阵、真实驱动/数据库和回读证据。

退出条件：真实制品与目标逐能力通过，兼容模式身份负例及错误映射通过。

可独立签发输出：`kingbase-vv1-pair`。

### EP-T08 稳定 Router 与部署启动器

- [ ] **EP-T08.W1**：固定对外地址与 Router 生命周期，A/B 使用不同内部监听；构建发布内容独立不可变。
- [ ] **EP-T08.W2**：实现请求/下载在途计数、排空、旧 hash asset 保留和旧页面 API 兼容。
- [ ] **EP-T08.W3**：EXE/App 的运行目录、子进程和本机安全通道不得依赖临时解包路径；盘点各 OS 部署入口。
- [ ] **EP-T08.W4**：Router/Supervisor 自身更新单独实现并验证监听接管或冗余入口；缺路径的基础包不得标记热更就绪。
- [ ] **EP-T08.W5**：接 T05a migrator-contract：启动器调用专用 migrator，API/Worker 在 managed-hot 仅校验；持锁者执行并持久 checkpoint/回执，多启动并发不产生多个有效迁移者，部分 DDL 先核对再恢复。
- [ ] **EP-T08.W6**：输出 platform-ab-budget-v0：真实平台 A/B、Router/Worker 与选定 Provider profile 的联合预算和本次资源预检；缺匹配容量或资源不足拒绝候选就绪。

交付物：Router/启动器制品、路由/排空与资产保留协议、基础自更新报告。

退出条件：固定入口和在途连续性经实际部署制品验证；启动器自身与业务进程更新分别出证据。

可独立签发输出：`platform-ab-budget-v0`。

### EP-T09 平台蓝绿与组合升级/回退

- [ ] **EP-T09.W1**：实现完整兼容图、候选 readiness、route CAS、旧版本排空及 P-1 代码回退。
- [ ] **EP-T09.W2**：T05a 脚本是硬依赖；预检平台/SDK/Schema/Provider/Worker/资源，不兼容先交桥接版。
- [ ] **EP-T09.W3**：实现平台+Provider 组合更新计划：先验证兼容交集，按有向步骤切换，每步有补偿；不能两个指针一起盲切。
- [ ] **EP-T09.W4**：将每个持久阶段的故障恢复与真实路由/运行身份对账，保留共享数据与原任务版本。
- [ ] **EP-T09.W5**：平台首发为单机 A/B 业务进程；多平台节点滚动不能用 T06b 的 Provider 节点证据替代。
- [ ] **EP-T09.W6**：readiness 校验实际 migration receipt/revision/checksum 和三份精确 v0；缺失或部分 DDL 未对账不接流，旧平台继续。hot 回退禁止 down migration 与自动共享库 restore，维护式恢复另走授权入口。

交付物：蓝绿与组合升级执行器、预检/恢复 runbook、运行和数据对照报告。

退出条件：单独/组合更新回退、前后故障及 P/P-1 双版本合同通过；升级不引入重置/5xx/任务丢失或重复副作用。

### EP-T14b CI、真实制品矩阵与容量基线

- [ ] **EP-T14b.W1**：接入各主责执行器与真实签名制品 CI；REL 在 run 前冻结 release_scope_manifest 和精确 required cells/capacity profiles，不能按已有环境倒推支持范围。
- [ ] **EP-T14b.W2**：运行六引擎真实 V/V+1 与多节点/OS/CPU/ABI矩阵，签发 channel=test 的 release-matrix-ready:test；缺/跳过/未认证格不能汇总通过。
- [ ] **EP-T14b.W3**：C67 按 profile 测最终真实驱动峰值/启动/RSS/连接/排空，输出 qualified-capacity-profile；C84 必须精确消费同一 engine/OS/CPU/driver/payload/load/context 的通过实例。
- [ ] **EP-T14b.W4**：核验来源/许可证/第三方 hash 与清理，分层保存 SOURCE/ART/PREPROD/FIELD 证据，不以 test 根结果签生产准入。
- [ ] **EP-T14b.W5**：正式渠道额外校验 E07、同一已测 payload 的生产签名外层、生产 verifier、签名前后 payload 摘要不变与受控 verify/install smoke，再签 production-release-ready；test 矩阵可先供 T15a 消费。

交付物：CI 配置、执行矩阵、容量报告、制品/SBOM/签名与发布证据索引。

退出条件：所有宣称格无 skip/缺证据；容量和组合升级报告可复现，QA/REL 签发布门禁。

可独立签发输出：`production-release-ready`、`release-matrix-ready:test`。

### EP-T15a 预生产负载、热更新与回退演练

- [ ] **EP-T15a.W1**：以可交付离线制品在预生产首装六引擎，执行需求/能力/目标/持久结果/API/浏览器全链。
- [ ] **EP-T15a.W2**：在冻结负载下分别演练 Provider、平台、组合更新/回退及失败，记录请求与任务守恒。
- [ ] **EP-T15a.W3**：比对升级前后数据、权限、审批、旧结果和运行摘要，出具预生产准入/拒绝结论。

交付物：预生产验收报告、缺陷关闭记录、正式候选包与现场步骤稿。

退出条件：精确 test 矩阵、预生产全链/负载/失败/回退与浏览器证据齐全，无未关闭 P0/P1；不等待生产根仪式，不关闭现场。

### EP-T15b 客户现场首包验收独立项目

- [ ] **EP-T15b.W1**：单独取得 E05 和现场所需 E01–E03：窗口、目标范围/账号、网络、版本介质许可、验收人和回退责任人。
- [ ] **EP-T15b.W2**：对现场实际版本执行已授权的首装/只读能力与结果回读；热更新/故障演练按批准窗口和范围执行。
- [ ] **EP-T15b.W3**：明确客户不允许执行的用例和未完成条件，不能用实验库或预生产结果替签现场通过。

交付物：现场准入清单、版本/包摘要、现场结果与验收记录、剩余项。

退出条件：现场授权/窗口/网络与正式 production-release-ready（已测 payload 生产封装/验签/smoke）均具备后执行 C72，客户证据及签字齐备才关闭。

### EP-T06a-core 单节点 Provider 热事务 core（签名 fake / L1-L2）

- [x] **EP-T06a-core.W1**：交付由测试根签名、走同一 Supervisor/RPC 的 fake Provider V/V+1，副作用仅进入隔离测试账本，不访问客户目标。
- [x] **EP-T06a-core.W2**：实现 verify/stage/candidate_ready/generation CAS/drain/recover；新任务切新版本，旧排队/执行/对账任务保持精确摘要。
- [x] **EP-T06a-core.W3**：在切流前后注入崩溃、重放、陈旧 epoch、吊销和超时；复用受控内部管理接口，不等待真实 DM 或完整 CLI。
- [x] **EP-T06a-core.W4**：采集 dual-generation-capacity-v0：V/V+1 并存进程/RSS/队列/连接/句柄、排空和泄漏；与单进程 v0、制品和负载 profile 绑定。

交付物：core 更新事务、测试根签名 fake 包、dual-generation-capacity-v0、L1/L2 故障和排空证据。

退出条件：签名 fake Provider 的真实进程/IPC 下 generation、旧任务固定、崩溃恢复与容量 v0 通过；不宣称厂商驱动已认证。

可独立签发输出：`dual-generation-capacity-v0`。

### EP-T06a-real DM Provider 真实连续性（L3）

- [ ] **EP-T06a-real.W1**：消费 T11 真实 DM V 并构建经审核源码实际不同的兼容 V+1；记录源码 diff、self payload 差异、包/驱动/运行时摘要和 test-channel 签名，输出 dm-vv1-pair，拒绝只改版本号/签名外层的候选。
- [ ] **EP-T06a-real.W2**：在精确 dm-ready 目标格运行只读任务负载，核验强身份、真实驱动连接、持久结果与 V/V+1 切流连续性。
- [ ] **EP-T06a-real.W3**：执行 C22/C79 真实驱动取消/终止/切流前后故障，核对连接、回执、旧任务版本与未知效果对账；缺 DM 格只阻断此线。

交付物：dm-vv1-pair、源码 diff/构建来源、真实 DM C22/C79 及连接/结果证据。

退出条件：真实 DM V/V+1 源码与 payload 的实质差异、驱动/身份/连接和更新连续性及真实故障对账通过；不能以改版本号代替。

可独立签发输出：`dm-vv1-pair`。

## 状态、外部输入与不可降低的条件

所有真实任务 planned，实际 test_instances/output_instances 为空。planned→ready→in_progress→review→accepted 的 ready 需精确输入和签署；未绑定 selector、失败、skip、缺证据不能记通过。T05b 的不触发须有合格旧版证明，非 skip pass。

无真实 DM/金仓/原生格不能关闭相应 L3；core 的签名 fake 进程不认证厂商驱动。MySQL 非事务 DDL 不承诺恰好一次，部分结构先核对/对账；hot 回退不执行 down 或自动恢复共享库。第三方原始 hash 漂移/来源不明阻断，自研 payload 才承诺两 clean builder 摘要一致。

C83 rehearsal 仅测试根/职责演练，真实 E07 需 OPS 托管及独立 SEC 公开交接签署。生产私钥不入开发/CI；test 矩阵不能标生产就绪。API/普通业务任务权限与本机管理员包管理权限保持分离。

## 唯一主责案例索引

| 主责任务 | 案例条目（父分组按叶实例关闭） |
| --- | --- |
| EP-T01 | EP-C09、EP-C50、EP-C51、EP-C52、EP-C53、EP-C54、EP-C55、EP-C56 |
| EP-T02 | EP-C02、EP-C03、EP-C06、EP-C57 |
| EP-T02b | EP-C49 |
| EP-T14a | EP-C70 |
| EP-T03 | EP-C15、EP-C16、EP-C17、EP-C19、EP-C58、EP-C59、EP-C73、EP-C83 |
| EP-T05b | EP-C78 |
| EP-T05a | EP-C33、EP-C64、EP-C74 |
| EP-T04 | EP-C05、EP-C10、EP-C13、EP-C14、EP-C18、EP-C60、EP-C61、EP-C62、EP-C63、EP-C80 |
| EP-T05 | EP-C08、EP-C11、EP-C12、EP-C24、EP-C65、EP-C68、EP-C76、EP-C77 |
| EP-T13a | EP-C04、EP-C20、EP-C69、EP-C75 |
| EP-T11 | EP-C41、EP-C42、EP-C43 |
| EP-T07 | EP-C29、EP-C30 |
| EP-T06b | EP-C37 |
| EP-T13b | EP-C25、EP-C26 |
| EP-T02c | EP-C01、EP-C47 |
| EP-T10 | EP-C39、EP-C40 |
| EP-T12 | EP-C44、EP-C45、EP-C46 |
| EP-T08 | EP-C28、EP-C35、EP-C82 |
| EP-T09 | EP-C27、EP-C31、EP-C32、EP-C34、EP-C36、EP-C38、EP-C66 |
| EP-T14b | EP-C07、EP-C67、EP-C71、EP-C84 |
| EP-T15a | EP-C48 |
| EP-T15b | EP-C72 |
| EP-T06a-core | EP-C21、EP-C23、EP-C81 |
| EP-T06a-real | EP-C22、EP-C79 |
