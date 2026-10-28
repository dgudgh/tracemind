# 测试与验收案例

版本：0.3，2026-10-26。84 个案例条目中 C67/C70/C73/C83/C84 为 case family 父分组；父不执行、不计通过，各 variant/cell/context 实例独立关闭。业务实例全部未执行，test_instances/output_instances 当前为空。readiness-fixtures.json 仅给规划检查器做合成正反例，不是业务结果。

L1＝离线合同/故障夹具，L2＝目标 OS 上实际构建制品/进程，L3＝真实驱动/目标/部署/浏览器/现场。普通案例列多个级别时须分别有证据；父分组的级别范围只描述下属实例，不生成父通过状态。

每个普通案例和每个叶实例只有一个 primary_task。QA 与列出的另一责任角色联合验收，不能只有开发者自签。协作分类、阻塞语义及精确 selector 以两份 v3 JSON 为准；晚阶段复验不得反向卡住 core。

| ID | 级别 | 主责任务 | 协作任务 | 联合验收角色 | 场景与通过条件 |
| --- | --- | --- | --- | --- | --- |
| EP-C01 | L2/L3 | EP-T02c | EP-T02b、EP-T04、EP-T05、EP-T06a-core、EP-T13b、EP-T14a | QA、ENG | 产品首次安装包含四个原生签名 Provider，真实驱动自检和已支持能力 ready，默认登记且无需用户另装；M1 候选包安装/自检不能替代本条 M2 产品接管验收 |
| EP-C02 | L1 | EP-T02 | EP-T02b、EP-T05a | QA、CORE | 对迁移前录制夹具离线对照，旧实例 ID、权限、能力 ID、API 路径、`db_type` 语义和历史回执不漂移；真实运行链另由高层案例验收 |
| EP-C03 | L1 | EP-T02 | EP-T03 | QA、ARCH | 普通外部包覆盖原生 `engine_id`、跨发布者冒名、重复注册或借 `family/protocol` 继承能力均被 Registry 拒绝 |
| EP-C04 | L1/L2 | EP-T13a | EP-T02、EP-T03 | QA、OPS | 安装一个新标准引擎只增加该 Provider 的不可变目录和 Registry 记录，平台、其他 Provider 及其配置摘要保持不变 |
| EP-C05 | L1/L2 | EP-T04 | EP-T01、EP-T03 | QA、ENG | 清单与 runtime handshake 的 SDK/RPC、`engine_id`、driver、entrypoint、runtime digest 任一不符时 Supervisor 不把候选置为 ready/active |
| EP-C06 | L1 | EP-T02 | EP-T01、EP-T10 | QA、CORE | 未安装、禁用、未支持或运行条件不足的能力在目录快照中返回稳定原因码，不被菜单、API 或 AI 目录当作可执行 |
| EP-C07 | L1/L2/L3 | EP-T14b | EP-T02、EP-T11、EP-T12 | QA、ARCH | 跨六引擎制品与目标的协议兼容、端口特征、伪造版本字符串负例矩阵不通过强产品身份核验；DM/KingbaseES 保持各自 canonical `engine_id`，目录映射的 M0 退出另见 C57 |
| EP-C08 | L1/L3 | EP-T05 | EP-T04、EP-T13a | QA、CORE | 用户、资产范围、目标摘要变化或授权撤销后，prepare/execute/verify 每个边界按调用时授权重新拒绝，并记录无秘密审计证据 |
| EP-C09 | L1 | EP-T01 | EP-T04 | QA、SDK | 模型、HTTP 或数据库记录以任意 SQL/脚本/模块/方法绕过已登记 capability 与参数 Schema 时拒绝；已登记专业 SQL capability 的合同内 SQL/绑定参数按目标、权限、风险、只读/写及审批校验后允许；标准新引擎首包不因协议兼容自动解锁该能力 |
| EP-C10 | L1/L2 | EP-T04 | EP-T03、EP-T05 | QA、ENG | 凭据、证书和原始异常不泄漏到 argv、普通环境、清单、计划、回执或日志；仅经受限 IPC 交付给被授权 worker |
| EP-C11 | L1/L3 | EP-T05 | EP-T01、EP-T04 | QA、CORE | prepare 无业务副作用；相同 `operation_id/request_id/attempt` 的重复请求只产生合同允许的一次执行和可重放回执 |
| EP-C12 | L1/L3 | EP-T05 | EP-T04、EP-T07 | QA、CORE | 写调用断线、worker 崩溃或回执不完整时先进入 verify/对账，不盲目重试、不伪报成功，`effect_state` 准确 |
| EP-C13 | L1/L2 | EP-T04 | EP-T05 | QA、ENG | `ipc_connect_timeout=5s`、`launch_challenge_timeout=10s`、无显式业务 deadline 的只读调用 `default_read_deadline=60s`，合作取消和最终强制终止逐级生效；连接/临时文件/受控子进程清理，任务状态与不确定效果准确 |
| EP-C14 | L1/L2 | EP-T04 | EP-T14b | QA、ENG | 默认上限 `max_call_output_bytes=64 MiB`（解压/解码后）、`max_queue_depth_per_generation=256`、`max_ipc_connections_per_generation=32`、`max_concurrent_calls_per_worker=8`、`max_target_connections_per_worker=8` 生效；受信 Provider 自管 DB 会话并上报，Host 以 heartbeat/SDK 遥测和实测验合同，不声称 broker 能阻止恶意私建池；放宽须容量安全验收 |
| EP-C15 | L1 | EP-T03 | EP-T13a | QA、REL | 未签名、未知 Key ID/信任用途、文件或清单篡改、额外未签名文件均不能 verify/install |
| EP-C16 | L1/L2 | EP-T03 | EP-T13a | QA、REL | 越界路径、大小写/Unicode 归一化碰撞、重复路径、符号链接、重解析点和解压炸弹在写入 active 根目录前被拒绝 |
| EP-C17 | L1 | EP-T03 | EP-T02 | QA、REL | 同 `package_id/version` 不同内容不可覆盖；包身份、manifest digest、runtime digest 可追溯且不可变 |
| EP-C18 | L2 | EP-T04 | EP-T03、EP-T14a | QA、ENG | 断网、缺 native 库、错误 OS/CPU/Python/native ABI 在候选运行前明确失败；不联网补依赖、不污染宿主搜索路径 |
| EP-C19 | L1/L2 | EP-T03 | EP-T13a、EP-T13b | QA、REL | 发布密钥轮换和包吊销可追溯；吊销信息刷新后禁止新激活，被吊销版本不得成为自动/手工旧包回退目标，既有任务转阻断/对账 |
| EP-C20 | L1/L2 | EP-T13a | EP-T03、EP-T04 | QA、OPS | install/stage/首次 activate/status/recover 各 journal 点注入断电或 kill 后可幂等恢复，无半安装 active；回退只针对尚未服务的初始激活失败或隔离实验，不冒充 Provider 热切流 |
| EP-C21 | L1/L2 | EP-T06a-core | EP-T04、EP-T05 | QA、ENG | 签名 fake Provider 的真实 V/V+1 进程并存，L1/L2 验 readiness、generation CAS 与新任务切换，pinned 旧任务保留原版本；不据此认证厂商驱动。 |
| EP-C22 | L3 | EP-T06a-real | EP-T11、EP-T02b | QA、ENG | 真实 DM V/V+1 的审核源码和规范化 self payload 实际不同（排除仅版本号/签名外层变化），在精确 dm-ready 格持续执行只读业务，核验真实连接、其他已激活 Provider 和持久结果连续。 |
| EP-C23 | L1/L2 | EP-T06a-core | EP-T13a、EP-T05 | QA、ENG | core 的签名 fake Provider 在 readiness 前与 CAS 后分别故障：不提前切流，兼容且未吊销时回切，旧任务和回执保持；真实驱动故障另由 C79 验。 |
| EP-C24 | L1/L2 | EP-T05 | EP-T06a-core、EP-T13b | QA、CORE | 旧任务的重试、恢复、补偿固定原 provider identity、capability/task/result 版本；精确版本缺失时阻断，不伪装成最新版执行 |
| EP-C25 | L1/L2 | EP-T13b | EP-T05、EP-T06a-core | QA、OPS | disable 只停止新调度并排空固定任务；active/rollback/任务/结果引用保护 uninstall/gc，命令重放不删共享配置、客户数据或仍需恢复的包 |
| EP-C26 | L1/L2 | EP-T13b | EP-T06a-core、EP-T06b | QA、OPS | 同引擎并发 upgrade/rollback/disable/uninstall/gc 中只有一个管理事务获租约；陈旧 generation、journal 或 fence token 不能双激活、双执行或误删 |
| EP-C27 | L2/L3 | EP-T09 | EP-T08、EP-T07、EP-T14b | QA、REL | 单机真实平台 P/P-1 A/B 在匹配的三份 v0 及冻结负载下持续服务，无更新引发的 5xx/连接重置/任务丢失；不等待 DM、多节点或 T14b 全矩阵。 |
| EP-C28 | L2 | EP-T08 | EP-T03、EP-T07 | QA、UI | 用实际 Router 制品、stub A/B 服务和两套 hash asset 验固定入口、在途请求/下载落原 stub、旧资源保留、未提交输入/任务编号恢复；完整 P/P-1 切流留 T09 案例 |
| EP-C29 | L1/L2 | EP-T07 | EP-T05、EP-T06a-core | QA、CORE | 两个版本化 Worker 与模拟 API 替换期间，排队/执行/对账任务守恒，租约接管及旧 owner fencing 生效，副作用/结果不重复；T09 在完整 P/P-1 蓝绿中复验 |
| EP-C30 | L1/L2 | EP-T07 | EP-T05、EP-T06a-core | QA、CORE | 候选 API 启动不自动成为有效 Scheduler/Worker；消费接管须获租约、membership/fence 及版本约束，旧 owner 提交被拒绝 |
| EP-C31 | L2 | EP-T09 | EP-T07、EP-T08 | QA、REL | 候选平台 readiness 的实际 release、Schema、SDK/catalog 或 worker 能力不符及启动失败时不切流、不停止 blue |
| EP-C32 | L2/L3 | EP-T09 | EP-T07、EP-T08 | QA、REL | 平台路由切换后 green 故障，在兼容条件内 CAS 回 blue；新旧已执行任务均按持久回执核验，不回滚共享业务数据 |
| EP-C33 | L2/L3 | EP-T05a | EP-T14a、EP-T05b、EP-T05、EP-T09 | QA、CORE | 只消费 T14a 登记的合格 P-1，或 no-qualified 分支由 C78 冻结并登记的桥接基线 B；在 expand/backfill 窗口内让未经修改的 P-1/B 与 P/B+1 同时读写并校验一致；两者均无资格则 T05a not_ready，contract 另行门禁，legacy 迁移不冒充热回退 |
| EP-C34 | L1/L2 | EP-T09 | EP-T05a、EP-T08 | QA、ARCH | 无兼容路径、不可逆数据变化、资源不足或无安全回退依据时 `hot_update_eligible=false`，不降级为停旧启新仍报告热更新成功 |
| EP-C35 | L2/L3 | EP-T08 | EP-T03、EP-T07、EP-T15a | QA、OPS | Router/启动器自更新单独演练套接字/监听接管或冗余入口；缺条件时业务 A/B 通过也不得把基础入口发布成无中断，T15a 只做下游环境复验 |
| EP-C36 | L1/L2 | EP-T09 | EP-T01、EP-T06a-core | QA、ARCH | 平台 P/P-1↔SDK S/S-1↔Provider V/V+1↔task/result↔核心 Schema 兼容图求交集；独立升级或回退不偷换另一版本轴 |
| EP-C37 | L2/L3 | EP-T06b | EP-T06a-core、EP-T07 | QA、OPS | 多执行节点按 `membership_epoch` 快照预备相同 digest 和容量；新旧/离线/迟到节点只接收匹配 generation 的任务 |
| EP-C38 | L2 | EP-T09 | EP-T08、EP-T05a | QA、REL | 平台迁移与蓝绿的每个持久点注入 kill：含迁移获锁前后、部分 DDL、校验后回执前、回执后候选 ready 前；恢复先核对真实结构/回执，未知进入 reconcile_required，hot 流程禁止 down/自动共享库 restore。 |
| EP-C39 | L1/L2 | EP-T10 | EP-T02、EP-T04、EP-T05、EP-T06a-core、EP-T11、EP-T12 | QA、UI | 用 catalog fixture、fake Provider 与实际 Host 制品执行 L1/L2：目录/字段/权限/provider version 同一 generation，陈旧上下文响应丢弃；不等待真实 DM/金仓，真实浏览器完整链由 C48 验。 |
| EP-C40 | L1/L3 | EP-T10 | EP-T05、EP-T07 | QA、UI | 无模型时导航专业页为零执行；有模型只选择 Registry 已登记、当前有权且运行可用的能力，最终仍走同一持久任务入口 |
| EP-C41 | L2/L3 | EP-T11 | EP-T04、EP-T14a | QA、ENG | DM 实际签名制品加载选定 dmPython/DPI/native client，连接、权限、TLS、字符集和只读会话策略通过冻结矩阵 |
| EP-C42 | L3 | EP-T11 | EP-T02、EP-T14a | QA、ENG | DM 实际产品、完整版本、edition/兼容模式和稳定实例身份明确；错误产品、伪造版本及不支持版本被拒绝 |
| EP-C43 | L3 | EP-T11 | EP-T05、EP-T14a | QA、OPS | DM 已声明的拓扑、指标、会话、锁、只读巡检、报告和持久回读逐项通过；未声明或缺失证据明确表达而不假健康 |
| EP-C44 | L2/L3 | EP-T12 | EP-T04、EP-T14a | QA、ENG | KingbaseES 实际签名制品加载选定 ksycopg2/libkci，ABI、权限、TLS、字符集和只读会话策略通过冻结矩阵 |
| EP-C45 | L3 | EP-T12 | EP-T02、EP-T14a | QA、ENG | KingbaseES 兼容模式不冒充 PostgreSQL，实际版本、实例/集群身份以及错误产品与伪造身份负例通过 |
| EP-C46 | L3 | EP-T12 | EP-T05、EP-T14a | QA、OPS | 金仓首包已声明的拓扑、指标/会话/锁/巡检/报告逐项真实回读，缺失关系/延迟明确表达；另交真实源码不同的 kingbase-vv1-pair 给 C84，首包不暗加 UI/core 切流依赖。 |
| EP-C47 | L2/L3 | EP-T02c | EP-T02b、EP-T04、EP-T05、EP-T06a-core、EP-T13b | QA、ENG | 四个原生 Provider 均不再由平台主进程静态执行，分别以独立签名制品启动、激活、按 V/V+1 逐个热升级/回退；迁移前后源码行为基线一致，源码夹具不能替代制品/runtime 证据 |
| EP-C48 | L3 | EP-T15a | EP-T14b、EP-T09、EP-T06a-real、EP-T11、EP-T12 | QA、OPS | 在独立预生产环境中，六引擎分别贯穿需求→能力→Provider→真实目标→持久结果→API→浏览器；平台/Provider 前后版本和更新期间结果均可回查 |
| EP-C49 | L1 | EP-T02b | EP-T01、EP-T02 | QA、ENG | 抽取 Oracle/MySQL/PostgreSQL/SQL Server 迁移前源码行为夹具，冻结连接身份、能力、错误、结果和路由基线；退出条件仅为离线对照可重放，不声称签名包或 runtime 已验收 |
| EP-C50 | L1 | EP-T01 | EP-T04 | QA、SDK | SDK 自带 fake runner 与 Provider 使用当前 S 合同时，handshake、方法、错误和结果 Schema conformance 全部通过，形成不等待 T04 runtime 的 T01 独立早期退出证据 |
| EP-C51 | L1 | EP-T01 | EP-T04 | QA、SDK | S-1 只有在兼容矩阵明确声明时可运行；未声明的 S-1 方法/字段组合被明确拒绝，不靠宽松反序列化碰运气 |
| EP-C52 | L1 | EP-T01 | EP-T04 | QA、SDK | S-2 Provider 一律在 handshake/激活前按 `contract_version_unsupported` 拒绝，不能协议降级后继续运行 |
| EP-C53 | L1 | EP-T01 | EP-T04 | QA、SDK | Schema 中未知 optional 字段按合同忽略，但代理与持久化必须原样保留；重放后数据不被误删或提升为权限输入 |
| EP-C54 | L1 | EP-T01 | EP-T04 | QA、SDK | 未识别 mandatory 字段、required capability bit 或枚举语义立即 fail closed，返回可诊断兼容错误而非采用默认值 |
| EP-C55 | L1 | EP-T01 | EP-T02 | QA、SDK | `publisher.namespace/key` 扩展仅在声明的 namespaced 容器中往返；冲突核心字段、跨命名空间读取或借扩展新增宿主权限均被拒绝 |
| EP-C56 | L1 | EP-T01 | EP-T04 | QA、SDK | RPC 协商遭版本删改、重复字段、能力剥离、强制 fallback 或中间人降级时 handshake 失败并审计，不回落到未声明协议 |
| EP-C57 | L1 | EP-T02 | EP-T01、EP-T02b | QA、CORE | Registry 兼容桥对旧 `db_type`、canonical `engine_id`、原路由和 availability reason 使用确定映射；未知/歧义身份 fail closed，形成 T02 独立早期退出证据 |
| EP-C58 | L1/L2 | EP-T03 | EP-T13a | QA、REL、OPS | 离线信任根按双人授权的签名元数据从 root K 更新到 K+1，验证防回滚、阈值、有效期、`trust_generation` 和信任用途；无联网取钥依赖，旧根撤除前后验签符合窗口；另单列 root threshold 泄露：停止自动更新，带外重建根信任，客户端重新锚定，盘点并重新背书受影响 digest，独立保存演练证据 |
| EP-C59 | L1/L2 | EP-T03 | EP-T14b | QA、REL | 构建隔离与发布扫描证明 `.tmengine`、日志、缓存、SBOM、provenance 和调试符号不含签名私钥、凭据、内部连接串或未声明绝对路径；泄露即阻断发布 |
| EP-C60 | L1/L2 | EP-T04 | EP-T01、EP-T05 | QA、ENG | 各目标 OS 的真实 peer API/ACL 验证；fresh host/provider 双 nonce、方向 label、完整 transcript 与双向 connection/frame sequence 使用成熟密码库保护，反射/重放/乱序/跨 generation、错误用户或伪造 `launch_instance_id/provider_identity` 均拒绝；接口不足的认证格标 unsupported |
| EP-C61 | L1/L2 | EP-T04 | EP-T05 | QA、CORE | secret lease TTL 不超过 `min(60s, remaining_deadline)` 并绑定 scope/调用；到期/撤权只停止新 secret 发放与未消费租约，不伪称已交付密码失效；Provider 合作关连接，超时终止 process group，服务端会话/效果未知则 `reconcile_required`；正常 V→V+1 不误伤合法 pinned V 任务 |
| EP-C62 | L1/L2 | EP-T04 | EP-T01 | QA、ENG | 截断、超过解码后 `max_frame_bytes=1 MiB`、递归、类型混淆、重复键、非法编码和乱序 RPC 帧在有界反序列化中安全失败，不崩溃、不越界分配、不执行部分请求 |
| EP-C63 | L1/L2 | EP-T04 | EP-T13a | QA、OPS | Supervisor/Provider 崩溃和宿主重启后，旧子进程、socket、凭据文件及 DB 连接被回收；重启退避为 1/2/4/8/16/30 秒后封顶 30 秒且 ±20% jitter，10 分钟第 6 次崩溃开启 15 分钟 circuit，只恢复经 journal 核验的 generation |
| EP-C64 | L1/L2 | EP-T05a | EP-T02、EP-T05 | QA、CORE | 新增 `engine_id` 与兼容 `db_type` 字段按 expand→可重入 backfill→校验推进；真实未修改 P-1 只写旧字段，P 能 fallback 读取并兼容写旧形态，新语义进入附表或 CAS 保护；空值、DomesticDB、未知和冲突行隔离，无法无损兼容时禁止热回退 |
| EP-C65 | L1/L2 | EP-T05 | EP-T01、EP-T05a | QA、CORE | 旧任务/API 请求不能伪造或覆盖 `engine_id`、provider identity、worker release、task/result 版本；宿主从持久绑定恢复，版本错配拒绝执行/解码 |
| EP-C66 | L2 | EP-T09 | EP-T06a-core、EP-T05a、EP-T08 | QA、REL | 单机平台 P/P-1 与签名 fake Provider V/V+1 的 L2 组合事务：两种顺序、第二轴失败、回退及任务绑定；不使用此结果关闭 C84 的真实六引擎组合矩阵。 |
| EP-C67 | L2/L3 | EP-T14b | EP-T04、EP-T06a-core、EP-T09、EP-T06a-real | QA、OPS | 父分组：按冻结 required_capacity_profiles 生成实际 profile 实例，测真实驱动/OS/CPU/并存峰值/连接/排空；各实例签 qualified-capacity-profile，由 C84 按同 engine/ABI/payload/load/context 精确消费。 |
| EP-C68 | L1 | EP-T05 | EP-T01、EP-T05a | QA、CORE | 从隔离夹具目录移除一个无任务执行引用的 fake Provider 后，核心 versioned decoder 仅凭持久标准快照和登记 Schema 回读已知版本结果，不访问数据库、不加载 Provider；T13b 在 C25 复验实际 uninstall |
| EP-C69 | L1/L2 | EP-T13a | EP-T05、EP-T03 | QA、OPS | 本机 CLI 的 inspect/verify/install/activate/初始 rollback/status/recover 经 OS 身份、角色、目标根与命令级授权；未授权用户、提权绕过、任意路径和审计缺口均拒绝 |
| EP-C70 | L1/L2/L3 | EP-T14a | EP-T03、EP-T04、EP-T11、EP-T12 | QA、OPS | 父分组：runtime/core/pminus1/dm/kingbase/native 六 variant 按 cell/context 独立记账和签发；父不执行不计通过，DM 等待不影响 native.mysql 已通过；发布 required cells 由 REL/T14b 在 run 前冻结。 |
| EP-C71 | L2 | EP-T14b | EP-T02c、EP-T03、EP-T11、EP-T12 | QA、REL | CI 对六引擎的实际签名制品、支持 OS/CPU/ABI、安装/启动/conformance/性能门禁逐格执行；skip、模拟 driver 或未构建组合记未认证，不汇总成全绿 |
| EP-C72 | L3 | EP-T15b | EP-T15a、EP-T14a | QA、OPS | 客户现场按独立清单重新核验介质、许可、拓扑、权限、数据隔离、负载、升级/回退与六引擎证据；预生产 C48 不能替代现场签字，未获授权项目保持未执行 |
| EP-C73 | L1/L2/L3 | EP-T03 | EP-T14b | QA、REL | 父分组：self-payload 比两个 clean builder 的规范化摘要；third-party-integrity 核来源/原始 hash/SBOM/许可/签名状态、不强求厂商源码重现；incident-rebuild 保留失陷冻结吊销与干净重建。分类穷尽互斥，整包完整性覆盖全部字节。 |
| EP-C74 | L1/L2 | EP-T05a | EP-T05 | QA、CORE | 无可靠历史 Provider/worker/参数摘要证据的旧待执行任务与审批冻结，不猜填当前版本；必须重新 prepare、授权和审批，历史完成结果仅由 legacy decoder 按已有字段回读 |
| EP-C75 | L1/L2 | EP-T13a | EP-T02c、EP-T04 | QA、OPS | 首装原生候选包缺失、验签/启动/selftest 失败时对应引擎准确 `unavailable`，无主进程静默 fallback，整体安装不得假通过；本条 host 故障负例不等待 T02c 最终四包业务接管，后续由 C01/C47 复验 |
| EP-C76 | L1/L2 | EP-T05 | EP-T11、EP-T13a | QA、CORE | 普通业务 CLI 通过平台登录、业务权限和普通任务 API 执行 submit/read-result，不要求本机管理 root/管理员权限，也不能调用包管理 IPC；M1 真实 DM 闭环由 C41–C43 使用该入口 |
| EP-C77 | L1/L2 | EP-T05 | EP-T01、EP-T05a、EP-T13b | QA、CORE | decoder 对篡改 digest、Schema Registry 冲突、未知 mandatory、超大/递归结果安全失败并保留原证据；解码过程不访问 DB、不执行 Provider；Schema 保留周期覆盖结果/审计，退役前完成受控迁移或导出，T13b 仅下游复验卸载态 |
| EP-C78 | L1/L2 | EP-T05b | EP-T14a、EP-T05a、EP-T03 | QA、REL | 仅在 E06 盘点为 `no_qualified_pminus1` 时构建桥接基线 B：现有业务/API/数据不漂移，不含未来 SDK、DM、Router 或热更新实现；以维护式安装完成回归后在候选测试前冻结不可变制品，冻结后不得修补；QA/REL 资格交 T14a 登记，未获资格时 T05a/T09 保持 not_ready |
| EP-C79 | L3 | EP-T06a-real | EP-T06a-core、EP-T11 | QA、ENG | 真实 DM 在取消/终止/候选 readiness 前后、generation CAS 前后故障时验证连接清理、回切、pinned 版本及持久回执；服务端会话/已发生效果无法确认进入对账，不盲重试。 |
| EP-C80 | L2 | EP-T04 | EP-T03、EP-T14a | QA、OPS | 按实际目标 OS/ABI 与已签 profile 测单 Provider 冷/热启动、RSS/子进程/连接/句柄/退出和排空，calibration 不计正式通过；冻结后正式签 single-provider-capacity-v0，fake 数据不认证真实驱动。 |
| EP-C81 | L2 | EP-T06a-core | EP-T04、EP-T05 | QA、OPS | 在签名 fake 的 V/V+1 并存真实进程下测双 generation 峰值/队列/连接/句柄、连续切流后泄漏与旧任务排空，输出精确 dual-generation-capacity-v0。 |
| EP-C82 | L1/L2/L3 | EP-T08 | EP-T05a、EP-T07、EP-T09 | QA、OPS | 启动器在 managed-hot 调用唯一有效 migrator；并发 API/Worker 不 DDL。校验 revision/checksum/回执，部分 MySQL 非事务 DDL 未核对或缺回执拒接流；hot 回退不 down/自动 restore，共享状态保留。 |
| EP-C83 | L1/L2/L3 | EP-T03 | EP-T14b、EP-T13a | QA、REL、OPS、SEC | 父分组：rehearsal 以测试根验根隔离、职责和带外恢复，仅签 test-trust-ready；production-handoff 必须有真实 REL/OPS/独立 SEC 公开交接证据并满足 E07，演练不能代签生产交接。 |
| EP-C84 | L3 | EP-T14b | EP-T09、EP-T06a-core、EP-T06a-real、EP-T02c、EP-T12、EP-T06b | QA、OPS、REL | 父分组：按冻结六引擎 release_scope_manifest 生成真实 V/V+1 组合叶实例，逐格绑定同 profile 的 C67 容量输出并验证两种顺序、第二轴失败和回退；test/production 仅为满足完整前置后的派生收据，父分组自身不执行、不计通过。 |

