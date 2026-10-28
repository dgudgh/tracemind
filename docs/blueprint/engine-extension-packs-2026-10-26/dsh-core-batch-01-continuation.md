# 给 dsh：连续批次 01 续做单，修复后完成 B00–B07

日期：2026-10-26。主任务为同目录 [dsh-core-batch-01.md](/Users/zhaolongjie/codex/mesdbtools/docs/blueprint/engine-extension-packs-2026-10-26/dsh-core-batch-01.md)。本单补充实际代码审核发现，不替代主任务、不缩减验收标准。dsh 编码与自测，Codex 审核。

## 当前状态与历史说明

用户后续授权 Codex 接手实现，本机 B00–B07 隔离执行链现已补齐。当前状态与真实命令、测试叶、20 轮更新、八点 Host SIGKILL 及单/双容量证据集中在[本轮交付](/Users/zhaolongjie/codex/mesdbtools/reports/engine-extension-core-batch-01-20261026/codex-delivery/delivery.md)。下文“当前阻断”“仍未完成”等保留为历次审核时点，不能再当作最新实现状态；旧失败证据仍保留。正式 EP 依赖、实例和 E07 不因这批 fixture 验证被签发。

## 执行要求

**当前已验证事实（2026-10-26）：** Orchestrator已接通Supervisor/Updater/SQLite，runner使用实算manifest摘要；Codex最新独立复跑61events/generation41/exit0，并确认独立Host退出-9、新Host recover_exit0及SQLite旧只读结果回读。包初建摘要标签和“仅Provider终止”的上一版问题已修，不再按旧诊断重做。但这只是一个Host强杀/回读烟测，原八指定中断点和效果verify尚未逐项验收。

**当前阻断负例（均归原任务）：** D02/D06/D07：V1固定任务提供V2有效签名包，恢复仍exit0并写succeeded，必须在业务调用前核持久task/active/journal与实际Provider身份。D05：formal错误source_digest/OS且cold samples=1仍获candidate；须先核冻结profile/同产物/环境/负载/样本，补30独立warm、200 invoke_read的并发1/4/8与超限，容差入profile。D04/D07/D08：runner清理报告为空但实际遗留独立`/tmp/tm-ipc-*`socket，必须枚举真实owned IPC根与PID/start identity，清理失败影响退出码。证据见[真实Host恢复窄复核](/Users/zhaolongjie/codex/mesdbtools/reports/engine-extension-core-batch-01-20261026/host-recovery-review.md)。

连续完成以上负例与原CrashPolicy/队列/取消、四类旧任务、效果verify、八指定kill点、单双容量及22组证据；不新增批次、不弱化身份/预算/安全前置。已有完整持久的只读结果核验回读，未知写效果保留对账、不盲重试。下方历次诊断保留历史，当前状态以本节为准。

**上一版推进纠偏（2026-10-26，保留历史依据）：** 当时新增State/CrashPolicy仅被单测引用，Updater未调用Supervisor，统一runner只启动V1调用三次。所要求的推进方向是把现有runner变成原批次的集成验收入口：两版签名包→可信启动/身份与SDK校验→真实调度及CrashPolicy→持久Updater/CAS→新旧任务/切流/排空/回退→真实Host kill/recover→结果回读/cleanup。先写出会因未接入组件而失败的完整链测试，再接通实现；任何缺环/预算或身份门未满足均非0退出，不用mock、手改状态或跳过安全前置凑绿。测试需要验证策略实际被Supervisor调用、Updater实际等待候选就绪、runner实际走同一账本和进程。当前实际状态以本节开头的串联复核为准。

一轮完整链只是内部集成里程碑，完成后仍在本批连续扩展为八类kill、四类旧任务、完整30/200/20容量与20轮更新回退；原8项22组最终退出条件全部保留。工作进度以“哪条原执行路径已接通、还在哪个原条件失败”说明，新增类/测试总数只能作佐证。当前未见真实DB/DM/金仓/生产根阻塞本机fixture范围的证据；本单没有要求新增这些外部前置。

继续现有代码，按下述顺序一次性补完：**B00 补漏 → B01/B02/B03 可信执行链 → B04 调度与生命周期 → B05 容量 → B06 持久切换与恢复 → B07 双版本及统一交付**。内部实现/测试问题持续修复，不因一个原型、一次测试总数或一个小项结束就停工询问下一步。

