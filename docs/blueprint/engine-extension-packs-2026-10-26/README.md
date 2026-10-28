# 原生四引擎、Engine SDK 与独立热更新

版本：0.3，日期：2026-10-26。S1 文档基线：24 项任务、109 个工作包、84 个案例条目；5 个条目是父分组，实际执行实例按 variant/cell/context 展开。追踪格式 v3。

> **本文件是设计与计划基线，不是实现现状。**
> S1 写下时"业务实现、制品和真实验收仍全部待执行"；代码此后已推进，该句已不适用。
> 当前到了哪一层、凭什么判定、还差什么，一律以
> [引擎运行时与热升级：实现现状](../engine-runtime-status.md) 为准。


## 1. 用户确认的范围

- Oracle、MySQL、PostgreSQL、SQL Server 原生预装，首次安装即可识别和使用已支持能力。
- 其他数据库通过公开、版本化 Engine SDK 和统一接入流程扩展；首批为达梦 DM、人大金仓 KingbaseES。
- “引擎更新”指 TraceMind 的适配器及驱动扩展包。
- 平台程序与引擎适配器独立版本、独立发布、独立回退；两者必须提供业务不中断的热更新路径。
- 现有客户入口、专业页面布局、实例 ID、能力 ID、权限及任务回执继续兼容。

## 2. 既有源码盘点依据（S1 不改业务代码）

| 事实 | 当前源码依据 |
| --- | --- |
| ClusterGuard 的四引擎仍在主程序中构造注册 | [runtime.go](../../../../clusterguard-ha/internal/runtime/runtime.go)、[Adapter 接口](../../../../clusterguard-ha/pkg/adapter/adapter.go) |
| ClusterGuard 可借鉴 Adapter、能力检查和签名发布，尚无独立引擎包加载器 | [Registry](../../../../clusterguard-ha/pkg/adapter/registry.go)、[引擎枚举](../../../../clusterguard-ha/pkg/model/model.go) |
| 本项目连接/API/能力/前端仍有四引擎静态分支 | [连接分发](../../../backend/app/services/instances.py)、[实例 Schema](../../../backend/app/schemas/db_instance.py)、[能力声明](../../../backend/app/services/capability_declarations.py)、[连接表单](../../../frontend/src/InstanceDrawer.jsx) |
| DomesticDB 是历史展示归类，不是真实驱动身份 | [CMDB 规范层](../../../backend/app/cmdb/normalized_service.py) |
| 已有签名发布、不可变版本、迁移、检查点和恢复基础 | [ReleaseManager](../../../backend/app/runtime/release_manager.py)、[发布生命周期](../../operations/release-lifecycle.md) |
| 当前入口未具备蓝绿切流，Web 发布中心刻意只读 | [启动器](../../../run.py)、[冻结程序启动器](../../../backend/app/runtime/exe_launcher.py)、[发布中心](../../../backend/app/api/v1/release_center.py) |

本设计不把现有“原子更新版本记录、重启后 readiness 成功”认定为无中断热更新。S1 只应用计划/合同/案例和检查器升级；没有开发 SDK、生成安装包或改动运行服务。0.2 原件已按字节快照保留。

## 3. 目标分层

```text
客户需求入口 / 原有专业页面
             │
稳定入口 Router ── 平台 API A / API B（平台蓝绿版本）
             │
平台内核：权限、资产范围、补参、审批、持久任务、审计、报告
             │
版本化任务路由 / Scheduler / 平台执行 Worker
             │
Engine Host + Engine Registry + SDK Contract
             │
Adapter Supervisor
   ├─ 预装 Oracle / MySQL / PostgreSQL / SQL Server Provider
   ├─ DM Provider V / V+1
   └─ KingbaseES Provider V / V+1
             │
各 Provider 的驱动、native client、方言、采集与验证实现
```

“原生”表示内置目录、默认预装及产品保证的接入体验；为满足独立热更新，四个适配实现也使用同一种独立签名 Provider 包。用户不需要另外安装这四个包。原生 engine ID 和其更新发布者受保护，普通第三方包不能冒名覆盖。T02b 做源码抽取/行为对照，T02c 做签名制品运行接管/逐个独立更新；两者分别验收。

本版采用的取舍与外部待办见 [决策表](decisions.md)：四原生预装独立包，平台相邻一个合格版本回退，UDS/本机命名管道双向身份，国产库真实版本/介质许可前置，平台先单机蓝绿，预生产与客户现场分开。原生更新失败回切合格旧包，首装失败准确不可用；不另维护主程序只读降级实现。

