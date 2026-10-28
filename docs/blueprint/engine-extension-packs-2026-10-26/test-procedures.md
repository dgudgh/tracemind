# 测试组织、叶实例与执行步骤

版本：0.3，2026-10-26。S1 实际交付 v3 文件与检查器；所有 Provider/制品/数据库/迁移/密钥/预生产/现场用例仍待执行。当前 test_instances/output_instances 为空，合成规划 fixture 另存 readiness-fixtures.json，不能回写为业务通过记录。

## 1. 实例与输出门禁

### 1.1 记录模型

案例条目包括普通案例与 C67/C70/C73/C83/C84 父分组。父节点只计划/归类，不直接执行、不计通过；每个实际叶实例的唯一键为：

```text
(case_id, variant_id, cell_id, requirement_context_digest)
```

每个实例独立记录 state、executed、输入、oracle、evidence_refs、acceptance；每个执行实例一个 primary_task。C73 的通过叶必须在实例内嵌 oracle_receipt 并由对应 machine oracle 校验，不能仅有 generic evidence/签字。C84 test-matrix/production-envelope 仅是 output_instances 中的派生收据，禁止进入 test_instances 或 executed=true，也不计业务执行。可执行叶通过要求 executed=true、实际证据和所需 QA/联合验收角色，不能从父状态复制子项通过。派生汇总收据不直接执行，必须递归核对自己的完整前置及已执行叶证据。新结果先核对源/制品/目标/profile/context，未绑定 selector ref 不就绪。

variant/cell/context/output selector 的 ref 是待从已冻结上下文解析的字段引用，不是通配符或已验证值。运行前必须解析成精确字串；缺字段或错 cell/context/output 均 not_ready。提供方整任务未 accepted，不妨碍已经验收的独立输出被消费。

### 1.2 五个分组

| 家族 | 叶定义 | 关键拒绝条件 |
| --- | --- | --- |
| C70 | runtime/core/pminus1/dm/kingbase/native，按实际 OS/CPU/引擎/ABI 再分 cell | DM 失败不污染 native.mysql；缺 P-1 只触发 T05b，不签 ready；父 passed、错 selector 拒绝 |
| C73 | self-payload/third-party-integrity/incident-rebuild | 自研差异、未知源或第三方原始 hash 漂移、分类遗漏/重复阻断；opaque 不强求厂商源码重建 |
| C67 | REL 冻结 required_capacity_profiles 对应的真实 profile | C84 必须拿同 engine/OS/CPU/driver/payload/load/context 的通过容量输出 |
| C83 | rehearsal / production-handoff | rehearsal 不满足 E07；生产根真实托管/独立 SEC 批准缺失仍 waiting_external |
| C84 | 六引擎 real-combination 叶实例与非直接执行的 test/prod 派生汇总 | 每真实叶须匹配 V/V+1 pair、C67 与多节点证据；单条 passed 收据或仅 MySQL 不放行最终矩阵 |

普通案例也要保存具体 variant/cell/context，不因其不属于上述分组而免除版本和证据绑定。输出绑定允许的 variant 以 v3 追踪表为准，不能用任意新 variant 冒充已有合同。

### 1.3 环境与发布范围

T14a 只登记 cell catalog 和真实叶事实。REL/T14b 在正式 run 前冻结 release_scope_manifest，明确宣称范围、精确 required cells 与 capacity profiles。必需格未执行/跳过/不支持/缺证据均不计通过；不能据当前环境反推产品应支持哪些格。

T03[test-trust-ready] 给开发/CI；E07 + production-trust-accepted 是真实 OPS/独立 SEC 交接。test 矩阵可给预生产。正式渠道还要同一已测 payload 的生产封装、生产公钥 verifier、payload 摘要不变与受控 verify/install smoke，才可签 production-release-ready。正式客户现场在此输出就绪后进行。

## 2. 参数冻结与证据

