# 给 dsh 的连续开发批次 01：补漏、Provider 运行时与单节点更新闭环

日期：2026-10-26。状态：**本机 fixture 批次已实现，最终证据见交付报告**。用户后续授权 Codex 接手核心编码与集成；原任务范围和退出条件保留。工作目录：`/Users/zhaolongjie/codex/mesdbtools`。

当前交付入口：[最终交付](/Users/zhaolongjie/codex/mesdbtools/reports/engine-extension-core-batch-01-20261026/codex-delivery/delivery.md)。这是 B00–B07 的当前 Darwin/arm64 隔离实现，不改变正式 EP 计划状态。原 dsh 分工是任务建立时的安排；本次 DSH Desktop 访问被自动权限审查拒绝，未实际向其发送任务。[简单任务交接稿](/Users/zhaolongjie/codex/mesdbtools/docs/blueprint/engine-extension-packs-2026-10-26/dsh-simple-handoff.md)仅作为待发送文档。

## 直接执行指令

按 B00→B07 连续完成本批，内部问题自行修复、复验，最后一次性提交可审核差异和证据；不用每完成一个小项再问是否继续。B05 的采样工具可在 B06 开发期间并行准备，最终 B07 统一复验。

本批交付的可运行结果是：**修复 S2-2 的真实缺口；用测试根签名的 fake Provider，经过同一 Engine Host/Supervisor、本机认证 IPC 和有界 RPC，在真实子进程中执行；测量单/双 generation 容量；完成单节点候选、切换、旧任务排空、回退与宿主崩溃恢复。**

这是一批完整的隔离开发闭环，不等待 DM/金仓环境、真实生产根或多节点。完成本单后统一交付，不能只交协议文档、mock 状态机或通过数量。

## 1. 已有事实与本批边界

1. S1 已有有效 0.3/v3 计划。先读同目录 `contracts.md`、`development-tasks.md`、`test-cases.md`、`traceability.json`，不要重新设计一套相冲突的 ID、版本轴和依赖。
2. S2-1 已有 migration contract 和 verify 启动门禁。本单保护这些代码及原启动行为。
3. S2-2 已有 `engine_packs/trust.py`、`contract.py` 和 22 条直接测试；Codex 实跑原定向套件 **87 passed**，同时独立复现 **5 项 P1**。详见[独立审核](/Users/zhaolongjie/codex/mesdbtools/reports/engine-extension-s2-20261026/s2-2-codex-review.md)。通过数量不能关闭这些缺口。
4. `T03[test-trust-ready]`、`T14a[runtime-ready/core-ready]`、正式 T05/T13a 等完整依赖尚未据本批签发。本单允许实现和运行显式 `fixture_only` 的隔离开发工具；**不改变原计划调度依赖，不把开发探针升级成正式案例/阶段通过**。
5. 本批不接实际平台任务队列或实际平台数据库。SQLite 是本批独占的 Host 测试账本，用于实现持久化接口及故障测试；以后接平台持久化须由 T05/T05a 接管，不能据此宣称生产任务绑定完成。
6. 原生四引擎 Provider 抽取/接管、DM/金仓驱动、secret broker、完整安装 CLI、安全 archive 解包、TUF 生命周期、多节点、平台 A/B、前端均不在本批。保留后续接口，不写空实现后报完成。

### 本批覆盖与正式依赖

| 原任务 | 本批可完成的实现/证据 | 不能据此关闭的部分 |
| --- | --- | --- |
| T01/T02 | test.fake 所需 SDK/目录最小子集 | 完整模板/conformance、原生身份与全 catalog 兼容 |
| T03 | S2-2 补漏、临时测试签名与字节合同 | archive/离线根生命周期、正式 test-trust-ready、生产根 |
| T04 | 当前 Unix 格的真实进程/IPC/生命周期与容量候选 | secret broker/真实连接池、原生运行时、全 OS/ABI、正式准入 |
| T05 | 隔离账本的任务版本绑定、回执和局部标准结果 decoder | 平台持久化/迁移、审批与业务 CLI、legacy schema 全兼容 |
| T06a-core | fake 单机事务和恢复代码、双 generation 容量候选 | 正式 T04/T05/T13a 与测试根/环境输出齐备后的联合验收 |
| T13a/T14a | 无正式交付；仅提供 lab harness | 安装/管理授权、runtime/core-ready 环境格签发 |

T04 的正式 T01/T02、T03[test-trust-ready]、T14a[runtime-ready] 依赖，以及 T06a-core 的正式 T04/T05/T13a/测试信任依赖均原样保留。B02–B07 用明确的 `dev_harness_only/fixture_only` 入口实现，不能接入主程序或产生正式 install/activate/ready selector。内部状态机可实现 stage/activate 等动作，但它们只改变本批隔离账本。

B00 是后续硬门：五项 P1/P2 未修完且定向回归未通过，不能把旧 verifier 接到后续启动链。B01–B04 的身份/协议硬门失败时，后续依赖测试记 `skipped_by_dependency` 并先修复，禁止改弱认证来凑完整批通过；独立测试和工具准备可继续。

### 允许变更与保护范围