## 4. 需求与完成判据

| ID | 需求 | 完成判据 |
| --- | --- | --- |
| EP-R01 | 原生四引擎兼容 | 首装可用；旧实例/能力/路由/任务身份保持 |
| EP-R02 | 标准 Engine SDK | 第三方按同一模板实现 Provider，无需为标准能力修改平台业务代码 |
| EP-R03 | DM / KingbaseES 独立接入 | 分别识别为 dm / kingbasees，独立驱动和能力矩阵，不借协议兼容冒充 Oracle/PG |
| EP-R04 | 专用签名扩展包 | 独立清单、信任用途、兼容性、自检、安装事务和审计 |
| EP-R05 | 引擎包热更新 | 新请求切新 Provider，旧任务留原摘要版本，其他引擎不受影响 |
| EP-R06 | 平台热更新 | 新旧平台并行，稳定地址切流；旧请求、下载和任务可完成 |
| EP-R07 | 版本及状态兼容 | 核心/SDK/任务/结果/元数据/驱动矩阵预检；不兼容拒绝切流 |
| EP-R08 | 恢复与回退 | 每阶段可回读、可恢复；禁止重复副作用和静默换任务版本 |
| EP-R09 | 宿主权限与凭据边界 | 适配包不扩权；每个执行阶段再鉴权和校验范围 |
| EP-R10 | AI 与手工共用能力 | 有模型按已登记能力调用；无模型只进入专业页，导航零执行 |
| EP-R11 | 跨平台离线交付 | 各 OS/CPU/运行时明确声明，实际驱动加载测试通过，安装不依赖互联网 |
| EP-R12 | 完整验收与可观测性 | 同一数据贯穿驱动、身份、能力、持久结果、API、浏览器及升级演练 |

## 5. 热更新的可测定义

本设计中的热更新以业务连续性验收，允许后台启动新进程和逐步退出旧进程。

- 外部服务地址不变，更新期间继续接收新请求。
- 在途 HTTP 请求和下载保留旧处理进程；更新不产生连接重置或升级引发的 5xx。
- 排队、执行中、待对账任务不丢失，已发生副作用不因更新重复执行。
- 新任务使用新 generation；旧任务、重试、恢复和补偿使用原先固定的版本身份。
- 发布成功须同时证明运行版本、路由版本、任务路由和存储合同一致。
- 不兼容版本必须拒绝激活；不能自动退化为停止旧服务再启动新服务，并仍报告“热更新成功”。

平台首发热更新采用单机业务 A/B，固定 Router 不变、资源足够并行、元数据库兼容真实未经修改的前一版是前提。Provider core 用签名 fake 的 L1/L2 单独验收；DM real、各原生/金仓和 T06b 多执行节点分别验收，不进入 T09 单机硬依赖。Router/Supervisor 本身也需要更新时，必须具备经过验收的监听接管或冗余入口；没有这些条件时该发布不得标记热更新就绪。主机/操作系统维护需要冗余节点，不能由单机业务蓝绿外推。

平台回退窗口固定为当前 P / 上一合格 P-1；窗口内禁止破坏性 contract。回退只退代码/路由，保留新增共享表/字段/记录。任务版本留存和 SDK S/S-1 协议兼容另有独立轴，不能都当成“切最新版”。迁移和回填由 T05a/CORE 独占，T09 对其有硬依赖。

## 6. 首批国产引擎的具体边界

| 引擎 | canonical ID | 驱动候选 | 必须冻结的现场参数 |
| --- | --- | --- | --- |
| 达梦 | dm | dmPython 与对应 DPI/native client | 服务端完整版本、兼容模式、字符集、权限、OS/CPU、驱动和 Python ABI |
| 人大金仓 | kingbasees | ksycopg2 与对应 libkci/native client | 服务端完整版本、兼容模式、字符集、权限、OS/CPU、驱动和 Python ABI |

现有开发 Python 为 3.12.14；不能由此推定两家驱动均支持该 ABI。Provider 可以使用自己已验收的独立运行时，通过统一 RPC 与平台通信。正式制品只声明实际通过的组合。

M1-DM 独立以一个真实 DM 只读能力贯通离线安装、强身份连接、执行、持久结果、CLI/API 回读，再逐项认证拓扑、指标、会话/锁、只读巡检和报告结果读取。该闭环不依赖后续 UI/AI 或热更新。写操作、备份恢复、启停及切换必须分别实现 prepare/execute/verify 并走平台审批；缺实现即明确 unsupported。

