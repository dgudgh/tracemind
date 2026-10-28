# 引擎运行时与热升级：实现现状（2026-10-27）

本文件是**当前实现状态**的唯一载体，随代码更新。设计与计划见
[引擎扩展包 0.3](engine-extension-packs-2026-10-26/README.md)；逐条问题与归宿见
[已知问题台账](../known-issues.md)（本条线为 ISS-72、ISS-74，本文件**不复制**清单内容）。

> 为什么要单独一份：`engine-extension-packs-2026-10-26/` 是 10-26 的**设计基线**，
> 其 README 写的是"业务实现、制品和真实验收仍全部待执行"。代码此后已推进过，
> 那句話不能再当作现状读。设计文档讲"要做什么"，本文件讲"现在到了哪、凭什么这么判"。

## 1. 一句话结论

**分层骨架已建成，热更新主干断在宿主生命周期与 Provider 能力两处。**
信任、契约、归档、注册表、主机侧 CLI 都有实现且测试通过；
`State` / `CrashPolicy` 已真正接进 Supervisor（此前是零引用的死代码）；
但**有界队列仍是线程伪装、Provider 能力面落后于契约**，所以"崩溃→重启→熔断→回退"整链还不能验收。

## 2. 代码分层（实际路径，非设想）

| 层 | 位置 | 规模 |
| --- | --- | --- |
| SDK 契约与进程通信 | [engine_sdk](../../backend/app/runtime/engine_sdk/provider.py)（provider/wire/contracts/broker_client/profiles） | ~950 行 |
| 宿主监管 | [engine_host](../../backend/app/runtime/engine_host/supervisor.py)（supervisor/store/orchestrator/metrics/update/ownership/lifecycle） | ~4100 行 |
| 引擎包与信任 | [engine_packs](../../backend/app/runtime/engine_packs/trust.py)（trust/contract/archive/repository/management/trust_metadata/production_admission） | ~3600 行 |
| 平台热更新与路由 | [runtime](../../backend/app/runtime/platform_update.py)（platform_update/platform_router/engine_cluster/secret_broker） | ~1900 行 |
| 引擎身份注册表 | [engine_registry](../../backend/app/engine_registry/__init__.py) | 109 行 |
| Provider 源码模板 | [engine_providers](../../backend/app/engine_providers/base.py)（6 引擎 + conformance） | ~250 行 |

## 3. 逐层状态

判定口径见第 5 节。**"通过"只表示现有用例通过，不等于生产可用。**

| 层 | 状态 | 证据 |
| --- | --- | --- |
| 包签名与信任 | **可用** | [trust.py](../../backend/app/runtime/engine_packs/trust.py)：`_validate_trusted_key` 从 `public_key.public_bytes_raw()` 派生并比对（`trust_key_raw_mismatch`）；`assert_no_cross_domain_key_reuse` 按真实 raw 比较；`parse_manifest_json` 严格 JSON。`test_engine_pack_trust.py` 通过 |
| 包契约与字节 oracle | **可用** | [contract.py](../../backend/app/runtime/engine_packs/contract.py)：`validate_c73_receipt(receipt, *, build_roots=...)` 已接构建根；`_validate_sbom`、`_actual_matches_block`、`_lstat_regular`/`_is_reparse` 拒绝目录链接。`test_engine_pack_contract.py` 通过 |
| 归档 / 仓库 / 注册表 | **可用** | `archive.py`、`repository.py`；`Catalog` 有 generation、CAS、`NATIVE_IDS` 发布者保护 |
| 引擎只读目录 API | **已挂载，但数据是静态占位** | [engine_catalog.py](../../backend/app/api/v1/engine_catalog.py) 已注册进 `registry.py`（新进程实测 `/api/v1/engine-catalog` 在位）；但唯一安装发布者的 `wire_engine_business_runtime` **零调用者**，故恒走 `Catalog.empty()`：6 项（含原生四引擎）全 `available=False / not_installed` |
| 主机侧生命周期入口 | **可用（CLI）** | [engine_pack_manager.py](../../scripts/engine_pack_manager.py) `--mode {lab,production}` 必填；production 需准入回调，缺则 `production_management_admission_missing`；**生产模式只放行 `status/disable/uninstall/gc`** |
| 宿主生命周期 | **部分** | [supervisor.py](../../backend/app/runtime/engine_host/supervisor.py) 已消费 [lifecycle.py](../../backend/app/runtime/engine_host/lifecycle.py) 的 `State`/`CrashPolicy`：状态机、`events`、崩溃退避重启、6 次熔断→QUARANTINED、`manual_reset`、`drain`、`restart`（复用 staging）、`cleanup_report`、启动前兼容性校验、staging 落盘后字节复核 |
| 有界队列与取消 | **未成立** | `submit` 仍是"每次提交起一个线程"，不是 `max_inflight` worker + pending 队列；`max_queue`、`queued_deadline_exceeded`、运行中协作取消均未实现 |
| Provider 能力面 | **未达契约** | [fake_provider.py](../../backend/tests/fixtures/engine_providers/fake_provider.py) 不支持 `output_bytes`/`return_envelope`/`optional_fields`/`reply_delay`/`ignore_cancel`/`spawn_child`/`cancel`，且不回声 `context` |
| 六个 Provider 源码一致性 | **通过（不含服务端认证）** | `conformance.check` 已对 `PROFILES` 全引擎实跑；`server_certified` 一律 `False` |