| 范围 | 本批约束 |
| --- | --- |
| `./backend/app/runtime/engine_packs/` | 修复验签/字节合同；可新增纯解析/证据校验模块 |
| `./backend/app/runtime/engine_sdk/`、`engine_host/` | 新增最小可扩展 SDK、Host、Supervisor、RPC、独立持久化接口；import 零启动/零注册 |
| `./backend/tests/`、`scripts/` | 新增离线 conformance、真实子进程集成、故障和容量工具；默认只操作显式隔离目录 |
| `docs/operations/`、本蓝图下交付说明、`reports/engine-extension-core-batch-01-20261026/` | 更新接口文档、测试索引、实现报告和原始证据 |
| 保护范围 | 不改 `./backend/app/migrations/`、`database.verify_schema_ready()`、`TRACEMIND_SCHEMA_ROLE`、平台启动器/main、真实 release 配置、`.env`、前端、业务 API/能力/菜单；不改实际平台表或目标库 |
| S1 机器基线 | 不改 `traceability.json`、`test-case-ownership.json`、`readiness-fixtures.json`、`validate_plan.py` 来迁就实现；不写 candidate 到 test/output instances，不放宽 oracle 或依赖 |
| 外部操作 | 不读生产私钥；不连接真实 DB/SSH/模型 API；不启停现有 8800/3300；不提交、推送、部署；不清理其他会话的文件/进程 |

新工具禁止隐式读取平台连接配置、自动定位真实 DB、联网安装依赖或发现并接管已有服务。不得用整仓 reset/checkout 清除已有修改。

## 2. 批次工作单、依赖与产物

下列 B 编号仅是本批执行单，不替代原 EP-Txx。原分配为 dsh 实现；本轮按用户新授权由 Codex 完成核心、集成和验证，并安排独立子代理复核。

| 单号 | 对应原计划切片 | 依赖 | 必须交付 |
| --- | --- | --- | --- |
| B00 | T03/W1–W2、C73 合同补漏 | 现有 S2-2 | 5 项 P1 + P2 的代码修复、先红后绿回归、真实字节 oracle |
| B01 | T01 最小 SDK、T02 测试目录接口 | B00 | 版本化 Schema、错误模型、不可变 descriptor、fake V/V+1 源码与 conformance |
| B02 | T04 启动与依赖隔离 | B01 | 验签后私有 staging、真实子进程 Supervisor、身份和清理所有权 |
| B03 | T04 认证 IPC/有界协议 | B02 | 本 OS 的真实 peer 校验、双向握手、逐帧保护、固定 RPC 和上限 |
| B04 | T04 生命周期/故障隔离 | B03 | 排队/超时/取消/崩溃/退避/circuit/受控子进程回收 |
| B05 | T04/C80 早期容量候选 | B04 | 单 Provider 采样工具、calibration、冻结 profile、正式测量候选报告 |
| B06 | T05 接口切片、T06a-core/W1–W3 | B04，切换验证消费 B05 profile | 独占 SQLite journal、版本绑定、generation CAS、排空/回退/崩溃对账 |
| B07 | T06a-core/W4、C81、联合回归 | B05、B06 | 双 generation 容量、连续更新、全链故障 runner、批次交付报告 |

开发顺序为：`B00 → B01 → B02 → B03 → B04 → {B05, B06 的非切流部分} → B06 切流 → B07`。B05 未达到冻结预算时，B06 可以完成故障夹具与代码，不得把候选切流写成验收通过。

## B00. 先修 S2-2：信任与实际字节一致

### 实现内容

| 修复 | 必须改成的行为 | 必须新增的回归 |
| --- | --- | --- |
| R01 公钥身份绕过 | 在构造与验签边界，从真实 Ed25519 public key 导出 raw32；重算 fingerprint/Key ID，核对 namespace/channel。跨域复用比较真实 raw fingerprint，不信任对象传入的 raw/key_id | 直接构造 dataclass、相同实际 key 配伪 raw/key_id、跨 test/prod 改标签均失败；factory 和公开构造不能有不同安全语义 |
| R02 混合用途 | trusted policy 和 manifest 均固定单一 `package_kind=tmengine`、`purpose=tmengine.manifest-signing.v1`；拒绝平台/引擎混合 purpose | 仅平台 purpose、混合集合、错误 kind、test→production、production→test、unknown key 全部失败 |
| R03 opaque 未绑定字节 | 每个 opaque 文件的实际 SHA、packaged raw SHA、锁定该文件原始字节的 SHA 严格一致；SBOM 指向实际 inventory 路径并核对字节摘要；明确 version/ABI/source/license/redistribution Schema | inventory 改成新 hash 但 locked/raw 仍旧必须失败；假 SBOM、未知来源/许可/再分发状态失败；不得用任意非空字符串过关 |
| R04 C73 自报即通过 | oracle 输入必须绑定可重新扫描的构建目录、锁文件和实算 inventory；两个自报 digest 相等不能代替字节计算 | `sha256:self`/`made-up`/`not-a-digest` 失败；同 builder 别名、无真实 build、重复 inventory 失败；incident 同 key 换 namespace、受污染 builder、新标签复活 revoked digest 均失败 |
| R05 目录链接漏检 | 扫描 `dirs` 和 `files` 都先 lstat；拒绝目录/文件 symlink、hardlink 及支持平台上的 reparse/junction；不静默略过 | 文件与目录 symlink、dangling link、hardlink；平台不支持项明确标未验证，不能用 skip 算该平台通过 |

补充约束：