交付必须包含全部当前 OS 必需项的实际结果。无法满足的外部条件独立记录，不能把 `not_run/skipped_by_dependency` 改成通过。保留原单的保护范围、正式依赖和 E07 状态；不操作真实目标 DB/SSH、生产根、平台配置/Schema/启动/前端/服务，不提交推送部署。全部运行在本批独占隔离目录，禁止接管或清理其他进程/目录。

## 0. 最新复核后的直接续做要求

2026-10-26后续修复窄复核：Codex实跑 **107 passed in 2.42s**。无构建根的ghost拒绝、Host严格JSON、长度前缀raw secret传递、active generation查询及事务CAS代码已有修复；回退探针receipt/API均为2。下方第1节104项表为上一轮历史时点，不把已修缺陷当未修，也不据107通过关闭整批。

**仍有三条阻断反例，先归回D01/D03修净：**

1. **D01/C73任意文件名绕过**：当前third分支仅在actual中存在`v.bin`才比较hash。实际`vendor.bin=b'actual'`，raw/packaged填另一份bytes的摘要、SBOM/license任意自报，仍validated=true。必须按每个已登记opaque路径绑定实际hash；回归参数至少覆盖`v.bin`、`vendor.bin`、嵌套路径和多个opaque，不能按测试fixture文件名写分支。
2. **D01/C73 self分类集合错绑**：实际两目录只有`real.bin`，receipt inventory/signed却声明`ghost.bin`并标opaque，只要digest用real.bin计算仍通过。实际路径集合、分类、signed coverage、按self-built筛选的摘要必须一致；增加缺失/额外/重复/重分类负例，不能只证明“目录存在”。
3. **D03/Host与Provider解析不一致**：Host拒绝40层JSON，Provider实际`recv_frame`函数仍接受；二者仍各写parser。统一受信SDK模块并在两端/握手/业务帧运行同一规则，对重复key、NaN/Inf、bool序列、深度/类型/编码均做双端叶测试。Host单测通过不能代替Provider拒绝证据。

证据见[四项修复后窄复核](/Users/zhaolongjie/codex/mesdbtools/reports/engine-extension-core-batch-01-20261026/post-fix-review.md)。其余D02–D08全部保留，**推进顺序是D01补漏→D02/D03可信执行链→D04生命周期→D05容量→D06/D07持久切流与kill恢复→D08双generation/统一交付**；D06纯账本修复可并行，不提前跑未满足前置的正式负载。不能直接跳B04–B07，把未闭合B01–B03留在基础里。

不新增一个批次、不降低原8项/22组验收、不重做已经验证的修复。dsh继续编码和修复，日常可汇报进度，最终统一交付按原单全部退出条件；Codex审核。

## 1. 本次独立核对后的基线

Codex 本轮实际运行当前定向套件：**104 passed in 2.39s**。命令与发现记录在[本轮审核报告](/Users/zhaolongjie/codex/mesdbtools/reports/engine-extension-core-batch-01-20261026/continuation-review.md)。这是现有用例通过，尚无整批交付、单/双容量、kill 恢复或现场通过证据。

| 项 | 当前代码事实 | 验收状态 |
| --- | --- | --- |
| B00 | key 构造/用途、普通 inventory 字节绑定、目录链接已有修复；C73 仍可完全依赖自报 inventory，无实际构建目录；SBOM 仍可指向任意 opaque 本体 | **部分完成，硬门未关闭** |
| B01 | 最小 SDK、descriptor 和4个 SDK 测试；一份 fake 源码按 argv version 切 marker，另一个入口仅给 v2 追加注释 | 版本协商/optional 保留/标准独立 decoder/真实两版未完成 |
| B02 | 签名与字节校验后启动子进程、私有 UDS；入口与完整 runtime 身份未形成受验证闭环 | 部分完成 |
| B03 | 有 HMAC 帧；手写 HKDF、缺双向 OS peer/完整 transcript；重复 JSON key、NaN、bool 序列、深 JSON 仍接受 | **不能判“帧协议完成”** |
| B04 | semaphore8、调用超时及杀进程组；无完整队列、取消、退避和 circuit | 部分完成 |
| B05 | 只启动一次、循环调用 selftest；formal 未核对制品/profile，failed_budget 仍 exit0/ok=true | 原型，完整采样/冻结/失败语义未完成 |
| B06 | SQLite packages/active/updates/tasks 与简化 updater；未接 Supervisor；存在 generation 查询和 CAS 事务边界错误 | 原型，尚非热事务闭环 |
| B07 | 单版本3次调用 runner；无双版本切流、20轮或统一证据目录 | 未完成 |