## 3.1 平台里看不到的两个入口（用户实际观察，2026-10-27）

现象：**"平台管理"下没有热升级，也没有"添加存储引擎"。** 这是三件事叠在一起，性质不同：

| 观察 | 性质 | 依据 |
| --- | --- | --- |
| 没有"热升级"菜单 | **有意边界** | `menuConfig.jsx` 的"平台管理"只有集成中心与系统管理两块；包生命周期按设计走主机侧 CLI（`engine_pack_manager --mode production`）。不是漏做 |
| 没有"添加存储引擎" | **动作本身不存在** | 引擎是签名包，不是平台里填表单新增的实体；能新增的是**实例**。扩展引擎本应出现在"新增实例 → 数据库类型"下拉里，但为**禁用态**，标签写明「`engine_id` · 原因 · 连接字段未接入」 |
| 连禁用的都看不到 | **后端进程过旧** | 运行中的 8800 对 `/api/v1/engine-catalog` 返回 404；而同一份代码在**新进程**里该路由已挂载（应用共 1001 条路由，含它）。Python 不热重载，改过代码必须重启后端 |

即使重启，下拉里出现的也是 `dm · not_installed`、`kingbasees · not_installed` 这类**占位状态**，
因为发布者未安装（见上表）。**"能看到"与"能用"是两件事**，不要把它们混为一谈。

## 3.2 OSS / 企业版可插拔边界（2026-10-27 建立）

目标形态：**开源仓库 = 平台 + 基础功能 + 引擎运行时协议（空实现）；企业版仓库 = 引擎运行时实现 + 签名引擎包。**
本地一份工作区，企业版目录以 `.gitignore` 排除；未安装企业版时自动降级。

已落地的唯一状态源：[engine_runtime.py](../../backend/app/services/engine_runtime.py)

| 内容 | 说明 |
| --- | --- |
| `EngineRuntime` Protocol | 业务侧只依赖这个面（freeze/check/invoke/retain_plan/release_completed/save_binding/check_binding/save_result） |
| `NullEngineRuntime` | 未安装企业版时的空实现，所有方法抛 `EngineRuntimeUnavailable`，**不返回伪造结果** |
| `install()` / `runtime()` / `available()` | 企业版启动时 `install()` 换入真实现；业务侧 `runtime()` 永远拿到对象，无需判空 |
| `READ_OPERATIONS` | 能力声明唯一来源；`engine_execution` 从其导入并对外重新导出，既有 import 路径不变 |

为什么不用 entry_points：本项目无 `pyproject.toml`/`setup.py`，源码直跑 + PyInstaller 单文件打包，
pip 的插件发现机制不适用，改用**显式注册 + 目录约定**。

**验收**：`test_engine_runtime_protocol.py` 14 项（含"未安装必须报类型化错误"与"bridge() 仍返回 None"两条兼容断言）。
全量引擎套件 **35 failed / 345 passed → 30 failed / 370 passed**（10-27 上午）→ **12 failed / 367 passed**（10-27 晚：宿主调度、取消、崩溃恢复主干闭环，`test_engine_host_dispatch` 24/24、`test_engine_host_security` 12/12）。验收标准与测试案例见 [hot-upgrade-acceptance.md](hot-upgrade-acceptance.md)。

### 3.2.1 物理拆分边界（import 级实测，2026-10-27）

用 import 钩子屏蔽候选目录，观察 `app.main` 能否完整导入（1001 条路由）。结果：

| 屏蔽范围 | 结果 | 含义 |
| --- | --- | --- |
| `runtime/engine_host` + `runtime/engine_packs`（7,739 行） | **导入成功，且零模块请求** | 完全隔离，可原样搬进企业版仓库，**无需改一行代码** |
| 再加 `runtime/engine_sdk` | 失败于 `engine_sdk.contracts` | 被 `services/engine_execution` 依赖 |
| 再加 `engine_registry` | 失败于 `engine_registry` | 被 `engine_execution` / `api/v1/engine_catalog` 依赖 |

由此确定归属（**推翻早先"8 个文件 20 个调用点"的估计**，那个数字把 `engine_execution`
误算成了必须搬走的部分）：