- opaque 的文件 hash 与上游发行 archive hash 分字段，不得错误要求 archive 与解包 member 摘要相等。若本轮只支持未改字节的单文件 opaque，明确拒绝重打包/转换路径；后续再扩展可核对的成员映射。
- 文件分类以受控 build-input lock/provenance 为依据，并绑定其摘要；自研 Provider 入口、SDK/脚本等在该 lock 中固定为 self-built，不能通过改 manifest 字符串从 self digest 排除。构建证据由调用方受控上下文提供，不能由包内任意字符串自己授权。此项是受信构建流程约束，不宣称能识别恶意受信发布者伪造来源。
- C73 self variant 分别扫描两份临时目录，比较相同锁定输入、工具链与规范化规则下的实算摘要。单机两个目录只验证 oracle 逻辑，不冒充两台独立 clean builder。
- C73 third variant 复用实际 opaque 字节校验；incident 用真实 raw fingerprint、实际重建摘要、结构化 compromised builder/key/digest 及新 key 背书的合成证据，不能用两个名字不同替代两份密钥材料。
- 新增严格 JSON 解析入口，拒重复 key、非字符串 key、NaN/Infinity、非法类型/编码/深度。`.sig` 精确三字段；size 要求 `type(value) is int`；digest 使用固定完整格式，非法输入稳定报合同错误，不抛裸 AttributeError/TypeError。
- production 命名下的负例/正例仍全部使用临时 `fixture_only` 测试材料；私钥仅在内存，不能写全局 truststore。保留之前冻结的 control files 和全量 inventory，不重引入自引用 hash。

### 完成标准

五项 P1 的最小反例必须先在旧实现上复现，再在修复实现上被稳定拒绝；保存测试名及旧/新结果。不能删断言、改正例让不安全行为继续成立，或只换错误文案。原 release/migration 回归保持通过。

## B01. 最小 SDK、描述符与两版 fake Provider

### 实现内容与文件

- `engine_sdk/contracts.py`：版本轴、消息/错误/任务/结果 Schema、严格类型、兼容协商。支持 S 与 S-1；S-2 明确拒绝；未知 mandatory 拒绝；未知 optional 在持久化/回传时保留；私有扩展用命名空间。
- `engine_host/catalog.py`：只读不可变 descriptor，包含 engine/publisher/package digest/runtime digest、Provider version、SDK/RPC、OS/CPU/Python ABI、capability/task/result schema、generation 与可用性原因。
- `./backend/tests/fixtures/engine_providers/`：fake V 和 V+1 的源码及构建输入。测试时构建、临时签名，不保存私钥、真实驱动或安装到平台。
- `fake` 必须使用保留的测试 ID（如 `test.fake`），且 Host 需显式 `fixture_only=True` 才能登记；绝不能伪装 `mysql/oracle/postgresql/sqlserver/dm/kingbasees` 或加入真实用户 catalog。
- 两版必须有可核对源码行为差异与不同 self payload digest，例如同一只读能力返回不同已声明版本标记和新增 optional 字段；不能只改 version、timestamp 或签名。
- 最小方法：handshake/describe、capabilities、validate_config、selftest、heartbeat、invoke_read、status、cancel、drain。结果和错误使用核心可独立解码的标准 envelope。
- 增加仅测试 profile 可启用的 `prepare/execute/verify` 合成操作，副作用仅写本批独占临时账本，用于 B06 的断点对账；不得开放通用 SQL、shell、任意路径、动态 RPC。
- 每个调用固定 request_id/operation_id/attempt、SDK/task/result schema、package/runtime digest、目标与参数摘要、synthetic scope/policy、deadline/fence。业务输入不能重选 Provider 或方法。

### 完成标准与测试

SDK conformance 在无 DB、无平台主进程时可运行；覆盖 S/S-1 协商、S-2/mandatory 拒绝、optional 往返不丢、标准字段覆盖和私有扩展边界。未知 capability、错误参数、错误 engine/digest 在派发前失败；未执行 selftest 不得 ready。结果可以在 Provider 进程退出且源码目录不可用后由核心 decoder 回读。decoder 和标准 schema 必须由 Host/core 自行版本化持有，不能复制/缓存 Provider 执行代码或从 Provider 目录加载 schema 来满足该条件。

本项只交最小公共接口和 fake profile；四原生迁移、完整 SDK 文档/模板/所有标准能力仍按 T01/T02b 后续独立验收。

## B02. 从已验证目录启动真实 Provider 进程

### 实现内容与文件

- `engine_host/staging.py`：从 B00 校验的临时源目录复制到 Host 独占、按摘要命名的运行目录；复制时拒链接、越界和非声明文件；复制后再次逐字节验签/校验，再启动。
- staging root 仅本次运行身份可写；目录 0700、受控 socket 0600，payload 不可变读。使用安全创建/rename；不得跟随已存在目录或接受请求传入的任意可执行路径。记录验证摘要及 runtime/launcher 摘要。文件权限不被描述成对同 UID/管理员的安全沙箱。
- `engine_host/supervisor.py`：用既定 SDK launcher 和固定入口 ID 启动**真实 OS 子进程**，新 session/process group，最小环境、固定 cwd/依赖目录；包内入口只从已验证 manifest/allowlist 解析。禁止业务请求传入 module/executable/import path。
- 入口 ID 必须由宿主固定映射到 SDK loader，manifest 本身也不能授权任意 executable/module/path；固定 loader 仅加载已验证 payload 的受限 Provider 入口。handshake 须逐项匹配 manifest 的 engine/Provider/package/runtime/driver/OS/ABI/entrypoint/SDK/task/result 身份，不符不 ready。
- 子进程不能依赖平台业务模块的私有 import，不污染宿主 `sys.path/PATH/LD_LIBRARY_PATH/DYLD_*`。使用已有依赖，不联网补包。
- `launch_instance_id`、真实 PID/start identity、Host identity、package/runtime digest、协商线进入 launch record；不能以 PID 一项作为跨重启清理依据。
- 使用当前开发解释器/venv/checkout 的事实写入 `dev_harness_only` 证据，记录真实摘要和依赖清单；这不是已封装不可变部署运行时，不满足原生制品/离线分发验收。
- launch secret 只通过受保护继承 fd/pipe 交付，父子及时关闭无关端；不得进入 argv、普通环境、日志、manifest 或落盘文件。
- 工具必须显式指定新建私有 workdir 并写 ownership marker。只终止本工具创建且身份仍匹配的进程组，只清理该 workdir 内本次所有的对象；不扫描/杀同名 Python 或其他平台进程。