## 五个父分组的执行合同

| 父分组 | variant / 实例来源 | 主责与关闭方式 |
| --- | --- | --- |
| C70 | runtime / core / pminus1 / dm / kingbase / native；native 再按四产品、OS/CPU/驱动等分 cell | T14a；每个叶实例的 state/evidence/acceptance 独立，发布范围由 REL/T14b 先冻结，父状态不给消费者放行 |
| C73 | self-payload / third-party-integrity / incident-rebuild | T03；分别验自研可重复、第三方来源/hash/SBOM/许可、失陷冻结吊销/干净重建，保留 C58 根门限泄露演练 |
| C67 | release_scope_manifest.required_capacity_profiles | T14b；每个实际 profile 输出 qualified-capacity-profile，C84 精确消费通过实例 |
| C83 | rehearsal / production-handoff | T03；前者只签测试信任，后者必须 E07 实际 OPS/独立 SEC 交接，未提供时 waiting_external |
| C84 | 六引擎 real-combination 叶实例及派生 test/prod 汇总收据 | T14b；每真实格精确消费 V/V+1 pair、C67 与多节点证据，汇总须复核完整前置，不凭单条 passed 收据放行 |

叶执行键：`(case_id, variant_id, cell_id, requirement_context_digest)`。父计划对象可 planned，但禁止 executed=true、passed 或从父复制所有子项通过。每个实例必须有自己的证据引用和足够联合验收角色；cell/context 不符、未绑定 ref 或重复执行键拒绝。C73 的 passed/executed 叶必须内嵌 oracle_receipt 并通过对应 machine oracle，只有 generic evidence/signoff 不足；C84 的 test-matrix/production-envelope 派生记录仅可进入 output_instances，不能放 test_instances 或记 executed=true。

