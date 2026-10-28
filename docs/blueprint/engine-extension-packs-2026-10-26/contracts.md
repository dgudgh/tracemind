# Engine SDK、独立 Provider 包与热更新合同 0.3

日期：2026-10-26。状态：**0.3 文档基线已同步，业务实现与全部业务案例仍为 planned**。本文定义拟开发合同，不是当前可调用命令、API、可安装制品或已验证能力；文中默认值只是开发初始上限，不是实测容量、SLA 或客户承诺。本轮 S1 只同步计划文档，未运行客户环境、数据库、SSH、部署、迁移、故障注入或性能测试。

规范词：**必须/禁止**表示实现与验收的强制条件；**应**表示若偏离须在决策记录中说明原因和替代控制；**可以**表示兼容范围内的实现选择。

## 1. 责任、任务与不可转移边界

角色缩写：ARCH（架构）、SDK（SDK 与协议）、ENG（引擎 Provider）、CORE（平台核心）、REL（发布与供应链安全）、SEC（独立生产安全批准）、QA（测试）、OPS（运维）、UI（前端）。每项可有协作者，但下表 owner 对最终合同和验收证据负责。SEC 必须独立于 REL 签名操作人、OPS 生产保管人和 build operator；同一人不得独自完成生产根保管、批准和恢复。

| 合同域 | 任务/owner | 强制边界 |
| --- | --- | --- |
| 平台数据库身份迁移与双版本兼容桥 | **T05a / CORE** | T05a 独占平台元数据库 DDL、backfill、兼容写和回退数据合同；Provider、安装 CLI、T06a-core/T06a-real/T06b 不得顺带改平台表 |
| 四原生源码分离与行为对照 | **T02b / ENG + CORE，M0** | 只证明源码边界和行为基线，不得宣称四原生已经成为完整独立 Provider 制品 |
| 四原生制品接管和独立升级 | **T02c / ENG + REL，M1/M2** | M1 候选包及自检；M2 才要求 Oracle、MySQL、PostgreSQL、SQL Server 被产品首装、接管并可独立升级 |
| 单节点 Engine Host 热事务 core | **T06a-core / CORE，M2-core** | 以测试根签名 fake V/V+1 走正式 Supervisor/RPC，验 L1/L2 generation、任务固定、故障恢复和双 generation 容量；不等待 DM，也不宣称真实驱动已认证 |
| DM 真实 Provider 连续性 | **T06a-real / ENG，M2-real** | 消费 T11 的真实 DM V 与源码实际不同的 V+1，验 L3 连接、切流和未知效果对账；缺 DM 只阻断此线 |
| 多节点 Provider 发布与 generation 收敛 | **T06b / CORE + OPS** | 明确依赖 **T07**；T06a-core 单机通过或 T06a-real 的 DM L3 通过都不代表 T06b 完成 |
| 启动器、migrator 与平台 A/B 预算 | **T08 / CORE + OPS** | 硬依赖 T05a 的 `migrator-contract`；managed-hot 下仅专用 migrator 可执行 DDL，API/Worker 只校验；输出平台 A/B 容量 v0 |
| 初装最小管理工具 | **T13a / REL + OPS** | 不依赖热更新；只覆盖初装/激活、未对外初激失败恢复，rollback 仅限离线 lab 或尚未接流实例 |
| 热管理 CLI | **T13b / REL + OPS，M2-core** | 依赖 T06a-core 的受控内部接口；T06a-core 不反向依赖 T13b |
| SDK/RPC、Schema 与兼容矩阵 | SDK / ARCH + SDK | 当前 SDK 线 S 与 S-1 的机器可测规范由 SDK owner 发布 |
| 包格式、签名、信任库和构建 | REL / REL | 发布密钥、信任 generation、制品来源和管理员操作审计不可下放给 Provider |
| 生产信任根交接 | **T03 / REL，OPS 保管，SEC 独立批准** | `test-trust-ready` 与 E07 `production-trust-accepted` 分开；生产私钥禁止进入 DEV/CI/build 环境 |
| 结果解码与历史可读性 | CORE / CORE | 标准结果由核心 versioned decoder 解码，禁止运行已卸载 Provider 代码来读历史结果 |
| 验收夹具、故障注入和容量冻结 | QA / QA | 覆盖成功、拒绝、崩溃、回退、重放和资源上限；开发默认值不是容量结论 |
| 目录、状态、兼容评估和进度展示 | UI / UI | 只展示 Registry/核心事实，不以 UI 状态替代运行证据 |

若任务表对 owner 或依赖的描述与本合同冲突，须先由 ARCH 形成显式决策并同时修订两处；不得靠实现者口头转移 T05a、T02c、T06a-core、T06a-real、T06b、T08 或生产信任职责。

## 2. 引擎身份、原生 Provider 与目录

- `engine_id` 是稳定产品标识：预装 `oracle`、`mysql`、`postgresql`、`sqlserver`；首批扩展为 `dm`、`kingbasees`。`family/protocol` 只供复用连接或展示组件，不能替代 `engine_id`，也不授予运维能力。
- 四个原生引擎必须抽取为与第三方扩展相同的独立、签名、不可变 Provider 包，由产品安装介质默认预装；其 publisher 与 engine namespace 受保护。不得保留一套主程序内置 Adapter 作为隐式优先路径或长期双实现。
- T02b/M0 只完成源码分离与行为对照；T02c/M1 只证明四个候选包可构建、验签、启动和无客户凭据自检。到 T02c/M2 完成产品介质默认预装、真实入口接管、首装可用和独立升级后，才能宣称四原生 Provider 包交付。C01 的主验收归 T02c/M2；T13a 仍负责 C04/C20/C69/C75 的安装与失败恢复负例，不能用负例工具验收替代产品可用性。
- 原生包和扩展包使用同一 Registry、SDK/RPC、Supervisor、兼容检查、激活、任务绑定、审计、回退和卸载规则。原生仅表示随产品交付和受产品支持，不表示绕过签名或生命周期检查。
- 每个受支持的原生引擎在首次安装事务结束前必须至少有一个签名、兼容且通过本机自检的可用 Provider generation。任一必需原生包安装/启动失败时，该引擎须准确报告 `unavailable`，整体验收不得通过。
- **不另做主程序“只读降级”实现。**因此首装包失败不会由主程序内置驱动兜底；这是消除双实现漂移的刻意取舍，不得用空数据、静态页面或历史缓存伪装可用。
- Registry 以 `catalog_generation` 发布不可变一致快照，合并预装和已验证扩展。未安装、禁用、隔离或不兼容的引擎返回机器可读原因。
- `last_known_good` 不是一个全局“神奇包”，而是按 `(engine_id, platform P/P-1, SDK S/S-1, OS/CPU/ABI, trust_generation)` 认证格得出的可回退 digest 保留集合；不同认证格可有不同集合，也可能没有安全候选。处于 `active`、属于相应格最后可用集合、被排队/执行/恢复/对账任务精确引用，或仍被留存策略引用的 package digest 均禁止卸载和 GC。