每个业务 run 固定 run_id、合同版本、源码/平台/Provider/runtime/manifest/驱动摘要、SDK/Schema、generation、cell/context、目标范围与授权、负载/profile、动作与 oracle。证据只保存脱敏引用，凭据/nonce/私钥不能落日志或报告。

开发初始限制沿用合同：frame 解码后 1 MiB，单调用输出 64 MiB，队列 256，IPC 连接 32，每 worker 并发及受信数据库池合同上限 8，连接/握手超时 5s/10s，缺省只读 deadline 60s，secret 发放授权 TTL<=min(60s,remaining_deadline)。它们是保护上限，不是测得吞吐量/SLA。受信 Provider 自管连接，Host 不宣称能强制让已交付密码到期失效。

容量先 calibration、再冻结预算、再正式 run，calibration 不计通过。三份 v0：T04 单进程、core 双 generation、T08 平台 A/B 联合预算。T09 必须匹配制品/OS/ABI/负载/profile/context 并做本次资源预检。真实驱动不能借 fake RSS/连接数获认证；最终 C67 的实例输出先于对应 C84 实例。

建议待开发执行器接口：

```text
<case-runner> preflight --case <case> --variant <variant> --cell <cell> --context <digest>
<case-runner> run --fixture <frozen-manifest> --evidence <run-dir>
<case-runner> evaluate --oracle <oracle-manifest> --evidence <run-dir>
```

这些 runner/产品 CLI 是未来实现，S1 不宣称已经可调用。每个 run 至少保留 environment/parameter-freeze/actions/observations/oracle/acceptance 和逐制品摘要，SOURCE/ART/PREPROD/FIELD 分层，QA 与规定另一责任角色验收。

## 3. M0 与 M1-base / DM 两条出口

1. M0：T01 自带 fake/conformance runner 验 S/S-1、拒 S-2、optional 保留、mandatory/降级拒绝；T02 离线 Registry/身份负例；T02b 四原生源码行为基线。只消费 T14a[plan]，不等待环境任务整体验收。
2. M1-base：T03 测试信任/包分类合同，T05a/条件 T05b 的合格 P-1 和迁移合同，T04 实际 Host/IPC 与单进程 v0，T05 的版本绑定/核心结果 decoder，T13a 最小工具。出口只需 test-trust-ready，不等待生产 root 仪式。
3. T05b 仅 no_qualified_pminus1 时触发，维护式交付并冻结 B；T14a 核验后签 pminus1-ready。已有合格旧版则有 not_required_by_gate 依据，不是 skip pass。
4. M1-DM 独立等待 dm-ready 与 T11 真实首包。没有 DM 时 M1-base/core/原生/单机平台可以按其余输入继续，但相应 DM L3 不能通过。

## 4. S2 / S3 / S4 的实际验收顺序

### 4.1 底座与 core

- T03：测试根/生产 namespace、Key ID 与用途分开；C15/C19/C58/C73 技术合同及 C83 rehearsal，生产交接另记 E07。
- T05a：输出 revision/checksum/前后置校验/migration_required/P-P1/可续跑对账条件。唯一 migrator 不是 MySQL 非事务 DDL 恰好一次。
- T04/C80：签名 fake 的实际进程/IPC、身份/帧/secret/资源/重启回收和单 Provider v0。
- T06a-core/C21/C23/C81：签名 fake V/V+1，事务/代次/旧任务排空/故障/双 generation v0，副作用仅隔离测试账本。不能把 L1/L2 写成厂商驱动认证。

### 4.2 角色、启动接线与单机平台

- T07/C29/C30：API 不抢调度，Worker 租约/claim/fence 和版本固定，复用 core 而非真实 DM。
- T13b/C25/C26：本机授权/审计、并发管理事务、在用/最后可用/任务引用保护，历史结果由核心解码。
- T08/C82：专用 migrator 接启动器，managed-hot API/Worker 不 DDL；竞争持锁者、checkpoint/实际结构/回执核对，缺输入拒候选 ready；产平台 A/B 预算。
- T09/C27/C38/C66：六硬依赖与三 v0。先校验迁移回执/结构/容量，再 readiness/route CAS/drain。旧平台继续；hot 回退从不 down/自动共享库 restore。
- C38 kill 点含获锁前后、部分 DDL、校验后回执前、回执后候选 ready 前。未知部分先 reconcile，不盲重放。