### 完成标准与测试

保存 Host/Provider 不同 PID、运行目录、manifest/runtime 摘要和自检回执。错误 ABI/缺依赖、固定入口篡改、源目录链接、复制后字节篡改、预置恶意目标目录、secret 出现在环境/argv 的负例均失败且 active 不变。Provider 可启动及被安全回收；没有 import 时启动或全局注册。

当前机器支持的 OS/CPU 格必须真实跑；其他格通过接口隔离，准确标 `not_run/unsupported`，不得写跨平台通过。archive 安装器和真实 OS 依赖介质验收不在本项。

## B03. 本机身份认证与有界 RPC

### 实现内容与文件

- `engine_host/ipc.py`：当前 macOS/Linux 走 UDS；TCP 默认无实现/禁用。macOS 使用合同 §6.1 的真实 UID/GID 和 PID/audit peer API，Linux 使用 SO_PEERCRED。只验证路径 owner、UID 或包自报 PID 都不够。Windows 保留命名管道适配接口，未实测不能宣布 Windows 合格。
- 若当前 OS 所需真实 peer API 不可用，给出具体探针和 `peer_identity_unsupported`；其他协议单测可继续，不得弱化认证后把闭环算通过。
- 宿主与 Provider 每次连接使用新鲜独立 nonce，按不同方向 label 完成双向 challenge；transcript 绑定双方真实 peer/Host、launch instance、engine/Provider/package/runtime identity、SDK 线、deadline。
- 本批固定使用现有 `cryptography` 的 HKDF-SHA256 派生方向独立 session keys，使用 HMAC-SHA256 保护 challenge/transcript 和每帧，使用库的验证接口进行常量时间校验；HKDF 本身不能替代完整性认证。MAC 覆盖方向、connection/frame sequence、绑定身份和原始受保护消息，严格单调；重放/乱序/跨连接/跨 generation/宿主重启旧帧拒绝。认证后才能派发，禁止自造密码 primitive。未来换 AEAD 须另交唯一 nonce/方向/序列合同和复用拒绝测试，本批不用临时更换构造。
- `engine_host/protocol.py`：长度前缀 UTF-8 JSON、严格 parser、固定方法 allowlist、解码前后字节/深度/数组/字段类型上限。无 pickle/eval/业务动态 import。先限制声明长度再分配；截断或非法帧不部分执行。
- 所有 RPC response 绑定 request/operation/attempt、generation/launch/digest 和序列；晚回执只能写其原始任务，不能覆盖新 generation 的结果。
- 内部管理调用必须验证本 Host 的 launch/operation context，不能开放无鉴权 management UDS/HTTP。测试驱动通过受控 Host API 调用，不能直接写 active 指针。

### 本批冻结的初始默认值

| 项 | 默认值 | 必须测试的处理 |
| --- | --- | --- |
| frame | 解码后 1 MiB | 超限在分配/派发前拒绝，稳定 protocol error |
| 单调用累计输出 | 64 MiB | 包括流式分块总量；超限返回 `output_limit_exceeded`，不把 partial 报完整 |
| 每 generation 宿主队列 | 256 | `queue_full`，不无限堆积 |
| 每 generation IPC 连接 | 32 | 拒绝超额并记录不含 secret 的 peer 证据 |
| 并发调用 | 8，且不高于 manifest/策略 | 第 9 个只能在有界队列或被拒绝 |
| 连接/握手超时 | 5s / 10s | 候选失败，不能提前 ready |
| 未指定只读 deadline | 60s | 有显式 deadline 则使用更紧值，不扩大权限 |

单测允许注入更低上限与假时钟来测边界，同时检查生产默认常量；不能为了快把运行默认放宽/删除。fake 没有驱动或真实 DB 连接池，不能据此声称 `max_target_connections=8` 的真实 DB 限制已验。

### 完成标准与测试

真实 UDS 下正确 peer 正常往返；同 UID 错 PID、错误 launch/digest、nonce 复用、方向反射、帧重放/乱序、旧连接/旧 generation、重复 JSON key、非法 UTF-8、巨长前缀、深层 JSON、未知方法、参数类型混淆和输出超限均稳定拒绝。要观察 Provider 执行计数，证明非法请求未被执行；只验 response 字符串不足。

## B04. 排队、取消、崩溃与回收

### 实现内容