| 归属 | 内容 | 依据 |
| --- | --- | --- |
| **企业版** | `engine_host/`、`engine_packs/`、`engine_cluster.py`、`platform_update.py`、`platform_router.py`、`engine_catalog_publisher.py`、`engine_execution_transport.py`、主机侧 CLI（`engine_pack_manager`/`tmengine`/`engine_trust_cli`/`platform_router*`） | 装包、验签、宿主监管、热切换 |
| **开源版保留** | `engine_sdk/`（955 行）、`engine_registry/`（109）、`engine_providers/`（249）、`models/engine_execution.py` + 建表迁移、`services/engine_execution.py`（280，`bridge()` 无实现时恒为 None）、只读 `engine_catalog` API + 页面 | 契约与通信是开放 SDK 的前提；数据结构与迁移不能分叉；`model` 留空表无害 |

**净效果**：企业版约 9,700 行（占后端 6.5%），开源版**零 import 改动**即可运行 ——
因为 `engine_execution.bridge()` 拿不到实现时本来就返回 `None`，下游守卫自动降级。

剩下的真实工作量只有三件：**迁移保留空表**、**测试按归属分家**、**前端靠目录状态呈现"未安装"**。

**运行级实证**（不只是 import）：用启动钩子屏蔽 `engine_host`/`engine_packs` 模拟"企业版不存在"，
再跑依赖 `engine_execution` 的那条业务链：

```
-p "(capability or assistant or plan_service or plan_queue)"  →  801 passed, 4 failed, 11 skipped
```

那 4 项（`test_assistant_body_limit.py`）在**完整环境**下同样失败、结果逐条一致，
属既有缺陷，**不是拆分引入**。屏蔽脚本见 `runtime-backups/oss_sim/sitecustomize.py`（gitignored，可复用）。

注意：pytest 的 `-k` 只过滤用例名，仍会 import 测试模块；企业版测试模块 import 即失败，
因此必须用 `--ignore` 在收集阶段排除，否则整轮 collection 会被打断。

**真机运行验证**（2026-10-27）：以"企业版不存在"的形态实际启动服务。

```bash
PYTHONPATH=runtime-backups/oss_sim:backend \
DATABASE_URL="sqlite:///./oss-demo.db" DB_TYPE=sqlite \
python -m uvicorn app.main:app --port 8899
```

结果：启动成功，**22 个迁移全部应用（含 `20261027_01_engine_execution`）**、
能力注册表同步 79 条、默认告警规则 107 条、调度器与嵌入 worker 就绪、`Application startup complete`。

| 验证项 | 结果 |
| --- | --- |
| `/health`（公开） | 200 |
| `/api/v1/engine-catalog` | 200，6 个引擎，全部 `available=false / not_installed`（占位，符合设计） |
| `/api/v1/engine-catalog/drivers` | 200，四原生引擎驱动可导入；`dm`/`kingbasees` 缺失 |
| `/instances`、`/system/dashboard-stats`、`/operations/capability-matrix`、`/inspection/tasks` | 全部 200 |
| `/api/v1/engine-catalog` 无 token 时 | 401（不是 404）—— 证明企业版缺失时路由依然挂载 |

驱动探测的一个实际收益：`postgresql` 声明驱动是 `psycopg2`（本机未装），
但候选集包含 `psycopg`(v3) 且连接器确实支持回退，因此判定为"可导入"。
若按单一声明判断会误报"缺失"。

## 3.3 版本能力边界：开源版 = 基础模块 + 四引擎运维

开关 `TRACEMIND_EDITION`（`enterprise` 默认 / `oss`），实现见
[edition.py](../../backend/app/core/edition.py)。**默认不限制**，现有部署行为不变。

三层拦截：菜单（`module_config.list_modules` 按 `enabled=0` 下发 → 前端自动隐藏）、
接口（`AuthMiddleware` 内 403，**对 admin 同样生效**）、写操作（开源版不允许打开付费模块）。

| | 数量 | 内容 |
| --- | --- | --- |
| 企业版模块 | 29 | `perf.*`(15) / `backup.*`(6) / `rescue.*`(7) / `protection.*`(1) |
| 开源版模块 | 45 | 资产、工作台、智能诊断、监控告警、巡检、报告中心、平台管理、**`ops.*`(11)** |
| 企业版引擎 | 2 | 达梦 / 金仓（含其运维 `/operations/{dm,kingbasees}/*`） |
| 开源版引擎 | 4 | Oracle / MySQL / PostgreSQL / SQL Server（**全部运维端点**） |

**完整对比（逐模块清单、接口边界、代码归属、交付状态、变更方式）见
[edition-boundary.md](edition-boundary.md)——那是版本划分的唯一权威载体，本节只留摘要。**