## 3. T05a 独占的平台身份迁移与启动接线合同

T05a 采用 expand → bridge/backfill → read-switch → 后续独立 contract 的迁移序列。热更新双版本窗口内禁止删除、改义或收窄真实、未经修改的平台 P-1 制品仍使用的列、约束、枚举和索引；兼容不能依赖给 P-1 打补丁。

T14a 先盘点 P-1 原始制品、Schema/脱敏快照与资格证据。没有合格旧版时触发 T05b/CORE 条件开发工单：只冻结既有行为/additive 兼容合同、交付并认证桥接 B，不做未来 SDK/DM/Router 或平台 DDL；T05a 责任人审签合同。T14a 核验 B 后登记输入，不构建或修改 B。T05a 完整实现/验收在该分支硬依赖 T05b；已有合格旧版则记录不触发依据。B 的首次落地是维护式安装，不能宣称热更新；冻结后测试候选时不得修补旧 B。

1. 新增显式 `engine_id`，保留旧 API/存储语义中的 `db_type`。P-1 继续只认识并写旧字段；P 必须 fallback 读取旧形态，并在兼容窗口继续写 P-1 可理解的旧形态。migrator/双写桥负责新增字段，不得要求 P-1 写未知列。
2. 旧值仅按四个已知产品的规范映射处理：Oracle → `oracle`、MySQL → `mysql`、PostgreSQL → `postgresql`、SQL Server → `sqlserver`。允许的历史大小写/空白/既有精确别名必须由 T05a 固化成可审计映射表和夹具，不能做模糊匹配。
3. `DomesticDB`、其他未知值、歧义值和空值不得猜成任一产品，也不得统一猜成 `DomesticDB`。它们保留原值，`engine_id` 为未解析状态，实例能力执行被阻断，等待人工识别及证据。
4. backfill 必须幂等、可断点续跑，记录迁移版本、输入旧值、映射规则版本、结果和冲突；重复运行不得改写已有人工作证据的身份。
5. 凡可能被 P-1 的旧行更新覆盖或清空的新语义，必须存入 P-1 不会整行覆盖的附表/sidecar，或以明确版本列、字段级 CAS/merge 保护。若无法证明 P→P-1→P 往返无损，该平台版本组合必须标记 `hot_rollback_eligible=false`。
6. 无可靠旧任务版本证据的待执行、重试、恢复和补偿任务必须冻结，重新 `prepare` 并重新走适用授权/审批，禁止按当前 active Provider 猜测后继续执行。
7. 历史结果保持原始 task/result schema 身份。核心保留 legacy schema decoder；缺少的新版本字段显示 `unknown/not_recorded` 或明确缺失原因，禁止伪填默认值后声称是新 Schema 结果。
8. T05a 必须输出版本化 `migrator-contract`，至少含 migration revision/checksum、前置/后置校验、`migration_required`、可续跑条件、部分完成对账条件及 P/P-1 兼容范围；T08 对此输出建立硬依赖并负责真实启动接线。
9. managed-hot 模式下，启动器须在候选 API 前调用专用 migrator role；API/Worker 只能核对 Schema revision/checksum 与 migration receipt，不得执行 DDL。初装 bootstrap 也只允许单一 migrator；显式维护入口另行授权。
10. 多启动器可以竞争，但数据库级锁只能允许一个有效执行者。该约束不等于承诺 MySQL 非事务 DDL 在崩溃后“恰好执行一次”：kill 后须先核对实际结构与校验，已完成幂等步骤跳过，不确定的部分 DDL 进入 `reconcile_required`，禁止盲重放。
11. 回退只切代码与路由到兼容的 P-1；hot 模式禁止注册或调用 down migration、自动共享库 restore 或逆向改写共享数据。维护式灾难恢复保留独立授权入口；真正删除 legacy `db_type` 或旧结构只能在 P-1 退出支持、备份与恢复演练通过后的独立 contract 阶段进行。

Provider 包不得携带或触发上述平台 DDL/backfill。T05a 之外的任务若需要平台 Schema 改动，必须停止并回到 T05a/ARCH 评审。

## 4. SDK 0.3 开发者接口

| 接口 | 必须输出/保证 |
| --- | --- |
| `handshake / describe` | engine、package/runtime digest、SDK/RPC 线、驱动、OS/ABI、task/result schema、launch instance；与签名清单逐项相符 |
| `validate_config` | 按连接 Schema 检查必需字段，返回字段级错误，不建立目标连接 |
| `test_connection / identify` | 产品、版本、edition、兼容模式、稳定实例/集群身份及带时间证据；不能以端口或登录成功替代产品识别 |
| `capabilities` | 支持状态、运行可用性、所需权限、读写级别、参数/结果 Schema、取消/幂等语义和原因 |
| `discover / collect / inspect` | 标准节点/关系/指标/巡检结果，保留原生身份、方向证据、缺失原因及命名空间 |
| `invoke_read` | 仅允许宿主登记的 `capability_id` 和 Schema；受行、字节、并发、deadline 限制 |
| `prepare` | 无业务副作用；冻结目标、参数、Provider/platform 身份、授权依据、执行及验证条件 |
| `execute / verify` | 按宿主 `operation_id` 执行并核验，区分未执行、完成、部分完成、未知 |
| `cancel / status` | 合作取消、回读进度/结果；终止后清理连接，效果不确定则进入对账 |
| `health / heartbeat` | launch instance、Provider V、队列/并发、Provider 自管数据库池的 open/in-use/target 分类计数和故障状态；供 Host 观察与准入，不伪装成 Host 强制计数 |
| `selftest` | 无真实客户凭据的运行时和合同自检；不得冒充目标数据库现场验收 |