保持本表为审核时点，后续以新增证据更新，禁止直接覆盖旧失败记录。

## 2. 先修现有缺陷，再接运行链

下列 D 编号是本次缺陷/补工编号，回归仍归原 CB01-01–22；每项需记录测试 node ID、原反例、修复后结果与实际证据。

### D01 / B00：C73 改为实际字节 oracle

位置：`backend/app/runtime/engine_packs/contract.py:220–327`、`backend/tests/test_engine_pack_contract.py`、`fixtures/engine_packs/c73_self_positive.json`。

- `validate_c73_receipt(receipt)` 当前不接受实际构建/文件上下文，重算收据自报列表不能证明磁盘字节。改为明确接收受控构建根/已校验 evidence context；从实际两份构建目录扫描生成 inventory、分类和 self digest，oracle 不得自行输入一个列表就获得 validated。
- 保留“纯 schema 校验”和“已验证实际字节”两种不同结果；若保留仅 receipt 的解析接口，禁止它返回或被消费为字节验证通过。生产模块不依赖蓝图检查器实现 oracle。
- self：虚构 `ghost.bin`、size999、任意合法SHA，再按该列表计算相等 digest，**磁盘不存在仍通过**的旧反例必须修成失败。校验两目录、输入锁/工具链/recipe 的受控身份和实际摘要；本机两个目录仅验 oracle，不冒充两台独立 builder。
- third：重用实际 payload 校验路径，opaque 实际SHA=锁定/packagedSHA，oracle opaque map、SBOM、版本/ABI/source/license/redistribution 全绑定；禁止 receipt 分支绕过 manifest 分支的枚举与字节核对。
- SBOM 必须是独立、可解析、符合本轮冻结最小 Schema 的真实 inventory 文件，包含被引用组件/版本/文件摘要关系；不能把 `sbom_path` 指向任意 `vendor.bin` 再对其 hash 就通过。许可检查不冒充法律授权。
- incident：重算实际 rebuilt payload，绑定真实测试公钥 fingerprint/背书和受控 clean builder context；`clean_rebuild:true`、改名称和自报指纹不能代替实际证据。
- inventory/signed/classification 均拒绝重复、非法类型/路径及不一致。`compute_self_payload_digest` 不能让 bool size、非法 hash、重复路径成为合法摘要。
- 所有入口先做类型校验再取字段；manifest=[]、inventory含不可哈希容器等必须稳定合同错误，不能裸AttributeError/TypeError。补文件/目录/dangling symlink及hardlink；root/dirs/files均核平台reparse标记，未实测Windows junction/reparse保持未验证。
- 验签入口按原任务再核对真实 key identity/policy，避免缓存字段漂移；这是合同防御性要求，**不将同进程任意 `object.__setattr__` 改内存宣称为远程攻击漏洞**。

退出：原 R01–R05/P2 与本次 ghost/third/SBOM/incident/重复反例逐项通过。没有实际目录上下文的 receipt 不得被后续当可信证据。重新写 B00 修复矩阵，保留原 `b00.md` 历史时点。

### D02 / B01–B02：真实两版源码和固定身份链

位置：`engine_sdk/contracts.py`、`engine_host/catalog.py`、`supervisor.py`、`fixtures/engine_providers/`、两个 runner 的 package 构建函数。