- 明确 Host/Provider 生命周期状态，至少区分 verifying/staged/starting/ready/active/draining/stopped/quarantined/failed；转换由已完成步骤和 journal 决定，不能直接赋 ready。
- 所有队列和 inflight 有界；deadline 在排队、派发和执行期间均检查。先 cooperative cancel，宽限期后终止本工具拥有的进程组。排队中取消证明没有进入 Provider。
- readonly 可安全失败/在合同允许时重试；合成写入断线、崩溃或终止后，若不能确认效果，必须 `unknown/reconcile_required`。不能把进程退出解释成副作用已撤回。
- heartbeat 关联 launch identity，检查非负且一致的 queue/inflight/connection 计数；不一致/缺失按合同阻断候选或隔离，不能用 Host 猜值替代 Provider 遥测。
- Supervisor 重启策略按合同：1/2/4/8/16/30s 后封顶30s，±20% jitter；10分钟第6次崩溃开15分钟 circuit。时钟/RNG 可注入以离线精确验证，真实子进程至少覆盖短时退避/受控恢复。
- Provider 派生的测试子进程在同一可回收进程组；超时/崩溃/宿主恢复清理 socket、句柄、临时 artifact，记录确切所有权。PID 已复用或 owner 不符时不得误杀，记录待人工处理原因。
- shutdown/drain 拒绝新任务，等待本 generation 的已绑定任务完成或准确转待对账，不因 active 改变就强杀旧任务。
- B04 仅负责 Provider 崩溃、超时及 Host 正常 shutdown 的即时清理。Host 被 SIGKILL 后的孤儿识别/清理明确由 B06 的持久 launch ownership + restart recovery 负责；进程组不会自动随父进程消亡，不能声称尚无持久记录时已证明这条。Host 不重启期间不承诺即时回收；后续若需此保证，另实现并验独立 reaper。

### 完成标准与测试

覆盖队列饱和、inflight 饱和、排队超时/取消、执行超时、拒绝合作取消、子进程挂起、Provider 异常退出、宿主正常退出、circuit 打开与人工受控恢复；宿主异常退出后的恢复在 B06/B07 完成。每项检查任务状态、实际执行次数、退出码、存活进程/句柄/socket；不能只有 mock 调用断言。secret marker 不出现在日志/argv/env/结果中。

## B05. 单 Provider 容量 v0 候选：采样与预算先行

### 实现内容与文件

- 新增 `./scripts/measure_engine_capacity.py`，只接受明确的 `--fixture-profile` 和私有 `--workdir`；复用正式 Supervisor/RPC，不另外写更轻的性能专用执行路径。
- `engine_host/metrics.py`：按真实 owned PID/process tree 采集启动/selftest/退出/排空耗时、RSS、子进程数、句柄/fd、queue/inflight、IPC 连接、调用延迟/失败/取消。采用 monotonic clock 测间隔，UTC 记事件；记录采样来源、单位、频率和缺失原因。
- Provider 自报 DB 会话为 0 只表示 fake 无驱动；真实 DB 连接容量字段写 `not_applicable/no_driver`。无法实测的 OS 计数写 `not_measured`，不能填0当测过。
- 冷启动定义为新私有 staging + 新进程；热启动为复用已验证不可变 payload、仍启动新进程。明确两者都不代表清空 OS 页缓存；不执行系统缓存清除。
- 分 calibration 和 formal-run 两个阶段；calibration 后写 profile/阈值并锁 digest，formal-run 只消费该文件。测完才调整阈值的 run 作废，重新冻结再测。
- profile 绑定 OS/CPU/Python/ABI、driver=`none/fake`、Host/SDK/launcher/package 源码与 payload 摘要、负载、队列/并发上限、阈值、统计法和运行 context；不同 profile 不能互相代签。

### 最小测量负载

- 冷/热启动各至少30个独立样本；只读调用至少200次，包含并发1/4/8与显式超限压力；报告 median/p95/max、错误和拒绝数量，说明统计方法。
- 至少20轮 start→invoke→drain→stop，记录前后 owned 进程、fd、socket、RSS 时序及残留。RSS 趋势阈值在 calibration 冻结，不把 GC 波动直接写成泄漏或无泄漏。
- 宿主保持运行时验证单 Provider 超时/崩溃不导致另一独立 fake worker 的在途只读请求中断；不要启动任何真实引擎。
- 预算至少含单进程/子树 RSS、进程/fd/IPC/queue 峰值、启动/selftest/排空上限和采样误差；不从合同中的保护上限推算实测容量。

### 完成标准

输出 `single-provider-capacity-v0.candidate.json`、calibration 原始样本、冻结 profile、formal-run 样本及 digest 校验。报告由真实采样计算，不允许手填“通过”或用单测耗时代替子进程指标。

候选报告保留 `qa_ops_acceptance=pending`，不伪造 QA/OPS 签字；不自动登记正式 `output_instances` 或签发 C80/`single-provider-capacity-v0`。报告 Schema 需能被 B06/B07 精确匹配消费，并为后续正式签发保留核验接口。

## B06. 单节点 generation 热事务与持久恢复

### 实现内容与文件