开发流程：SDK 模板 → Provider → 能力/连接 Schema → 脱敏夹具与身份负例 → conformance kit → 平台构建离线制品 → 签名 → 隔离安装验证 → 真实目标验收 → 发布能力矩阵。标准能力内新增引擎无需修改平台业务代码；新增操作族、权限语义或不兼容结果类型必须先升级 SDK/宿主合同。

每次调用由宿主固定 `engine_id`、platform worker release、Provider/package/runtime digest、capability 版本、task/result schema、`request_id`、`operation_id`、attempt、绝对 deadline、目标/参数摘要、scope、授权和 `fence_epoch`。模型、数据库记录或 HTTP 请求均不能选择未登记的 RPC 方法、模块、函数、脚本、路径或命令，也不能利用 SQL/脚本参数绕过已登记 `capability_id` 和参数 Schema。

禁止任意入口不等于禁止现有专业 SQL 工具。已登记的专业 SQL capability 可以接收其版本化参数 Schema 明确允许的 SQL 文本/绑定参数，但宿主必须按目标、风险、权限、只读/写语义、结果上限和适用审批重新校验；Provider 只能在该 capability 的范围内处理。标准新引擎的首包不会仅因实现 SDK 而自动解锁 SQL 或写能力，必须逐项声明、实现和验收。

错误模型至少包含 `code/category/retryable/effect_state/safe_message/diagnostic_ref`，区分驱动加载、认证、TLS、权限、产品不符、服务端不支持、协议不兼容、超时、取消、worker 崩溃、部分结果和副作用未知。写调用断线或 worker 崩溃后禁止盲目自动重试，必须先依据持久回执、幂等声明和 `verify` 决定。

## 5. P/S/V 版本轴、RPC 兼容与扩展语义

- `P/P-1` 专指当前/上一平台 release，`S/S-1` 专指当前/上一 SDK/RPC 合同线，`V/V+1` 专指某认证格内 active/candidate Provider 版本；禁止再用一个通用版本符号同时表示三条轴。
- 平台发布窗口支持 P 与未经修改的 P-1，共享数据遵守第 3、11 节。SDK/RPC 支持 S 与 S-1；Provider 激活前须精确协商到其中一条，S-2 或更早明确返回 `contract_version_unsupported`，不得标成“未经验证但自动降级”。Provider 热事务只表示 V→V+1，不暗示 P 或 S 同步升级。
- S 与 S-1 共存期不得引入破坏性 contract：不得删除/改义字段、方法、错误分类、能力权限或结果语义，不得把 optional 变 mandatory。破坏性变化只能进入下一条明确协议线，并在旧线退出后实施。
- 未知 optional 字段：消费者忽略其行为影响；承担代理、持久化或回传职责的组件须原样保留，不能因 S-1 读写而静默丢失。未知 mandatory 字段、required capability bit 或无法理解的语义必须拒绝请求/激活。
- 私有扩展必须放在 `extensions.<publisher-or-engine>.<name>` 命名空间，不能覆盖标准字段、错误码或 capability ID；宿主不因看见私有字段而授予权限。
- 任务一经 `prepare` 即固定合同线和所有版本摘要；恢复、重试、验证不得静默换线。所需 Provider 已被吊销或不再兼容时，任务进入阻断/对账，而不是使用 active 版本猜测执行。

## 6. 本地 IPC、进程身份与资源上限

Provider 默认只通过本机 IPC：Linux/macOS 使用 Unix domain socket，Windows 使用命名管道。TCP 默认禁用。未来若引入回环 TCP，必须使用双向 mTLS、独立短期工作负载身份、禁用非 loopback 监听并单独完成威胁建模和验收；不能把 launch nonce 当作 TCP 的替代认证。

### 6.1 建立连接

- UDS socket 与父目录必须由专用宿主身份拥有，目录权限不得允许非授权用户创建/替换端点；Windows pipe 必须使用显式 DACL，仅允许宿主服务身份和指定 Provider 身份，并以 `PIPE_REJECT_REMOTE_CLIENTS` 拒绝远程连接。管理端点同样受此约束。
- 每个认证格必须实现并测试本 OS 的真实 peer 接口：Linux UDS 使用 `getsockopt(SOL_SOCKET, SO_PEERCRED)` 核对 PID/UID/GID；macOS 使用 `getpeereid()` 核对 UID/GID，并用 `getsockopt(SOL_LOCAL, LOCAL_PEERPID/LOCAL_PEEREPID)` 或 `LOCAL_PEERTOKEN` 核对可用的进程/audit 身份，不能把 Linux PID 接口写成 macOS 通用要求；Windows pipe server 使用 `GetNamedPipeClientProcessId` 后核对进程 token/SID，client 使用 `GetNamedPipeServerProcessId` 核对预期 Supervisor PID，并同时应用显式 DACL。若目标 OS/版本不能提供该认证格要求的足够 peer 身份，该格必须标为 unsupported，不得降级成只看 socket 路径、pipe 名或文件 owner。
- Supervisor 启动候选前创建 `launch_instance_id` 和只经受保护继承句柄/pipe 传递的 launch secret；不得进入命令行、普通环境变量、日志或可复用文件。Host 和 Provider 在每次连接分别生成新鲜独立的 `host_nonce` 与 `provider_nonce`。
- 双向 challenge 必须使用不同方向 label（至少 `host-to-provider`、`provider-to-host`），将双 nonce、`launch_instance_id`、双方 OS peer/PID、package digest、engine、V、宿主实例、协商 S/S-1 和 deadline 纳入认证 transcript，防止反射、跨连接和跨 generation 重放。
- 握手 transcript 和每帧都须由会话密钥保护完整性，每个方向使用独立、严格单调的 connection/frame sequence；重复、乱序、跳跃超窗和 Supervisor 重启后的旧帧必须拒绝。KDF/MAC/AEAD 和常量时间校验必须使用成熟、维护中的加密库及其受支持构造，禁止自造密码 primitive 或仅拼接 hash。