- 提供两份分别构建/签名的 fake V/V+1，实际实现有可测行为差异，例如 v2 增加已声明 optional 字段和新的只读计算结果。禁止仅追加注释、version 参数、环境变量或 marker 切换后声称两版。
- 同一份构建产物在传入不同 version 参数时不得冒充另一版；loader/handshake 需把包内版本与受签 manifest 逐项核对。
- 真正将 SDK validate/参数 Schema/capability allowlist 接入 Host 发送和 Provider 派发两端，不只是独立单测里调用；未知方法或错误参数在业务执行计数增加前被拒绝。
- 完整错误 envelope 至少 code/category/retryable/effect_state/safe_message/diagnostic_ref；请求/响应绑定request/operation/attempt/generation/launch/package/runtime/schema。negotiate拒float等非法类型，不能先str()把1.0浮点当合法版本；fake补齐describe/capabilities/validate_config/status/cancel的真实行为。
- S/S-1/S-2 的测试分别覆盖当前 `1.0/0.9/0.8`；另测未知更高线，不把 `2.0` 负例当 S-2。mandatory 拒绝、optional 持久化/回传保留，修正当前全部额外字段一律拒绝与兼容合同冲突的行为。
- catalog按engine/generation/digest持有不可变快照，可让同一engine的V/V+1同时存在并显式选择active；当前仅engine_id去重会拒第二descriptor，必须修复并测双generation共存/晚回执和陈旧快照。
- 固定 Host SDK loader/entry ID，manifest/业务请求均不得指定任意 module/executable；记录 interpreter/SDK/launcher/payload 的真实 runtime identity。仅使用开发 venv 时显式 dev_harness_only。
- staging 复制后重新校验完整 inventory/签名和只读权限；拒源/目标链接替换、预置目录。启动准备在子进程前持久记录所有权（与 D07 接口协商），launch ID 在认证前生成。
- Supervisor/runner强制显式新建、绝对、私有workdir和ownership marker，禁止构造器静默生成无法追溯目录。若UDS路径长度需单独短IPC root，必须新建并记录在本次ownership根集合；所有socket/staging/artifact/PID均可归属，禁止清理未登记根外对象。
- Host/core 自持标准结果 decoder/schema；Provider executable/source 删除且其目录 import/open 被阻断后仍可读取旧结果。

退出：双版本源码/行为/摘要对照、SDK兼容叶、stage篡改/身份不匹配负例均有实际证据，而非文档声明。

### D03 / B02–B03：认证链、二进制 secret 和统一严格 parser

位置：`engine_host/ipc.py`、`supervisor.py`、fake Provider 的 secret/握手/帧代码。

- **修复 secret 截断**：Host 当前写32随机字节加换行，Provider `readline().strip()` 会截断内嵌换行/删除首尾空白。改受限继承 fd/pipe 的固定长度 read_exact 或有界长度协议，32字节原样交付；覆盖每个位置的换行、空格、tab、CR、NUL及分段读取/EOF。不能把随机字节改为低熵字符串逃避测试。
- 双方真实核 OS peer。macOS PID/audit 不可取得时必须 unsupported，不得 `pid=None` 自动退化为仅 UID。Provider 也核对预期 Supervisor 的真实 peer/PID，不只 Host 验 Provider。
- 预生成 launch ID；双 fresh nonce、方向 label、双方真实 PID/身份、Host/engine/V/package/runtime/SDK/deadline 都进入认证 transcript。现有 `session_keys(host_id, provider_id)` 忽略两参数的实现必须移除。
- HKDF-SHA256 与 HMAC-SHA256 使用已有维护库接口，删掉 Host/fake 两套手写 HKDF；提供共同、可离线打包的受信 SDK 实现，两端不得复制出行为不同的密码/解析代码。
- 每方向 session key/sequence，MAC 绑定 connection/launch/direction/序列和消息；严格整型非bool、序列单调、重放/乱序/跨 launch/跨 generation 拒绝。
- 每个response精确绑定request_id/operation_id/attempt/generation/launch_instance_id/package与runtime digest/sequence，Host落账前逐项核对。旧launch/旧generation晚回执只能落其原任务或拒绝，不能仅凭MAC/序列正确写入当前任务。
- handshake 与业务帧共用严格 parser：重复key、NaN/Infinity、非法UTF-8、递归/深度、错误根/字段类型、过长前缀均有界拒绝。当前 `MAX_DEPTH=32` 仅定义未使用，必须在两端执行，处理 parser 自身 RecursionError；深度边界前不得发生无界分配。
- 先限制 wire bytes，再验MAC与解码/结构；区分 frame上限与单调用64MiB累计输出。非法请求执行计数必须为0。

退出：真实当前OS UDS 正/反身份、完整 transcript 篡改、secret二进制边界、严格JSON和逐帧保护全部执行；unsupported 格不写通过。

### D04 / B04：真正的有界调度、并发与生命周期

位置：`supervisor.py`、新增所需调度/协议模块及真实子进程测试。