- `engine_host/store.py`：明确持久化 repository 接口；本批仅启用 `FixtureSqliteStore`。数据库位于 ownership marker 所属 workdir，使用独立建表入口，禁止复用平台 DATABASE_URL。
- `engine_host/update.py`：实现 `verify → stage → start_candidate → handshake/selftest → candidate_ready → activate_generation → drain_old → retain_rollback → complete`。包状态、更新 operation 状态、任务状态分表/分模型；事务转换写 journal，不能只放进程内 dict。
- SQLite 最小保存：package/digest/兼容身份；active generation 与 last-known-good；update operation/idempotency key/expected generation/fence/阶段；task 固定身份/输入摘要/deadline/授权快照/attempt/effect；result/receipt/schema；引用与待对账；launch ownership/recovery 状态。
- 本机 lease + transaction CAS 串行认领更新；同 operation/request 并发幂等；不同更新竞争只有一项获胜。陈旧 expected generation/fence 不可切流或写新结果。SQLite 并发须用真实独立连接/进程测，不能只测同对象重入。
- `prepare` 时即固定 package/runtime digest、SDK/task/result/capability 版本和 generation；旧的排队、执行、重试、恢复、verify/reconcile 均保留原绑定。切流后新任务走新版本，旧任务只能被原精确 digest 的受信实例处理。
- V/V+1 共存使用 B01 的实际不同源码包，走 B02–B04 的同一 Supervisor/IPC/RPC；不能在状态机里 mock ready，也不能给两个进程加载同一模块后只改返回版本。
- 切流前核对冻结容量 profile/实际资源预算；profile 与制品/OS/SDK/负载不匹配、采样未完成或候选未 ready 时 active 保持不变。本项仅消费本批 fixture 候选，接口不能让其伪装正式生产 gate。
- 正常 drain 期间旧任务和对账引用未清零，保留旧包及所需实例；引用清零才退出。适用的停用/GC guard 拒删 active、last-known-good、待恢复/对账或 retained artifact 引用；不实现面向用户的卸载 CLI。
- 回退是一次新的单调 generation CAS，路由到兼容、未吊销的 last-known-good digest；不回拨 generation 数字，不重写旧任务绑定，不执行 down migration/restore。无安全旧版本则准确 unavailable/blocked，不能静默回主程序实现。
- 隔离持久化吊销/quarantine 状态；重启不能凭旧 active 指针重新启用已吊销包。已有合成副作用保留证据后进入可核对状态，不靠删表/重跑解决。
- Host 启动恢复时先读持久 launch record，核对 ownership/PID start identity 后回收原实例/socket，再按 journal 恢复相同受信 digest 的新 launch；不能重用已丢失 secret 的旧会话。无法核实身份则标 blocked，不按进程名称/PID 猜测清理；不在旧实例尚有不确定效果时重放 execute。

### 必须完成的故障恢复点

| 注入点 | 重启后必须出现的结果 |
| --- | --- |
| stage 完成但无启动记录 | 核验摘要与 journal 后继续或安全清理本工具孤儿；不改 active |
| 候选进程启动、handshake 未成功 | 不 ready、不切流；仅回收身份匹配的候选 |
| selftest 后、CAS 前 | active 仍旧；可重试同 operation，不重复启动/重复切换 |
| CAS 已持久化、返回前 Host 被 SIGKILL | 回读确认已激活，不再次执行 CAS；新任务只路由已确认 generation |
| 新版已接流、旧版仍有任务 | 恢复原绑定和引用；必要时重启同 digest 的新 launch，不给 active 新版代跑旧任务 |
| 合成副作用已写、Host receipt 未写 | 先用原 operation_id/摘要/合成账本 verify；确认后补回执；不盲重试 execute |
| drain 中途/旧进程退出前 | 结果可回读，未决引用准确；不误删仍需恢复的包 |
| rollback CAS 后返回前 | 同 operation 回读，generation 单调，不能反复切回/切前 |

测试 fake 可在临时独立账本记录 operation 的可核对效果；该实现只证明指定幂等/verify 合同。无法核对时必须保留 `reconcile_required`，不承诺任意外部系统“恰好一次”。

### 完成标准

真实 OS Host/Provider 进程在上述 kill 点恢复；同请求并发只发生一次合成效果；旧任务回执不被新 generation 覆盖；断开客户端后持久状态仍可恢复；移除 Provider executable/source 并阻断对其目录的 import/open 后，标准历史结果仍由 Host/core 持有的 decoder/schema 阅读。不同 OS kill/ownership 语义分别记录，不拿 mock SIGKILL 当真实进程故障。

## B07. 双 generation 容量、连续更新和统一交付

### 实现内容

- 复用 B05 工具/profile Schema，采集 V/V+1 同时存在的 process/RSS/fd/IPC/queue/inflight 峰值、旧任务排空、切换期间新任务延迟及失败/拒绝。
- 至少20轮 V→V+1→安全回退的完整隔离循环；每轮保留旧任务、新任务、generation、digest 和最终结果对照。中间穿插 B06 的故障点，分别记录注入故障与无注入连续性，不能把预期拒绝混成成功吞吐。
- 输出 `dual-generation-capacity-v0.candidate.json`，引用精确单 Provider profile、两版实际 payload/源码差异、冻结负载和阈值。报告仍待 QA/OPS 接受，不自动签 C81 或 T06a-core。
- 新增 `./scripts/check_fake_provider_runtime.py`：一个离线入口覆盖构建/临时签名/验签/启动/认证/selftest/调用/切流/排空/回读/回退/恢复/清理。必须实际启动子进程并走真实 UDS；禁止“happy-path 全 mock”。
- runner 使用显式新 workdir；没有平台环境变量也可运行；不占用现有 TCP 端口；不创建/修改真实资产和任务。正常结束/失败退出时执行所有权安全清理；原始证据保留到报告目录，秘密不得保存。