### 6.2 协议与默认上限

RPC 采用长度前缀的 UTF-8 JSON 消息和固定方法 allowlist；禁止 pickle、语言原生对象反序列化、由请求指定的 `eval/exec`、动态 import、任意方法名、任意可执行文件与任意文件路径。Provider 自身代码只经验签后的固定 SDK 入口加载，不能把业务输入当成加载指令。已登记 SQL capability 的 SQL 只是经 Schema 与授权校验的业务参数，不是动态 RPC/模块入口。二进制数据须使用已声明的分块编码或受控 artifact handle，并单独校验 digest/size/scope。

| 参数名 | 0.3 开发初始默认值 | 拒绝/处理语义 |
| --- | --- | --- |
| `max_frame_bytes` | 1 MiB（解码后） | 连接级协议错误并审计，不分配声明值以上缓冲 |
| `max_call_output_bytes` | 64 MiB（解压/解码后） | 中止并返回 `output_limit_exceeded`；部分结果不得冒充完整 |
| `max_queue_depth_per_generation` | 256 calls | 新调用 `busy/queue_full`，不无限排队 |
| `max_ipc_connections_per_generation` | 32 | 拒绝超额连接并记录 peer 身份 |
| `max_concurrent_calls_per_worker` | 8，且不得高于 manifest/能力声明 | 超额留在有界宿主队列 |
| `max_target_connections_per_worker` | 8 个活动数据库会话 | 受信 Provider 的合同/准入上限；Provider 自管会话与池并在 heartbeat 报告，Host 结合 SDK 统计和测试观察，不声称普通 broker 能从进程外强制阻止私建池 |
| `ipc_connect_timeout` / `launch_challenge_timeout` | 5 s / 10 s | 候选不可 ready；不切流 |
| `default_read_deadline` | 60 s，仅适用于未声明业务 deadline 的只读调用 | 到期发 cancel，随后按 effect 语义处理；长任务必须显式声明 deadline |
| `restart_backoff` | 1、2、4、8、16、30 s，之后最大 30 s，±20% jitter | 10 分钟内第 6 次崩溃打开 15 分钟 circuit；人工可在审计后恢复 |

这些数值须能由更低的 package capability 和部署策略收紧；放宽必须经过容量、安全和故障测试并版本化记录。0.3 不冻结启动性能或进程内存；数据库连接数只是防失控的安全上限，不是经测量的连接池容量。容量值必须在目标 OS/ABI、驱动和负载包上实测后另行冻结，不能从上述保护上限推导。所有数值都不是实际吞吐量或 7×24 证明。

T09 前必须具备三份精确匹配的早期容量输入：T04 的 `single-provider-capacity-v0`、T06a-core 的 `dual-generation-capacity-v0`、T08 的 `platform-ab-budget-v0`。三者都须记录 profile、OS/CPU、制品/驱动摘要、负载、启动/排空时长、RSS/进程/连接/句柄时序、阈值及 QA/OPS 冻结签署；先 calibration、后冻结正式 run。fake profile 只证明 fake，不得替代真实驱动容量。T14b/C67 再按冻结发布矩阵产生真实 `qualified-capacity-profile`，由 C84 按 engine、OS/CPU、driver/native ABI、Provider payload、load 和 context digest 精确消费；C67/T14b 不能反向成为 T09 前置。

Provider 在模型 A 中自行管理数据库会话与连接池；Host 通过 versioned SDK heartbeat、Provider 计数、进程/网络遥测和准入/容量测试观察合同是否满足。计数缺失、矛盾或实测越界必须阻断候选或隔离运行实例，但这仍是受信代码合同，不是对恶意 Provider 的强制沙箱。

Supervisor 必须将 Provider 及其子进程放入可回收的 OS process group/cgroup/job（按平台实现），跟踪 IPC 和临时 artifact。崩溃、超时终止或显式隔离时终止 worker/process group，回收本机进程、IPC、句柄和临时文件；进程退出通常会关闭其本地数据库 socket，但不能据此声称已在数据库服务端结束会话或撤回效果，无法确认时标成 `unknown/reconcile_required`。系统只执行通过签名、兼容和信任策略的包；独立进程提供故障与依赖隔离，**不能把不可信包变可信，也不是安全沙箱**。文件、网络、OS 账户和数据库权限仍须最小化。同机 OS 管理员、能读取/调试 Provider 的同权限恶意进程，以及已取得 Provider 运行身份的攻击者不在这层进程隔离的安全保证内。

## 7. 凭据短租约与撤销语义

- Provider 只接收宿主 secret broker 签发的短租约能力，不读取平台密钥库，也不能解析任意 `secret_ref`。租约绑定 Provider launch instance/package digest、`operation_id`、target、purpose 和最小权限。
- TTL 必须 `<= min(60s, remaining_deadline)`；它只限制 broker 发放/续发授权窗口，**不能让已经交付给 Provider 的可复用数据库密码在 TTL 到期时自动失效**。凭据不得进入命令行、普通环境变量、计划、结果、日志、core dump 或持久 Provider 状态；但合同不声称能阻止恶意 Provider 在其地址空间缓存已交付密码。长调用续租需宿主重新鉴权、重检 fence/deadline，并只发给原绑定实例。
- 正常 V→V+1 切流只停止把新任务给旧 V，不能撤销仍被 pinned 旧任务需要的租约。只有显式隔离、权限/凭据撤销或 `fence_epoch` 失效才停止新 secret 发放并撤销未消费租约；普通 deadline/cancel 是否停止续租按 capability 的取消/effect 合同处理，不能误伤仍获授权的 verify/reconcile。
- 对已建立会话，受信 Provider 必须合作关闭池/连接；必要时由目标数据库管理员撤销账号/密码/服务端会话。合作关闭超时后 Supervisor 终止 worker/process group 以回收本地 socket，但 Host 不承诺可直接关闭数据库服务端会话，也不承诺恶意 Provider 未缓存密码。
- 撤销只能阻止后续授权并尽力中断连接，**不能撤回已经提交或在目标数据库发生的效果**。会话或效果无法确认时必须持久化 `effect_state=unknown` 并走 `verify/reconcile`，禁止报告“TTL 到期/进程终止即已撤销或回滚”。