- 实现队列256/inflight8/连接32/累计输出64MiB及 manifest/策略更紧上限。semaphore 抢不到就 queue_full 不等于已实现有界排队。
- 实现排队与执行 deadline、queued取消不派发、运行取消合作宽限→安全终止、status/drain 与 heartbeat计数。所有状态有可观察事件和最终回执。
- 明确verifying/staged/starting/ready/active/draining/stopped和failed/quarantined转换；完整handshake+selftest+匹配launch身份且非负一致的heartbeat计数后才能ready，禁止直接赋ready。计数/身份矛盾阻断候选或隔离实例；draining/quarantined拒新任务。
- 当前共享一条 socket 却只对 seq 分配加锁，会有多个调用交错 send/recv、取错回执风险。交付能验证 request/operation/attempt/generation 的多路复用/独立连接调度方案；不能用一个大锁将所有调用串行后声称并发8已实现。
- 退避1/2/4/8/16/30秒、±20%jitter、10分钟第6次崩溃开15分钟 circuit、受控人工恢复。clock/RNG 可注入；至少真实子进程覆盖短时重启，其余精确策略用假时钟。
- launch失败路径回收 accepted socket/listener、stdin/stdout/stderr、受控子孙及进程组，kill后 wait/reap；子进程 leader先退出仍有同组子孙时也要验证回收。只查leader poll不足。
- Host正常shutdown/Provider crash即时清理；Host SIGKILL后的孤儿恢复由 D07持久ownership记录负责。不承诺无独立reaper时父进程退出会自动回收子孙。

退出：CB01-10–13 的真实队列/进程树/并发/取消/超时/退避/circuit叶完成；检查无错配响应、丢任务、僵尸、泄漏或其他进程误杀。

### D05 / B05：重做采样语义与 formal 失败语义

位置：`scripts/measure_engine_capacity.py`、`engine_host/metrics.py`、新增容量测试。

- **30次 selftest 不是30次启动**。按原单各30次冷/热独立启动；至少200只读调用覆盖并发1/4/8和超限；至少20轮 start/invoke/drain/stop，保存逐样本时间与资源。
- 先构建并冻结同一批可回读的签名 fixture 产物和公开测试信任证据；calibration/formal复用匹配产物。当前每次临时重建key/manifest会改变身份，不能略过匹配。私钥不落盘；formal只需要已签制品与受控测试公钥。
- profile 固定 OS/CPU/Python/ABI/Host/SDK/launcher/package/payload/load/context 与阈值、允许余量、统计法和摘要；余量必须在冻结前写入profile，不能在formal代码里临时乘1.5放宽。
- **formal预检在启动和采样前完成**：缺profile、摘要/制品/环境不符、阈值非法、sample不足、profile篡改，非0退出、ok=false，不先跑一遍再发现没profile。
- 超预算/必需指标缺失/采样失败必须非0退出；报告带准确失败原因和原始样本，不能 `failed_budget` 同时 `ok:true/exit0`。empty/0/negative samples拒绝，禁止median/max裸异常。
- 采真实owned进程树RSS/子进程/fd/IPC/queue/inflight/排空；未知写null和reason。fake DB容量not_applicable；统计来源/单位/频率/误差可核对。

退出：原单完整负载、calibration→预冻结→formal，single candidate与raw/profile一致；所有失败路径CLI退出码有自动回归，QA/OPS保持pending。

### D06 / B06：先修 generation/CAS/幂等，再接 Supervisor

位置：`engine_host/store.py:66–145`、`update.py`、新增多连接/多进程测试。

- **generation权威分离**：`active()` 当前 SELECT p.* 返回包自身 generation；回退后 active表gen2、API却gen0。路由generation属于active路由，不属于package版本；同包可在不同单调generation复用。模型/查询/receipt/重启回读使用同一权威值。
- **读改同事务**：expected generation 检查当前在 BEGIN IMMEDIATE 前，UPDATE又没有expected条件。将读取/lease/fence/更新放进同一事务或条件CAS并检查rowcount，独立连接/进程竞争只能一个胜者。
- `add_package INSERT OR REPLACE` 不能把active/last-good/quarantined记录随 prepare 任意覆盖成candidate；包身份不可变，路由生命周期另记。禁止失败prepare留下可被误用的候选状态。
- 同幂等key相同语义回放原operation；不同digest/参数返回idempotency_conflict，不能返回旧op同时写入新包。operation结果固定原engine/digest/generation，不引用“当前active”冒充原操作回执。
- rollback 使用独立持久operation/key和同一CAS；核last-known-good、兼容、吊销、引用，generation单调。禁止假定 package.state字符串就证明安全。
- Task补齐原单版本/SDK/schema/capability/runtime/operation/attempt/fence/deadline/scope/输入摘要/result/effect/引用等持久字段，显式repository接口；FixtureStore仍只在ownership私有目录，不接平台DATABASE_URL。