## 新增案例与既有覆盖的迁移

- C21/C23 为 core 的签名 fake L1/L2；C22/C79 为真实 DM L3，审核源码/self payload 实质不同，不能只改版本号或签名外层。
- C39 仅 catalog/fake/实际 Host L1/L2；真实引擎浏览器完整链仍由 C48 关闭。C46 保持金仓首包，V/V+1 pair 给 C84，不暗加 UI/core 依赖。
- C80＝单 Provider v0（T04）；C81＝双 generation v0（core）；C82＝启动迁移与平台预算（T08）；C83＝信任交接实例；C84＝六引擎真实组合矩阵（T14b）。
- C66 为单机组合 L2，不关闭 C84；C84 在同 profile 的 C67 通过后执行，按 engine/OS/CPU/driver ABI/payload/load/context 精确匹配，同时消费 T06b 多节点证据。
- T09 不等待 T14b，C27 使用三份匹配 v0。C38 增迁移获锁/部分 DDL/回执/ready 的故障点，未知部分结构不盲重放，hot 不 down/自动 restore。

## 记录要求

每实例记录源码/平台/Provider/runtime/驱动摘要、SDK/Schema、generation、环境 cell/context、输入/动作/预期/实际、证据和签字。未实施的 runner/CLI、未认证的 OS/驱动和未授权现场保持 planned/not_run/waiting_external，不得以假数据、一次 health 200 或 fixture 检查计为业务通过。实施步骤见 [test-procedures.md](test-procedures.md)。