### 完成标准

批次测试矩阵中的所有当前 OS 必需项真实执行，0 未解释失败；必需项 skip 视为未完成。其他 OS 的未实测列明，不能阻断当前 OS 其余独立单元开发，也不能把当前格写成全平台交付。

持续更新中，原定不受故障注入影响的已接收任务不得丢失/错绑/重复副作用；注入故障的任务须有准确失败或待对账结果。最后 owned 进程、socket、fd/临时对象回到冻结允许范围，实际账本和结果可回读。

## 3. 统一测试矩阵

这些 `CB01-*` 是开发回归 ID，仅追踪本批；`EP-Cxx` 是关联的正式计划目标，不能因为本矩阵通过就自动记正式 EP-Cxx passed。

| 本批测试 | 关键输入/动作 | 可观察预期 | 对应目标 |
| --- | --- | --- | --- |
| CB01-01 | 真 key + 伪 raw/Key ID 直接构造 | 信任边界稳定拒绝，跨域不能同材料双验过 | T03/C73 前置 |
| CB01-02 | mixed purpose、wrong kind、双向跨域、非法签名 envelope | 正确错误码；无平台信任继承 | T03 |
| CB01-03 | opaque 改实际字节并只改 inventory | locked/raw/SBOM 不符拒绝，不能重新归类逃避 self digest | C73 third |
| CB01-04 | 两构建目录、假 digest、同 builder 换名、incident 同 key 换域 | oracle 重算字节；负例拒绝，正例不冒充独立机器 | C73 self/incident |
| CB01-05 | 目录链接/硬链接/越界、非字符串 JSON key/重复键/bool size | 不忽略、不越界、不裸异常 | T03/C16；JSON primitive 供 C62 |
| CB01-06 | fake V/V+1 conformance、S/S-1/S-2、mandatory/optional | 兼容线明确；两版不同源码；标准结果可独立解码 | T01/C05 |
| CB01-07 | stage 字节篡改、错 ABI/入口/依赖、路径替换 | 未验证包不启动/不 ready，原 active 不变 | C05/C18 局部 |
| CB01-08 | 真实 peer 与同 UID 错 PID、旧 launch/nonce/反射 | 双向身份拒绝；Provider 非法执行计数为0 | C60 |
| CB01-09 | 截断/巨长/非法 UTF-8/深度/重复键/重放/错序 | 有界安全失败、不部分执行、不越界分配 | C62 |
| CB01-10 | frame/累计输出/连接/队列/并发超限 | 稳定拒绝/部分状态准确；上限真实生效 | C14 局部 |
| CB01-11 | 排队取消、执行超时、不合作、派生子进程挂起 | 无越界执行；回收正确进程组；未知效果不当成功 | C13/C63 |
| CB01-12 | fake clock 连续崩溃 + 真实短时重启 | backoff/jitter/circuit 准确，不重启风暴 | C63 |
| CB01-13 | Host/Provider argv/env/log/result 扫描 secret marker | 无泄漏；仅证明 launch secret，不冒充完整 DB broker | C10 局部 |
| CB01-14 | 单 Provider calibration→冻结→formal-run | 真实进程采样、预算/digest匹配，candidate 待签 | C80 候选 |
| CB01-15 | 同更新并发、expected generation/fence 陈旧 | 一个 CAS 胜者、同 operation 幂等，旧响应不越权写新状态 | C21 |
| CB01-16 | 切换前已排队/执行/重试/待对账，切换后新任务 | 旧任务原 digest，新任务新 digest，引用不丢 | C21/C23 |
| CB01-17 | B06 全 kill 点的真实 Host SIGKILL/重启 | 持久 journal 恢复；副作用先 verify，未知保留对账 | C23/C63 |
| CB01-18 | candidate 不健康、吊销、无安全旧包、容量摘要不符 | 不提前切流；合法回退新 generation；无安全版本则阻断 | C23 |
| CB01-19 | 引用存在时清理、移除 Provider 后读历史结果 | 禁删仍需包；核心独立解码标准结果 | T05 接口切片 |
| CB01-20 | 双 generation 负载与20轮完整循环 | 峰值/排空/泄漏证据，candidate 精确绑定两版 | C81 候选 |
| CB01-21 | 无平台配置运行 runner，全部 teardown | 无 DB/SSH/模型 API、真实端口/平台配置操作，所有权清理正确 | 本批隔离 |
| CB01-22 | S2-1/release/计划检查器回归 | verify 仍无 DDL，旧发布行为保持，正式门禁未伪签 | 回归保护 |

建议测试文件按职责拆分：`test_engine_pack_trust.py`、`test_engine_pack_contract.py`、`test_engine_sdk_contract.py`、`test_engine_host_staging.py`、`test_engine_host_ipc.py`、`test_engine_host_supervisor.py`、`test_engine_host_update.py`、`test_engine_host_capacity.py`。参数化测试需输出每个 variant 的 node ID，不用一条聚合断言掩盖未执行分支。

## 4. 验证命令与证据

使用现有环境 `./.venv-mac-primary/bin/python`；系统 python3/python3.12 曾缺依赖，不安装/升级全局环境。下列为原任务约定接口；实际已实现模块和完整调用命令以[运行说明](/Users/zhaolongjie/codex/mesdbtools/docs/operations/engine-host-runtime.md)为准，staging 已并入 Supervisor，不另建同名空模块。