### 4.3 真实引擎、多个执行节点与交付

- DM/C22/C79：消费真实 DM V 和经审核源码实质不同的 V+1。保留源码 diff/self payload 比较与构建来源，排除只改版本号/签名外层。真实连接/取消/终止/切流故障及未知效果逐项 L3。
- 原生/T02c/C01/C47：四独立签名包逐个接管，真实驱动/旧行为/独立更新回退；不因 DM 未就绪阻断已具备格。
- 金仓/T12/C44–C46：首包不等 UI/core；交 kingbase-vv1-pair 给 C84，首包不暗加切流案例。
- T06b/C37：至少两执行节点，membership_epoch/租约/分区/容量和陈旧提交拒绝；不进入 T09 单机硬依赖。
- T10/C39/C40：先 catalog/fake/实际 Host 的 L1/L2，不等待 DM；无模型导航零执行。真实引擎页面在 C48 完整链复验。
- T14b/C67→C84：先精确容量 profile 再真实六引擎组合两种顺序、第二轴失败和回退；C66 的 fake 组合不代签。
- T15a/C48：消费 test 矩阵做六引擎预生产全链/负载/失败/回退。
- T15b/C72：E05 现场条件及 production-release-ready 都具备后，使用已验证正式封装做独立现场验收；未获授权项目保持未执行。

## 5. 主要案例组的 fixture / 动作 / oracle

| 组与案例 | fixture / 动作 | oracle / 证据 |
| --- | --- | --- |
| SDK/Registry C02/C03/C06/C09/C49–C57 | S/S-1/S-2、旧记录与未知字段/身份/SQL 正反向量 | 原 ID/路由/参数权限保持，未知 mandatory/降级/越权拒绝，M0 只关闭源码合同 |
| 供应链 C15–C19/C58/C59/C73 | 合法/篡改包、两 clean builder、opaque bytes、根轮换/门限泄露演练 | 自研 digest/第三方 hash/SBOM/许可分别验；全部字节仍验签，根门限泄露停自动更新/带外重锚定/盘点，演练不代生产交接 |
| Host/IPC C05/C10/C13/C14/C18/C60–C63 | 错 peer/反射/重放/超限/超时/secret/崩溃 | 帧/seq/身份/边界有效，池是受信合同，终止后会话/效果未知对账，TTL 不假称密码失效 |
| 迁移/任务 C08/C11/C12/C24/C33/C64/C65/C74/C76 | 真实合格 P-1、旧写/断点、撤权/重复/断线，C76 早期 fake/实际 Host | 不伪填旧任务版本，冻结重新 prepare/审批，重复效果拒绝；C76 L1/L2，真实 DM CLI L3 在 C41–C43 |
| decoder C68/C77 | 移除 Provider、legacy/未知/篡改/递归 envelope | 不访问目标/执行 Provider，digest/Schema/大小错误拒绝，保留原证据/legacy 真实缺字段 |
| 安装 C04/C20/C69/C75 | 固定导入句柄、OS 身份、路径替换、逐 journal kill | 幂等恢复，无半 active/任意路径；C75 候选故障可 M1 验，C01 产品首装留 M2 |
| core/real C21–C23/C79–C81 | core 签名 fake；real 真 DM pair；匹配 v0 | 分层证据不混，旧任务 pinned，新任务新代次，容量/连接/故障语义可核对 |
| 平台/迁移 C27–C38/C66/C82 | 实际 Router/平台 A/B、migrator/receipt、三 v0、每持久点 kill | 缺回执/部分 DDL/资源不足不切流，旧路由继续，代码回退不逆改共享库 |
| 环境/正式信任 C70/C83 | 每 variant/cell/context、测试根演练与真实 E07 公开交接 | 独立实例签发，父不通过，SEC 与 signer/custodian/build 人员分离，生产私钥不进 DEV/CI |
| 最终组合 C07/C46/C47/C67/C71/C84 | 真实六引擎 pair/OS/ABI、冻结发布范围/容量格/多节点 | C67 精确通过先于对应 C84，未构建/skip/缺格不汇总全绿 |
| 预生产/现场 C48/C72 | test 矩阵与独立现场授权/正式封装 | 预生产与现场各自签署，test 根/实验库不替生产或客户现场 |