## 8. `.tmengine` 包与安装边界

```text
engine-package/
├── ENGINE-MANIFEST.json
├── ENGINE-MANIFEST.sig
├── runtime/       # 版本化 Provider/运行时
├── drivers/       # wheels、native client 或精确依赖清单
├── schemas/       # 连接、参数、输出
├── content/       # 巡检、规则、报告绑定
├── fixtures/      # 脱敏合同夹具
└── SBOM.json
```

清单必须含 `format/package_kind/package_id/engine_id/publisher/key_id/version`、host SDK/RPC 范围、最低平台版本、task/result/content schema、服务端版本/edition/兼容模式、OS/CPU/Python/native ABI、固定入口 ID、能力白名单绑定、依赖、逐文件 size/hash、私有状态 schema、迁移与回退条件、构建/来源分类。

payload 中每个文件必须恰好归入且只能归入一个类别：`self-built` 或具有真实上游来源证据的 `third-party-opaque`。自研 Provider/Schema/脚本以锁定源码、依赖和工具链及规范化规则比较独立 clean builder 的 `self_payload_digest`；第三方 wheel/native client 记录厂商或仓库来源、完整版本/ABI、原始 hash、签名验证状态、许可证和 SBOM，不承诺从厂商源码重编译出相同字节。同一锁定第三方 bytes 在不同 builder 间原始 hash 必须不漂移；变化须进入新包、新依赖锁和重新认证。未知 blob、重复分类或把自研不稳定输出冒充第三方一律拒绝。

签名覆盖规范化清单和**全部文件**摘要列表，包括第三方 opaque 二进制；自研可重复性与整包完整性是两个独立判定，第三方文件不得从完整性或验签范围剔除。签名/时间等允许变化的外层字段须单列并作机器可读解释，不混入 `self_payload_digest`，也不得从整包清单漏掉。安装器拒绝额外/缺失文件、重复或大小写碰撞路径、绝对路径、`..` 穿越、符号链接/硬链接/重解析跳转、解压炸弹、身份冲突、未知信任用途、同版本异内容和不受控入口。包禁止安装/卸载 shell hook、调用者指定的动态脚本、任意宿主 executable/path 声明、平台 DDL 和联网下载；native 依赖须在包内声明、校验并使用隔离搜索路径。正式包不得携带凭据或任何签名私钥。

SDK 定义受控运行 profile 与固定入口 ID；安装器将其解析为当前已认证 OS/ABI 上、不可变包目录内的已签名 Provider payload/运行时。新增引擎可以交付自己的 Provider 代码，不能以 profile/入口参数跳到宿主其他路径；新增运行 profile 本身需宿主合同升级和认证。此处拒绝的是任意执行入口，不是禁止合法 Python Provider 或 native 驱动代码。

## 9. 签名密钥与供应链生命周期

1. **根与用途**：信任根离线生成、离线保管，日常构建不可访问。根只签发信任元数据和用途受限的 release signing 子 key；子 key 限定 `package_kind=tmengine`、publisher、允许的 engine namespace、渠道、有效期和算法，不能签平台包或扩大权限。
2. **生成与保管**：release key 在受控 HSM/等价硬件边界内生成并使用，最小双人授权；私钥不可导出到 builder。签名记录制品 digest、builder identity、source revision、依赖锁、SBOM、时间和 trust generation。
3. **轮换与连续根链**：采用 TUF 式根轮换。新 root metadata 的版本必须恰为已信任版本 `+1`，同时满足旧根角色门限签名和新根角色门限签名；客户端逐版本验证连续 chain，禁止跳过中间根或仅凭更大的 `trust_generation` 接受。客户端持久化最高已接受版本并拒绝回滚。
4. **吊销**：吊销元数据由离线根签名，包含 key/package、原因、作用范围和 effective generation/time。被吊销 package 不得新安装、激活或自动回退；活动任务按风险策略阻断、隔离或进入对账，不能仅因签名曾有效继续使用。
5. **到期与离线时钟**：root/targets/snapshot/timestamp 等信任元数据必须有明确到期时间；一次更新流程固定验证开始时间。离线环境必须使用经过平台安全基线验收的本机时钟，并持久化不可后退的 last-seen time；时钟回退、明显漂移或无法建立可信时间时拒绝导入/激活新包，不能跳过 expiry。若采用签名时间票据，签发者和验证规则须由离线根显式授权，包内自报时间无效。
6. **旧签名回退包**：只有 digest 精确匹配、当前未吊销、仍满足 P/P-1、S/S-1 与 OS/ABI 认证格且保留策略允许的包，才可进入对应 `last_known_good` 集合。“签名发生在 key 有效期内”不能由包内时间自证，必须有可信签名审计/认证时间证据，或由当前受信根/targets 角色对同一 digest 重新背书；CLI 不能临时放宽。
7. **三类独立证据**：C73 分为 `self-payload`、`third-party-integrity`、`incident-rebuild` 三个 variant。自研 payload 须在固定源码/依赖/工具链下由至少两个独立 clean builder 复现相同规范化摘要；第三方 opaque 只验锁定来源、原始 hash、SBOM、签名状态及许可且 hash 不得漂移；incident variant 保留 builder/release-key 泄露后的冻结、干净重建和重新背书。任一 variant 不能替代另两个。 passed/executed 的 C73 叶实例必须内嵌 `oracle_receipt` 并通过该 variant 的 machine oracle；generic evidence 与 QA/REL 签署不能替代缺失的 locked inputs/builders/digests、opaque 来源/hash/SBOM/许可/签名状态、分类与整包文件清单或事件重建字段。
8. **泄露处置**：builder 或 release signing key 疑似泄露时立即冻结发布、提升吊销 generation、盘点已签 digest/安装/active/任务引用、隔离未确认包，并用干净 builder 和新 key 重建复核。旧包不能仅凭版本号重新信任；活动写任务的停止/对账须保留 effect 边界，不能粗暴删除包或历史证据。
9. **根门限泄露**：达到 root threshold 的密钥材料疑似泄露时，攻击者可能伪造更高版本/generation；此时不能靠在线发布一个更大 generation 自救。必须停止信任自动更新，并通过预定义带外通道重新建立根信任、重新盘点/背书 digest 和恢复客户端；流程及证据由 REL/OPS/SEC 按职责分离演练。
10. **测试根与生产根分离**：T03 的 `test-trust-ready` 由测试/演练根技术矩阵签发，供 T04、T13a、T06a-core、T08 和 T15a 消费；真实生产交接由 E07 `production-trust-accepted` 单独记账。DEV/CI/build 只能持测试根材料，禁止访问、导出或缓存生产私钥；正式 verifier 必须拒绝测试根签名。
11. **生产交接职责**：OPS 保管生产离线根/HSM 或等价介质与受控恢复份，REL 提供已测制品摘要、来源和签名请求，SEC 独立批准根、用途、轮换、吊销及根门限事件。仓库/日志/开发机只记录公钥指纹、门限、用途、角色/审批、托管证明及恢复流程引用，不记录私钥、恢复密语或可导出生产材料。
12. **同一 payload 的正式封装**：`production-release-ready` 必须同时满足 E07、对同一已通过测试矩阵的 payload 仅增加生产签名封装、生产公钥 verifier 成功、封装前后 payload digest 不变，以及受控 verify/install smoke 通过；交接后重构建或重组 payload 字节必须回到测试矩阵。T15a 只消费 test channel 矩阵，T15b 必须消费 production channel 门禁。