首个国产工程组合暂取 Linux x86_64 候选，不能写成实际已支持；具体厂商版本、模式、驱动/客户端、使用及再分发许可由 T14a 分格冻结。现有四引擎已承诺的 OS/CPU 组合先盘点并保持，其他新增组合逐格认证，skip 不算通过。

## 7. 当前基线、批次与检查

S1 已将任务/合同/案例/两份 JSON 与检查器同步为 0.3。T06a 历史 ID 不可调度；当前任务为 T06a-core / T06a-real。M1-base 的输出可进入 core/单机路径，不要求真实 DM 或生产根交接完成。

实现进度与上述**计划**分开记录：见 [实现现状](../engine-runtime-status.md)（含可复现命令与逐层判定口径）。

| 批次/出口 | 进入及退出边界 |
| --- | --- |
| M0 | SDK/Registry/原生源码基线；T14a 只需 plan 输出，不等整个环境任务 |
| M1-base | T04/T05a/T05/T13a + T03[test-trust-ready]；P-1 缺失按条件 T05b 准备 |
| M1-DM | T11 与精确 dm-ready 独立关闭，不反向卡 M1-base |
| M2-core | T06a-core/T07/T13b；签名 fake 的真实进程/IPC 只计 L1/L2 |
| M2-real | DM/四原生/金仓/多执行节点各走自身输入；互不代签 |
| M3-single | T08/T09；不等 T06b、DM、金仓或 E07，须匹配三份 v0 与迁移回执 |
| final-matrix | T14b 在 run 前冻结 required cells；C67 profile → C84 真实组合矩阵 |
| 预生产/现场 | T15a 消费 test 矩阵；T15b 在正式生产封装就绪后做独立现场验收 |

- [SDK、进程、迁移、包与热更新合同](contracts.md)
- [决策与 E01–E07 外部输入](decisions.md)
- [24 项任务的工作包、依赖与输出](development-tasks.md)
- [案例条目与父分组/variant](test-cases.md)
- [执行步骤及 S1 检查器验证方法](test-procedures.md)
- [v3 机器追踪表](traceability.json) / [唯一案例主责](test-case-ownership.json)
- [0.2→0.3 变更依据：S1 已应用，S2–S4 待实施](change-plan-0.3.md)
- [审核处理记录](review-resolution.md)
- [交给 dsh 的连续开发批次 01：补漏、运行时、单节点更新](dsh-core-batch-01.md)（开发任务，尚未实现/验收；覆盖与正式门禁分开）
- [连续批次 01 续做：当前代码缺陷与 D01–D08 完整收尾](dsh-core-batch-01-continuation.md)（部分实现后独立审核；104项回归不代表整批通过）
- [S2-2 独立审核：87项回归通过仍有5项P1](../../../reports/engine-extension-s2-20261026/s2-2-codex-review.md)
- [0.2 原件与逐文件摘要快照](../../../reports/engine-extension-s1-20261026/snapshot-manifest.json)
- [S1 检查/独立复核证据目录](../../../reports/engine-extension-s1-20261026/)

从项目根目录运行：

```bash
python3 docs/blueprint/engine-extension-packs-2026-10-26/validate_plan.py --self-test --report reports/engine-extension-s1-20261026/validation.json
```

该命令检查实际 v3 文件并运行规划数据的合成正反例；不运行 Provider、构建、迁移、数据库或密钥操作。`test_instances` / `output_instances` 当前全空，合成 fixture 单独存放，不会写成业务通过记录。S2/S3/S4 仍须按真实制品和环境逐项验收。

## 8. 官方资料

- [JSON-RPC 2.0](https://www.jsonrpc.org/specification)：作为拟选的版本化进程通信基础；业务方法、鉴权与任务语义由 SDK 另行约束。
- [Python venv](https://docs.python.org/3/library/venv.html)：虚拟环境应在目标位置重建，不能直接复制开发机 venv 作为跨平台制品。
- [pip 离线安装](https://pip.pypa.io/en/stable/cli/pip_install/)：扩展包采用完整离线依赖与摘要校验。
- [达梦 Python 接口](https://eco.dameng.com/document/dm/zh-cn/start/python-development.html)：dmPython 与 DM 驱动环境。
- [KingbaseES Python 接口](https://help.kingbase.com.cn/v8/development/client-interfaces/python/python-1.html)：ksycopg2、libkci 及版本/平台适配要求；实际支持以选定发行版驱动验证为准。