## 6. 三个关键逐步闭环

### 6.1 C01 产品首装四原生 Provider

前置：`native-ready` 已签；四个 Provider、产品安装器、driver/native client 和目标库版本均在认证格内；空白主机满足 OS/CPU/ABI 与资源预算；QA/ENG 已登记。

1. 记录主机安装前进程、服务、端口/socket、安装根、目录和摘要；确认不存在历史 Provider。
2. 只运行一次产品安装器，不单独执行 engine install；采集安装器和内含四包 digest、签名链、SBOM、`trust_generation`。
3. 启动平台并读取实际进程树；确认 Oracle/MySQL/PostgreSQL/SQL Server 分别由独立 Provider 进程和不可变目录运行，主进程静态执行探针为零。
4. 对每包核对 manifest↔handshake：canonical engine、V、S/S-1、driver/native、runtime digest、OS/ABI、`launch_instance_id` 和 readiness。
5. 使用最小权限真实目标逐个执行 identify/test_connection 和一个已支持只读能力；记录 DB 侧会话、任务绑定、持久结果和核心 decoder 回读。
6. 重启主机/服务后执行 status/recover，再重复第 3–5 步；不得自动联网补依赖或换 digest。
7. 对照 C49 行为基线、旧实例/能力/API 身份和 C47 路由关闭表。任一包缺失、未 ready、fallback 或目标证据缺失，C01 整体不通过。

必须保留：安装前后清单、产品/四包摘要、签名报告、进程树、四份 handshake、四份真实只读结果、重启恢复时间线、主进程 fallback 探针和 QA/ENG 签字。

### 6.2 C20 install/初激故障恢复

前置：一个合法签名 fake/DM 候选包、隔离安装根、可重复清理的 Host、尚未接收业务请求的 generation、可控 kill/断电注入器。

1. 枚举并稳定命名 journal 点：固定导入句柄后、验签后、临时目录 fsync 后、不可变目录 rename 前后、Registry stage 后、进程启动前后、selftest 前后、initial active CAS 前后、完成标记前。
2. 对每个点从同一干净快照开始执行 install/initial activate，在该点 kill Host；禁止测试脚本直接修正 active 指针。
3. 重启管理服务，先执行只读 status，再执行 recover；采集磁盘、journal、Registry、进程和 active generation 的一致快照。
4. 再执行一次相同 recover 和相同 operation ID，确认幂等；重复 install 不产生同版本不同目录或双 active。
5. 若候选从未接流，可验证 initial rollback/清理；若存在任何服务证据，转交 C23 core 或 C79 真实驱动热回退/对账，不在 C20 宣称回退成功。
6. 对每个注入点检查临时目录、socket、子进程、secret 文件、DB 会话均按合同清理；效果未知必须显式记录。

通过 oracle：只有“旧状态完整保留”或“新状态完整提交”两类稳定结果；不存在半安装 active、双进程、摘要错配、重复副作用或假成功。证据为逐点矩阵、journal/actions、恢复前后快照、第二次 recover diff 和 QA/OPS 签字。

### 6.3 C41 首个真实 DM 驱动/连接/只读调用