一句话：边界已生效，但企业版真正的货只有"性能分析与数据保护在开源版被挡住"；
热升级与达梦/金仓仍跑不通（ISS-71 / 72 / 74 / 75）。

## 4. 实测数字（可复现）

```bash
PYTHONPATH=$PWD/backend .venv-mac-primary/bin/python -m pytest -q backend/tests/ \
  -k "engine_host or engine_pack or engine_provider or engine_management or engine_sdk \
      or engine_registry or engine_catalog or engine_trust or engine_secret or engine_task \
      or engine_cluster or platform_router or platform_update or process_roles" \
  --tb=no -p no:randomly
```

| 时点 | 结果 |
| --- | --- |
| 接线前（10-27 上午） | **46 failed / 334 passed** |
| 接线后（同日） | **35 failed / 345 passed** |
| 调度/取消主干闭环（10-27 晚） | **12 failed / 367 passed** |

剩余失败分布：

| 模块 | 数量 | 主因 |
| --- | --- | --- |
| `test_engine_host_dispatch.py` | 14 | 队列/取消语义、Provider 能力面 |
| `test_engine_management_service.py` | 8 | LabManagement 侧流程 |
| `test_engine_provider_rpc.py` | 4 | `ProviderSupervisor` 与基类的启动契约未统一 |
| `test_engine_host_security.py` | 3 | ownership / 私有 workdir 拒绝路径 |
| `test_engine_cluster_process.py` | 3 | 多进程集群形态 |
| 其余各 1 | 3 | 平台 AB 路由、包仓库、catalog 视图 |

**根因聚类**：动前 46 项里 22 项是同一个
`TypeError: FixtureSupervisor.__init__() got an unexpected keyword argument 'restart'`；
其余是被它遮蔽的下游断言。因此"46 个缺陷"的说法是错的，实际是**一条断链**。

## 5. 判断口径（这几条决定了"能不能说完成"）

1. **源码一致性 ≠ 服务端认证。** `conformance.check` 明确返回 `server_certified: False`；
   任何 provider 只有在目标数据库上真跑通过才能翻 True，否则一律按未认证表述。
2. **lab 绿灯 ≠ 生产热更新。** `LabManagement(mode="lab")` 用合成 fixture；
   热更新验收必须走 `platform_router` + `engine_pack_manager --mode production`。
3. **测试变绿 ≠ 语义成立。** 判定"接线完成"要能指出**谁读它、怎么读**；
   仅把状态翻回 pending 或写一行历史不算闭环。
4. **`app/api/` 对 `engine_packs`/`engine_host` 零引用是有意边界**，
   不是漏接——包生命周期只在主机侧。看到"API 里没有"不要当成缺陷。
5. **未取得真机证据的能力不得写成"已认证"。** 达梦/金仓驱动（`dmPython`/`ksycopg2`）
   当前均未安装，也没有注册实例，因此这一条现在恒为"未认证"。

## 6. 剩余工作与顺序

| 顺序 | 事项 | 依赖 | 完成判据 |
| --- | --- | --- | --- |
| 1 | Provider 能力面：`context` 回声、`output_bytes`、`cancel` | 无 | dispatch 中"provider 侧不支持"类失败转绿 |
| 2 | 真队列：`max_inflight` worker + pending 队列 + 排队 deadline | 第 1 项（取消需要 provider 配合） | `max_queue`/`queued_deadline_exceeded` 成立 |
| 3 | 取消语义：排队直接撤销、运行中协作取消、已提交副作用报 `effect_state=completed` | 第 1、2 项 | 取消相关断言转绿且副作用不重复 |
| 4 | `ProviderSupervisor` 启动契约与基类统一（`_stage` 返回形态、`_spec`、`_prepare_launch_config`） | 第 1 项 | `test_engine_provider_rpc.py` 转绿 |
| 5 | LabManagement 8 项 | 独立 | `test_engine_management_service.py` 转绿 |
| 6 | 达梦 / 金仓真机认证 | 驱动 + 实例 | 逐条 SQL 实测记录，`server_certified` 才可翻 |

**顺序不能反**：先做 2 再做 1，会得到"队列测试绿了但取消是假的"；
先做 4 再做 1，会因为 Provider 不回声 `context` 而反复返工。

## 7. 边界说明

- 达梦 / 金仓**不在**原生四引擎内，它们是扩展引擎（`engine_id` 见 `engine_registry.KNOWN`），
  能力成熟度必须单独声明，不得继承原生引擎的 ready。业务能力面（巡检/容量/备份/操作路由）
  目前对两者均未实现，实例层已可创建与连通测试。
- 本文件描述源码状态；客户部署、真实外部系统与故障演练的验收状态**不在本文件范围内**。