## 10. Provider 安装、激活、回退与卸载

Provider V→V+1 热事务：`verify → stage → start_candidate → handshake/selftest → candidate_ready → activate_generation → drain_old → retain_rollback → complete`。

- T13a 只实现初装/激活和**尚未对外接流**时的初激失败恢复；其 rollback 仅用于离线 lab 或未接流实例，不得包装成业务在线热回退。T13b/M2-core 才通过 T06a-core 暴露的受控内部接口管理完整热事务。
- T06a-core 的受控内部接口必须在无 T13b 时也能由合同测试驱动；T06a-core 不依赖 CLI 业务实现。T13b 只是授权、编排、审计客户端，不能绕过 Host 直接改 active 指针。T06a-real 和各真实引擎案例在此 core 合同上补充 L3，不得以真实引擎等待反向阻断 core。
- 新旧包位于不可变、digest 命名目录；激活使用 lease + generation CAS。包状态与更新操作状态分别记账。
- 新任务路由新 generation；原排队/执行/重试/恢复/补偿任务保持 `prepare` 固定的精确摘要及 Schema。旧进程只在调用、待对账和版本引用清零后退出。
- 回退只允许切到第 2、5、9 节共同确定、与当前 P/P-1、S/S-1 和 OS/ABI 认证格相容的 `last_known_good` digest，并只改变代码/路由；不得 down 平台共享数据或 Provider 共享状态。已发生副作用仍以持久回执和 verify 处理。
- 被吊销、隔离或只支持 S-2 及更早合同线的 digest 不得自动回退。对应认证格不存在安全候选时准确报告 unavailable/blocked，不能启用主程序内置只读替代。
- `disable` 停止新任务但不篡改旧任务绑定；`uninstall/gc` 必须同时检查 active、该认证格最后可用 digest 集合、任务/对账、审计留存和 artifact 引用。历史标准结果解码不构成 Provider 包保留理由，因为由核心 decoder 负责；原始证据与 Schema 定义仍按留存策略保存。

## 11. 平台 P/P-1 蓝绿与 Router 合同

事务：`verify → compatible_schema_expand → start_green → readiness → route_generation_CAS → drain_blue → retain_rollback → complete`。

- 初版平台热更新目标是**单节点**：固定 Router/Supervisor 与业务 release 分离，A/B 使用不同内部端口或 socket；不承诺硬件 HA、主机故障连续服务或整机维护无中断。
- T09 的单机平台合同硬依赖 T05a、T08、T06a-core/T13b 及三份匹配容量 v0；**不依赖 T06b、T06a-real、DM 或金仓**。T06b 的多节点 Provider 证据只在最终组合/发布矩阵汇合，不能成为单机平台切流前置。
- readiness 必须返回实际 release、共享 Schema 合同、SDK/catalog generation、worker 能力、T05a migration receipt/revision/checksum 和三份匹配容量 v0。候选 API 启动不能自动抢占 Scheduler/任务；Scheduler lease、claim、外部副作用和回执提交使用 fencing。缺 receipt、部分 DDL 未对账、容量不匹配或本次空闲资源不足时不接新流量，旧路由继续服务。
- 新请求切 green；在途请求/下载留 blue。旧/新静态资源按 release 共存，API 与共享数据库保持真实未经修改的 P/P-1 双版本兼容。
- 平台回退只切 Router 路由和相应代码到 P-1；managed-hot 禁止 down migration、自动共享库 restore、删除或逆向改写共享数据。无法由 P-1 安全解释或会被 P-1 无损往返破坏的数据/任务必须阻断并报告 `hot_rollback_eligible=false`，不得静默丢字段或猜测。
- Router/Supervisor 自身不是“永不更新”。必须提供独立的监听/socket handoff 用例，至少覆盖候选接管、在途连接、失败返还、双 owner 防护和实际 `route_generation`；缺该证据时只能声明业务 release 蓝绿，不得声明 Router 热更新完成。
- 缺资源、兼容路径、监听接管或安全回退依据时，预检必须给出 `hot_update_eligible=false`，不可自动改为停机升级后仍报告热更新成功。
- 多节点 Provider 收敛、节点 membership epoch 和容量切流属于 T06b，且依赖 T07；单机 Router 蓝绿与 T06a-core 结果不得外推为多节点或硬件 HA。

## 12. 本机管理 CLI 的授权与导入