前置：`runtime-ready/core-ready/dm-ready` 已签；DM 服务端完整版本、edition/兼容模式、字符集、TLS、OS/CPU 已冻结；dmPython/DPI/native client 的来源、摘要和许可明确；测试数据集和最小只读账号引用已授权。

1. 用 T03 clean builder 生成 DM `.tmengine`（自研 payload 可重复；第三方原始 hash/来源/SBOM/许可单独校验），保存 manifest/payload/runtime/driver/native/SBOM digest 并完成签名验证。
2. 通过 T13a 管理服务执行 inspect→verify→install→initial activate→status；本机管理员只负责包管理，不用其身份执行数据库业务。
3. T04 Supervisor 启动 Provider，核对实际 OS peer、双 nonce transcript 摘要、S/S-1、V、driver/native 和 readiness；日志不得包含 secret/nonce/连接串。
4. 以普通平台业务用户登录 C76 CLI，提交 test_connection/identify；平台校验目标和权限后短租 secret，DM 返回真实产品、完整版本、兼容模式、字符集和稳定实例身份。
5. 通过同一 CLI 提交首个登记的只读能力（建议基础指标或有界会话查询），记录 `operation_id/request_id/fence_epoch`、deadline、任务版本绑定和 DM 侧只读会话。
6. 等待持久回执，使用 CLI `read-result` 和普通 API 分别读取；两者由核心 versioned decoder 得到同一标准结果和 Schema 身份。
7. 停止 DM Provider 进程，在不访问 DM、不重启 Provider 的条件下再次读取已完成结果；必须仍可读。
8. 负例依次使用错误产品目标、错 TLS、权限不足、只读账号尝试未登记 SQL/写能力、过期未消费 lease；错误类别、安全消息和无副作用状态准确。
9. 查询 DM 侧会话，核对活动连接不超过冻结合同并在完成/终止窗口后回收；无法确认的会话或效果进入对账，不假称关闭。

通过 oracle：实际 DM 身份/驱动/ABI 匹配；只读能力成功且结果持久；普通业务用户不需本机管理员；停止 Provider 后结果可解码；负例不越权、不解锁 Oracle/任意 SQL/写能力。必须保留包/driver 摘要、安装 journal、IPC 身份摘要、CLI/API/任务/结果、DM 侧会话与审计、负例和 QA/ENG 签字。

## 7. S1 检查器及拒绝场景

当前可执行的是文档检查器：

```bash
python3 docs/blueprint/engine-extension-packs-2026-10-26/validate_plan.py --self-test --report reports/engine-extension-s1-20261026/validation.json
```

检查器读取实际主目录的 v3 JSON/Markdown、task/case/variant/output selector、依赖与角色，运行隔离合成 fixture 的正反例。它不启动 API/Provider、不构建包、不调用数据库/迁移/密钥操作。证据引用/签署结构检查不替代未来真实验签、真实执行和人类交接。

必验的合成场景：

1. DM failed 或 waiting_external 时 runtime/native.mysql 可独立 passed；wrong cell/context/output 或 unbound ref 不得匹配。
2. T14a/T03 whole 未 accepted，但所需 plan/test-trust/叶输出已签时 M0/M1-base 仍可就绪；E07、DM、金仓、T06b 缺失不反向卡 core/T09。
3. 三份容量/迁移回执不全、checksum/profile/context 不符、部分 DDL 未核对时 T09 not_ready；禁止 hot down/共享库 restore。
4. test 矩阵可供 T15a；缺真实 E07、生产封装 payload 改变、未验签或未 smoke 时 T15b 不就绪，rehearsal 不签 production。
5. 重复执行键、父 executed/passed、未知角色、证据/联合验收缺失、SEC 与保管/签名/build 同人必须被拒。
6. 发布 required cells 为空/未冻结/缺必需格不能过；T14b/C84 必须消费匹配的真实容量 profile，不用 fake 数字认证真实格。

这些自测只验证规划数据判定。实际业务 case 维持 planned；未来每个实例获得完整目标证据与联合验收后才改变执行状态。