```bash
PYTHONPATH=backend ./.venv-mac-primary/bin/python -m pytest -q ./backend/tests/test_engine_pack_trust.py ./backend/tests/test_engine_pack_contract.py ./backend/tests/test_engine_sdk_contract.py ./backend/tests/test_engine_host_staging.py ./backend/tests/test_engine_host_ipc.py ./backend/tests/test_engine_host_supervisor.py ./backend/tests/test_engine_host_update.py ./backend/tests/test_engine_host_capacity.py ./backend/tests/test_release_manager.py ./backend/tests/test_release_config_merge.py ./backend/tests/test_migration_contract.py

PYTHONPATH=backend ./.venv-mac-primary/bin/python ./scripts/check_fake_provider_runtime.py --fixture-profile local-fake --workdir /ABS/NEW/PRIVATE/WORKDIR --report /ABS/REPORT/runtime.json

PYTHONPATH=backend ./.venv-mac-primary/bin/python ./scripts/measure_engine_capacity.py --fixture-profile local-fake --phase calibration --workdir /ABS/NEW/PRIVATE/CALIBRATION --report /ABS/REPORT/calibration.json

PYTHONPATH=backend ./.venv-mac-primary/bin/python ./scripts/measure_engine_capacity.py --fixture-profile local-fake --phase formal --profile /ABS/FROZEN/profile.json --workdir /ABS/NEW/PRIVATE/FORMAL --report /ABS/REPORT/capacity.json

./.venv-mac-primary/bin/python docs/blueprint/engine-extension-packs-2026-10-26/validate_plan.py --self-test --report reports/engine-extension-core-batch-01-20261026/plan-validation.json

git diff --check
```

`/ABS/...` 由 dsh 替换为本次新建绝对目录，不指向项目/知识库/用户已有目录。可提供安全 `mkdtemp` wrapper 生成路径；必须保留 ownership marker 和绝对路径检查。正式测量 profile 必须来自已冻结 calibration，runner 不得自动补签预算。

先检查 `scripts/check_all.py --fast` 的实际步骤，在确认不会访问真实库/服务后运行；禁止附加 live/API/诊断 smoke。新加代码后运行相关定向回归，失败修复后再复验；没有前端改动，不用反复跑前端总数替代本批 runtime 测试。

### 必须交付的文件

统一输出目录：`/Users/zhaolongjie/codex/mesdbtools/reports/engine-extension-core-batch-01-20261026/`。

1. `delivery.md`：B00–B07 完成矩阵、接口/文件落点、关键取舍、实际命令/解释器/退出码、失败/skip/未跑、边界和后续缺口。
2. `case-results.json`：CB01 每叶 node ID、参数/OS/context/source/payload/profile digest、实际 executed/pass/fail/not_run、证据路径。父项/预期结果不能当执行叶。
3. `s2-2-fix-regressions.json`：R01–R05 及 P2 的旧实现反例与修复后拒绝结果，不只写文字“已修”。
4. `runtime.json`、脱敏事件日志、两版源码与 payload 摘要、真实 Host/Provider PID/peer 证明、generation/operation/任务对照和每个 kill 点恢复轨迹。
5. calibration 原始样本、冻结 profile、single/dual candidate 报告及摘要；统计从原始样本可重算。没有 QA/OPS 角色签署则保持 pending。
6. `cleanup.json`：owned 进程/句柄/socket/临时对象的前后计数、未回收原因；不得包含 secret/私钥/真实连接配置。
7. 更新 `docs/operations/engine-pack-contract.md`，新增 `engine-host-runtime.md`：公开接口、错误码、状态机、离线 runner 用法、支持/未测 OS、容量口径及生产集成缺口。

输出 JSON 必须严格可解析，证据文件真实存在；不能先生成全 passed 模板再不执行。中间运行失败保留原因，最终成功不能覆盖失败历史而使其无法审查。

## 5. 整批退出条件与后续交接

- R01–R05 与 P2 已有可复现回归并修复；原有87定向回归没有被放宽。
- 当前 OS 的**真实签名包→真实子进程→真实 peer/认证 RPC→任务→结果**闭环完成；没有 mock 替代关键路径。
- 单节点 update/cancel/drain/rollback/kill/recover 在独立持久账本可重复运行，旧任务精确绑定、结果可独立回读，无静默成功或盲目重试。
- 单/双 generation 容量都有真实原始样本、冻结预算、精确 profile/digest 和未签状态，不能互相替代或当真实驱动容量。
- 定向回归、v3 检查器、差异检查和经确认安全的 fast 检查通过；现有平台服务、S2-1、前端及真实数据未改。
- `E07=waiting_external`；不写正式 test/output instances，不签 `test-trust-ready`、`runtime-ready/core-ready`、C80/C81、完整 T04/T05/T06a-core 或生产门禁。签发留独立证据复核与实际职责人。

遇到内部实现/测试失败继续修复。遇到只能由外部提供的 OS API/权限/真实环境条件，记录具体失败、已完成部分、受影响叶及解除条件，其他独立工作继续；不能将“未跑”写为完成，也不能为赶进度弱化安全合同。

本批最后一次性回报，交由 Codex 审核。后续大批次另行分配平台持久化集成/T05a迁移、安装管理、角色拆分、平台 A/B、原生接管和真实国产驱动；不自行进入现有开发环境升级或现场操作。