T13a/T13b 的查询面可按本机策略开放；所有变更命令必须连接受保护的本机管理服务 IPC，由服务端按**本机管理员 OS 身份**重新授权，不能靠 CLI 进程自报角色、文件所有者或包内声明授权。

- 禁止远程管理监听、Web 变更入口、透传任意 executable/参数、任意服务器路径或 Provider hook。未来远程入口必须另立威胁模型、认证授权与验收任务。
- 每次操作生成唯一 `operation_id`，审计 OS user/SID、有效组/提升状态、会话/peer PID、命令、engine/package digest、前后 generation、`trust_generation`、授权决策、阶段 journal、结果和 diagnostic reference；审计不得含秘密。
- 包只能从配置的受控导入根进入。服务端用目录句柄相对打开，拒绝 symlink/hardlink/reparse point、跨文件系统跳转、非普通文件和权限不合规对象；打开时使用等价 `no-follow` 控制。
- 校验前后必须对同一已打开句柄核对 file identity、size 与 metadata，将字节复制到服务端私有 staging 后再以 staging 内容计算 digest/验签，避免路径复查与打开之间的 TOCTOU。客户端给出的名称、路径和摘要都只是请求，不是信任依据。
- 变更命令幂等并逐阶段持久 journal；崩溃恢复从已验证检查点继续或安全回滚，不重新执行不可判定副作用。

Web/API 首期只读提供 engine catalog、连接 Schema、运行身份、版本兼容评估、影响、进度、历史和审计；业务调用仍走平台任务/能力 API。无模型时进入专业页仅导航、零执行。

## 13. 结果解码、停用后可读与诊断

- Provider 只产生由 manifest 声明的标准 versioned result envelope 与 namespaced 原生证据；核心在写入时保存原始 envelope、schema ID/version、Provider identity、内容 digest 和解码状态。
- 核心维护当前、S-1 及结果留存范围内历史 Schema 的纯数据 versioned decoder。decoder 不导入、启动或执行 Provider 代码，不访问客户数据库，不借解码提升权限；其发布和回归归 CORE/QA。
- Provider 被 disable、uninstall、吊销、OS/ABI 不再可运行后，历史标准结果仍须可读。未知 optional 字段按第 5 节保留；未知 mandatory 语义返回明确 `decoder_unsupported` 并保留原始证据，禁止伪造可读结果。
- legacy decoder 只表达历史证据拥有的字段，不以当前默认值补齐新字段。Schema 保留周期必须至少覆盖对应结果/审计留存周期；若不能继续支持，须在数据保留策略中显式迁移或导出，不能靠保留可执行 Provider 充当 decoder。
- 诊断至少暴露平台/Provider 实际摘要、RPC 协商线、route/catalog/trust generation、候选 readiness、IPC peer/launch instance（脱敏）、在途数、队列/连接/输出限额事件、崩溃 circuit、排空进度、旧任务引用、Schema 兼容、恢复检查点和安全错误原因；禁止输出秘密、nonce、原始连接串或私钥材料。

## 14. 环境叶输出、阶段门与并行消费

C70 是案例家族/分组父节点，不是可执行通过或失败门，也不计入通过数。其执行实例固定为 `runtime`、`core`、`pminus1`、`dm`、`kingbase`、`native` 六个 variant；`native` 继续按 Oracle、MySQL、PostgreSQL、SQL Server 与 OS/CPU 等矩阵拆 cell。每个实例的精确执行键至少为 `(case_id, variant_id, cell_id, requirement_context_digest)`，分别记录 `status/executed/evidence/acceptance_roles`。任何阻塞 fixture 必须携带精确 `variant_id/cell_id/output/context` selector；提供方 T14a 整任务未结束时，消费者也可以消费已经签发的叶输出。父案例家族共五个：C67、C70、C73、C83、C84；父节点均不可直接执行或计为 passed。

T14a 只交付 `cell catalog`、介质/许可/环境/资源事实和相应叶输出，不签发 `all-advertised-cells-ready`，也不根据现有环境反推发布范围。REL/T14b 必须在执行前冻结 `release_scope_manifest` 及 required cells；“六引擎全矩阵”范围至少覆盖真实 Oracle、MySQL、PostgreSQL、SQL Server、DM、KingbaseES 以及声明的 OS/CPU/ABI/profile/context，不能用只有 MySQL 或任一局部可用格的 scope 缩写成全矩阵。

C84 也是不可直接执行的 case family，须按冻结范围为真实六引擎及其 OS/CPU/driver-native ABI/load profile/context 逐 leaf 生成 `real-combination` 实例并输出 `qualified-real-combination`。每个 C84 leaf 必须消费该格源码实质不同且经过认证的真实 V/V+1 pair、精确匹配且 passed 的 C67 `qualified-capacity-profile`，并验证平台/Provider 组合顺序、第二轴失败、回退及需要的 T06b 多节点上游证据；fake pair 或其他格的 C67 receipt 不可代替。局部 C70/C67/C84 leaf 可以先通过，且提供方整任务未结束不妨碍消费该叶输出，但消费者必须验证该输出自身的 selector、状态、证据、联合验收和全部前置，不能只看 receipt ID 或父/任务状态。

`release-matrix-ready:test` 是 T14b 对冻结范围的非直接执行派生收据，不得由一条 passed receipt 直接签发。它必须同时核验有效且未漂移的 required cells、每个 required engine 的真实 pair、逐格 C67 profile、逐格 C84 `qualified-real-combination` leaf，以及发布范围要求的上游 T06b 等任务证据；任一 required leaf 的 skipped、not_run、unsupported、selector 不匹配、证据/联合验收缺失或前置失效都阻止聚合通过。生产 `production-release-ready` 同样是非直接执行派生收据，还必须建立在仍有效的 test 矩阵之上，并满足 E07、同一已测 payload、生产 verifier 和受控 verify/install smoke；生产封装不能绕过或代替 test 聚合。 C84 的 `test-matrix` / `production-envelope` 仅允许出现在 `output_instances`，不能进入 `test_instances`，不能记为 `executed=true` 或计入业务执行数。