退出：回退后API/表/重启一致；真实多连接/多进程CAS竞争、重复operation/不同参数冲突、故障事务回滚、引用/吊销/旧结果回读都能跑。单对象RLock测试不能代替DB事务并发测试。

### D07 / B06：Supervisor 集成和八类 kill 点

- update编排必须调用 D01–D04 同一可信Supervisor/RPC：verify/stage/start/handshake/selftest/candidate_ready/CAS/drain/retain/complete，禁止先写candidate/active再补自检。
- 切流前冻结预算匹配与当前资源准入；不符active不变。新任务新generation，原排队/执行/重试/verify/reconcile任务保持原digest与schema。
- 持久记录launch ID、真实 PID/start identity、owner/workdir/socket/package、operation阶段与fence。重启先核实并回收旧owned实例，再启原受信digest的新launch；不重用丢失secret的连接，不凭PID/名字清理。
- 合成prepare/execute/verify真正写独立临时效果账本，operation唯一/可核对；当前直接返回 synthetic 字符串不足。副作用写完但receipt未写时，恢复先verify；不可核对则reconcile_required，不盲重试。
- 原单八类kill点全部用外层test controller真实SIGKILL Host并重启：stage后、握手前、自检后CAS前、CAS后回执前、双版本任务在途、效果后回执前、drain中、rollback CAS后。每点检查准确状态、任务绑定、效果次数、所有权清理和结果持久化。
- queued/running/retry/reconcile四类旧任务分别测；旧引用清零才终止/GC。回退不得改旧任务或共享数据；无安全旧包就blocked。

退出：八类kill叶、四类任务绑定、缺包/吊销/陈旧fence/CAS重放/未知效果各有实际轨迹。只对SQLite行赋state不计完成。

### D08 / B07：双 generation、20轮与统一交付

- runner提供一个入口，构建两份实质不同源码的签名fixture→双进程→认证→自检→只读/合成任务→切流→旧任务排空→回读→回退→kill/recover→清理。
- 至少20轮 `V→V+1→安全回退`，记录每轮generation/operation/四类旧任务/新任务/两digest/效果/资源，不能把20次selftest算更新20轮。
- 双generation容量先calibration再冻结formal，保留峰值/RSS/fd/连接/队列/在途/排空/失败拒绝/泄漏时序，精确引用single profile与两份产物，candidate待签。
- 逐个补全原CB01-01–22，下面是最少交付文件：delivery.md、case-results.json、s2-2-fix-regressions.json、runtime.json、两版源码/产物身份、八kill轨迹、calibration原样本与冻结profile、single/dual candidate、cleanup.json、operations文档。
- 当前新增实现多为untracked；交付清单要同时包含tracked差异和untracked完整文件，记录源码摘要，不能只审git diff就遗漏全部新模块。不为此自动git add/commit。
- 报告先区分代码实现/实际执行/正式准入；缺evidence、未执行、skip、失败不能填passed。根结果取所有必需叶的合取，任一失败或依赖未满足时CLI非0，不能始终打印ok。

退出：全部当前OS必需叶完成且可从保存证据复算；正式test/output instances不写入，C80/C81/T04/T05/T06a-core和生产门禁不自动关闭。

## 3. 工作顺序与交付约束

1. D01 先关闭 B00 的可信证据缺口；D02/D03 修可信运行链。旧实现的运行结果只作缺陷证据，不能被后续容量/热切流当通过前置。
2. D04 完成真实调度/生命周期。D06 的纯持久化修复可与 D02–D04 并行；D07 依赖其全部完成。
3. D05 产单Provider冻结容量；其采样工具可先写，正式run必须用修复后的运行链。
4. D07 完整集成/八kill通过后跑 D08。内部失败修复并回归，保留中间失败轨迹，不停在部分完成说明。
5. 最终一次性交付本批全项状态和证据路径，交给Codex复核。职责保持dsh实现、Codex审核，不自行进入平台升级/真实引擎或现场任务。

解释器继续用 `/Users/zhaolongjie/codex/mesdbtools/.venv-mac-primary/bin/python`。先跑相应定向回归，再跑原单全套已实现测试、真实runner/单双容量/kill测试、v3规划检查器、git diff --check及确认无真实连接的fast检查。未涉及前端，不用反复前端总数替代本批缺失证据。

**本批验收只沿用原单的8项与22组叶标准；测试总数增长、文件新增或一份状态说明均不构成退出条件。**