阶段门分开计算：`M1-base` 由 T04/T05a/T05/T13a、T03[`test-trust-ready`] 及所需 runtime/core/P-1 叶输出组成，不等待 T03 整任务或生产根交接；`M1-DM` 由 T11 与精确 `dm-ready` 叶输出组成。T06a-real 只在 M1-DM 与 core 汇合；其等待不能阻断 T06a-core、T07、T06b、T13b、原生格、T10、T08/T09。

T10 早期目录/UI 证据只消费 catalog、签名 fake 和实际 Host 的 L1/L2，不要求 DM 或 T06a-real；真实引擎页面与真实结果 L3 留在后续组合案例。T12 首包不依赖 T10 或 T06a-core，只交付 KingbaseES 真实首包及源码实际不同的 `kingbase-vv1-pair` 给 C84；真实组合切流由 C84 汇合 core、UI、T06b、精确 C67 容量 profile 与各引擎真实 pair，不能把 fake pair 当成六引擎证据。

## 15. 0.3 合同检查结果

| 检查项 | 0.3 文档结果 | 待实现/验收证据 |
| --- | --- | --- |
| 迁移 owner 与真实 P-1 | 已明确 T05a/CORE 独占；P-1 不改造、只写旧形态；P fallback/兼容写，附表或 CAS 防覆盖 | migration dry-run、幂等 backfill、未修改 P-1 往返和不可回退负例 |
| 原生四引擎抽取 | 已区分 T02b/M0、T02c/M1 候选包自检及 M2 产品首装可用；C01 主归 T02c/M2 | 四包、真实入口接管、首装失败负例、按认证格回退集合与引用保护 |
| IPC 与 secret | 已明确逐 OS peer 接口、方向 label、fresh 双 nonce、transcript/逐帧序号完整性；TTL 不伪装密码失效 | 跨 OS peer 缺失、反射/重放、Provider 不合作、撤销/崩溃清理用例 |
| DB 连接模型 | 已采用受信 Provider 自管池；Host 以 heartbeat/遥测/实测观察 8 会话合同上限，不宣称 broker 强制阻止私建池 | 计数矛盾、越界准入、合作关闭和终止 process group 后 unknown 用例 |
| 签名生命周期 | 已明确 TUF 式连续双门限根链、expiry/离线时钟、可信签名时间、根门限泄露带外重建 | REL ceremony、HSM/等价控制、逐根轮换/过期/泄露与独立重建演练 |
| P/S/V 与破坏性 contract | 已区分 P/P-1、S/S-1、V/V+1；只退代码/路由，不 down 共享数据；S-2 拒绝 | P/P-1 数据往返、S/S-1 RPC、V→V+1、字段保留和 contract 后置演练 |
| 专业 SQL 能力 | 禁止任意入口不删除现有查询能力；只允许登记 capability/Schema 内 SQL 并逐项鉴权、风险和审批 | 允许的专业查询、SQL 越权/绕过、首包不自动解锁写能力负例 |
| CLI 授权与任务依赖 | 已明确 T13a 最小离线边界、T13b→T06a-core 单向依赖、本机管理员服务端授权和安全导入 | OS 权限矩阵、离线/在线边界、symlink/reparse 与恢复负例 |
| 历史结果解码 | 已明确核心 versioned decoder，卸载 Provider 后仍可读，不执行 Provider 代码 | legacy/current fixture、卸载/吊销后离线回读和 unsupported 负例 |
| 单机/多节点承诺 | 已明确初版单机蓝绿，无硬件 HA；Router 有独立 handoff；T06b 依赖 T07 | Router handoff 故障用例；T06b/T07 后续多节点证据 |
| 环境与发布汇总 | 已明确五个 case family 父不计通过；C70 六 variant/cell/context 叶 selector；C84 按真实六引擎逐格消费 pair/C67/必要上游；T14b 先冻结 required cells | 叶实例执行记录、输出自身前置复验、单 receipt/仅 MySQL 不得汇总六引擎矩阵的负例 |
| 迁移启动接线 | 已明确 T05a 输出 migrator-contract、T08 硬依赖并接线、managed-hot 单 migrator/API-Worker 不 DDL | 多启动竞争、部分 DDL reconcile、receipt/readiness 与不 down/restore 负例 |
| 测试/生产信任 | 已区分 test-trust-ready 与 E07；SEC 独立，生产私钥不进 DEV；T15a/test、T15b/prod | 真实交接公开记录、prod verifier、同 payload 封装与 install smoke |
| 构建与完整性 | C73 已拆三 variant；分类穷尽互斥、第三方原始 hash 不漂移、整包覆盖全部文件 | clean builder、opaque 来源/hash、incident rebuild 与漏文件负例 |

0.2 审核曾称设计缺口已具名闭合；本轮复核已修正其中仍存在的依赖、环境粒度、迁移接线、容量、生产信任及供应链缺口，因此该历史结论不再作为当前状态。0.3 文档同步只表示上述约束已纳入有效计划基线，**不表示任何实现、容量、安全性、热更新、数据库支持或现场验收已经通过**。修订依据见[具体变更计划](change-plan-0.3.md)。

## 16. 官方 primary 参考

- [Microsoft Learn：Named Pipe Security and Access Rights](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)：Windows pipe security descriptor、DACL 与连接访问检查。
- [Microsoft Learn：GetNamedPipeClientProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeclientprocessid) 与 [GetNamedPipeServerProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeserverprocessid)：连接后核对 pipe client/server PID。
- [Microsoft Learn：CreateNamedPipe](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createnamedpipea)：显式拒绝远程客户端的创建标志。
- [Linux man-pages：unix(7)](https://man7.org/linux/man-pages/man7/unix.7.html) 与 [Apple XNU：sys/un.h](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/sys/un.h)：各 OS 的 peer credential 接口依据；实际支持仍须逐目标 OS 制品认证。
- [The Update Framework Specification v1.0.36](https://theupdateframework.github.io/specification/v1.0.36/)：root threshold、连续版本、旧/新根双门限签名、expiry、rollback/freeze 防护。本文借鉴其信任更新模式，不因引用即宣称实现已符合 TUF。
