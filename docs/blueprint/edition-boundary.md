# 开源版与企业版差异对比（2026-10-27 实测）

> 本文是**版本划分的唯一权威载体**。代码改动涉及版本边界时同步更新本文。
> 引擎运行时实现现状见 [engine-runtime-status.md](engine-runtime-status.md)，缺陷编号见
> [known-issues.md](../known-issues.md)。
>
> 需要给人看的可视化版本：[edition-matrix.html](edition-matrix.html)（矩阵勾叉表，可打印）。
> 该视图由本文事实导出，两者不一致时以本文为准。

> **✅ 2026-10-27 17:10 边界已执行到代码**
> 最终划分：**开源 30 / 企业 43**（共 73 个模块）。能力矩阵已改为无页面，不计入模块表（见
> [engine-capabilities.md](engine-capabilities.md)）。已落到
> [edition.py](../../backend/app/core/edition.py) 与 `test_edition_boundary.py`（13 项，6 次变异全有效）。
> 逐项清单见 [edition-matrix.html](edition-matrix.html)（本文不重复清单）。
> 开源实例实测：`/api/v1/modules` 返回 31 项启用，菜单叶子 30 项（权限配置不在菜单）。

## 0. 一句话结论

**代码层面只差 6.3%（9,522 行 / 150,153 行），产品层面差 43 个模块和 2 个引擎；
但"名义差异"远大于"实际可交付差异"——企业版里真正能用的只有门禁隔离本身，
热升级与扩展引擎运维都还跑不通。**

## 1. 总览

| | 开源版（OSS） | 企业版（EE） |
| --- | --- | --- |
| 开关 | `TRACEMIND_EDITION=oss` | `TRACEMIND_EDITION` 未设 / `enterprise` |
| 模块 | **30 个** | **43 个** |
| 引擎 | Oracle / MySQL / PostgreSQL / SQL Server | 上述四个 + **达梦 / 金仓** |
| 引擎运维 | 四引擎全部运维端点 | 四引擎 + 扩展引擎运维 |
| 性能分析 | **5 项**（慢 SQL / 锁等待 / 性能基线 / 容量分析 / AWR 综合） | 其余 10 项 |
| 数据保护 | 无 | `backup.*` / `rescue.*` / `protection.*` 共 14 个模块 |
| 智能诊断 | **仅诊断工作台** | 工作台 + 状态与现场 / 事故中心 / 现场 Agent / 快照策略 |
| 巡检 | **4 项**（总览 / 一键 / 任务 / 报告） | 另 3 项（历史对比 / 策略 / 模板） |
| 监控告警 | 综合 / 容量 / 可观测总览 | 日志关联 / Grafana 模板 / APM 链路 |
| 集成中心 | 无 | Zabbix / Prometheus / 通知渠道 / 工单 |
| 数据库运维 | **7 项**（高可用 / 数据治理 / 归档对象存储 除外；能力矩阵已无页面） | 高可用 / 数据治理 / 归档对象存储 |
| 引擎热升级 | 无入口 | 主机侧 CLI（平台内无 UI） |
| 代码 | 约 93.7% | 约 6.3%（9,522 行） |

**重要**：默认不限制。未设置 `TRACEMIND_EDITION` 时按企业版处理，
现有部署行为不变——版本门禁是可选的，不是主链路硬依赖。

## 2. 模块差异（73 个，逐项）

### 2.1 企业版独有（43 个）

| 域 | 数量 | 说明 |
| --- | --- | --- |
| `perf.*` 性能分析 | 10 | 总览 / 引擎深度报告 / SQL 画像 / 索引分析 / 表画像 / 问题趋势 / 优化建议 / AWR 历史·对比·采集 |
| `backup.*` 备份恢复 | 6 | 总览 / 策略 / 方案 / 恢复方案 / 报告与告警 / 平台接入 |
| `rescue.*` 应急抢救 | 7 | 总览 / 案件与证据 / 工具授权 / 远程 Agent / 任务 / 报告 / 风险审批 |
| `diagnose.*` 智能诊断 | 4 | 状态与现场 / 事故中心 / 现场 Agent / 快照策略 |
| `integration.*` 集成中心 | 4 | Zabbix / Prometheus / 通知渠道 / 工单 |
| `inspect.*` 巡检高级 | 3 | 历史对比 / 巡检策略 / 巡检模板 |
| `monitor.observability.*` 可观测 | 3 | 日志关联 / Grafana 模板 / APM 链路 |
| `ops.*` 数据库运维 | 3 | 高可用 / 数据治理 / 归档对象存储 |
| `system.*` 系统管理 | 2 | 企业认证 / 诊断评测 |
| `protection.*` 数据保护 | 1 | 自动化审批 |

### 2.2 开源版可用（30 个）

| 域 | 数量 | 说明 |
| --- | --- | --- |
| `ops.*` 数据库运维 | 7 | 配置基线 / 运行概览 / 空间管理 / 日志管理 / 会话事务 / 对象统计 / 用户安全 |
| `inspect.*` 巡检 | 4 | 总览 / 一键巡检 / 任务 / 报告 |
| `perf.*` 性能分析 | 5 | 慢 SQL / 锁等待 / 性能基线 / 容量分析 / AWR 综合 |
| `monitor.*` 监控告警 | 3 | 综合告警 / 容量告警 / 可观测总览 |
| `system.*` 系统管理 | 4 | 用户管理 / 系统参数 / AI 配置 / 调度任务 |
| `report.*` 报告 | 2 | 统一报告 / 告警报告 |
| 其他 | 5 | 资产管理 / 工作台 / 诊断工作台 / 权限配置 / 引擎与版本 |

> 逐项清单以 [edition-matrix.html](edition-matrix.html) 为准（74 行逐条，含 `module_key`）。
> 本文只保留分组统计，避免两处维护同一份清单。

### 2.3 菜单实际渲染（`TRACEMIND_EDITION=oss`，CDP 抓 DOM）

一级分组 **7 组**：智能诊断 / 监控告警 / 巡检 / 性能分析 / 数据库运维 / 报告中心 / 平台管理
（另有独立入口「资产管理」「工作台」）。**数据保护整组消失**。

叶子项 **29 个**（30 个开源模块减 1——`moduleConfig` 权限配置不在菜单里，只在用户管理页内）：

| 菜单 | 剩余叶子 |
| --- | --- |
| 智能诊断 | 诊断工作台 |
| 监控告警 | 综合告警 / 容量告警 / 可观测总览 |
| 巡检 | 巡检总览 / 一键巡检 / 巡检任务 / 巡检报告 |
| 性能分析 | 慢 SQL 分析 / 锁等待分析 / 性能基线 / 容量分析 / AWR 综合 |
| 数据库运维 | 配置基线 / 运行概览 / 日志管理 / 会话事务 / 空间管理 / 对象统计 / 用户安全 |
| 报告中心 | 统一报告 / 告警报告 |
| 平台管理 | 用户管理 / 系统参数 / AI 配置 / 调度任务 / 引擎与版本 |

注意：**「性能分析」父菜单仍会显示**——因为 15 个叶子里有 5 个可见，
前端只在叶子全空时才隐藏父菜单。

## 3. 接口边界

`AuthMiddleware` 在鉴权之后、业务之前拦截。**对 admin 同样生效**——
这是产品版本能力，不是账号权限；权限层（`request_scope` / action policy）继续按角色判断，两者互不替代。

### 3.1 规则语言

`edition.py` 的 `PAID_ROUTE_RULES` 用**路径段元组**表达边界，从 `/api/v1/` 之后逐段比对：

| 记号 | 含义 |
| --- | --- |
| 字面段 | 必须相等，如 `"performance"` |
| `{}` | 任意**纯数字**段（实例 id 的位置） |
| `*` | 任意单段（引擎名等变量） |

命中任一规则即 403。这套语言是必要的——三个层次都要能表达：

| 层次 | 例子 | 为什么不能按一级前缀切 |
| --- | --- | --- |
| 一级整段 | `("backup-center",)` | 该前缀下全属企业版 |
| 前缀下子段 | `("performance", "overview")` | `baseline` 开源、`overview` 企业，同在前缀下 |
| 引擎内多级 | `("oracle", "{}", "awr", "compare")` | AWR 导出/上传开源，对比/采集企业 |
| 运维 section | `("operations", "*", "{}", "ha")` | `ha` 在 operations 的第 4 段 |

当前拦截项：数据保护 3 个前缀、事故现场 7 个前缀、集成中心 5 个前缀、
`identity` / `diagnosis-evaluation` / `data-governance` / `sql-profiles`、
性能分析 8 个子段、可观测 3 个子段、巡检 3 个子段、Oracle 12 个段、MySQL 5 个段、
operations 的 `ha` 段与 2 个扩展引擎。

### 3.1.1 智能诊断为什么能只留工作台

诊断工作台（`diagnose.smart`）由 `UnifiedDiagnosisPage` 包装 `DiagnosisCenterPage` +
`RuleDiagnosisPage`，只依赖四个前缀：`diagnosis`（规则与结果）、`diagnosis-center`
（工单/方法学/数据源/架构画像）、`assistant`、`ai`。

另外四个页面共享 `incidentSnapshotApi` + `stateInvestigationApi`，只依赖事故域前缀。
两组前缀**没有交集**，所以可以干净切分。唯一要小心的是同名子串：

| 路径 | 结果 | 原因 |
| --- | --- | --- |
| `/api/v1/diagnosis-center/incidents` | **放行** | 第一段是 `diagnosis-center`，是工作台的历史工单列表 |
| `/api/v1/incidents/7/probes` | 拦截 | 第一段是 `incidents`，属事故现场 |

按第一级路径段精确匹配即可区分；`test_incident_prefixes_are_only_used_by_the_enterprise_pages`
另有一条护栏：事故域前缀一旦出现在那两个 api 模块之外，测试即红。


### 3.2 为什么引擎内要按二级段切

引擎专项接口把"运维"与"性能"放在**同一个一级前缀**下（`/api/v1/oracle/{id}/` 有 172 条路由）：

| 路径 | 归属 | 结果 |
| --- | --- | --- |
| `/oracle/1/tablespaces` | 运维（`ops.space`） | 放行，实测 200 |
| `/oracle/1/users` | 运维（`ops.security`） | 放行 |
| `/oracle/1/awr/dashboard` | 性能（`perf.awr.*`） | 403 |
| `/oracle/1/top-sql` | 性能（`perf.slowsql`） | 403 |

**整体开放则性能泄漏，整体封锁则运维整片 403，两种一刀切都是错的。**
因此一级前缀放行，再按第二级功能段拦截；实例 id 位置的纯数字段会跳过，
`/oracle/awr/x` 与 `/oracle/42/awr/x` 同样命中。

### 3.3 三种拒绝体

| code | 触发 | 附加字段 |
| --- | --- | --- |
| `edition_module_unavailable` | 付费聚合前缀 | — |
| `edition_capability_unavailable` | 引擎内性能段 | `capability` |
| `edition_engine_unavailable` | 扩展引擎运维 | `engine` |

三者的 `edition` 字段均为当前版本值，`path` 为原始请求路径。

### 3.4 前端必须按分区降级，不能整页报错

**约定：`edition_*` 的 403 是版本说明，不是业务错误。**

开源版里只要有一个接口被锁，`Promise.all` 就会整体 reject，页面只剩一条
「当前为开源版，该模块需要企业版授权」的弹窗——用户既看不到本版本可用的数据，
也分不清是故障还是版本差异。实测踩过两次：

| 页面 | 一条被锁接口毁掉整页的原因 |
| --- | --- |
| 综合告警 | 统一事件依赖 7 个并发读，其中 Zabbix / Prometheus / 推送日志 3 条属集成中心 |
| 可观测总览 | 13 个并发读里有 `/observability/logs` |

实现分两层：

| 层 | 落点 | 行为 |
| --- | --- | --- |
| 识别 | `frontend/src/api.js` | 403 且 `detail.code` 以 `edition_` 开头时，给错误对象打 `editionLocked: true` 与 `code`；普通 403 仍按权限错误处理 |
| 降级 | `frontend/src/modules/editionFallback.js` | `optionalEditionCall()` 把被锁的调用转成 `{items: [], editionLocked: true}`；`isEditionLocked()` 供页面判断要不要渲染提示 |

页面侧要求：

1. 本版本可用的数据照常渲染，**不弹 error**
2. 被锁分区就地显示一条 info 说明（指到「集成中心」或对应企业版模块）
3. 依赖被锁接口的按钮要 `disabled` 并给出原因，而不是点了才报错

**排查同一类问题的方法**（新页面接入时必查）：把所有 `/api/v1/...` 路径字面量拿去跑
`edition.path_allowed()`，凡是 `False` 的就是会在开源版弹提示的位置。注意模板字符串
（`` `/zabbix/problems${...}` ``）要按前缀截断后再比对，否则会漏。

## 4. 引擎差异

| 维度 | Oracle / MySQL / PostgreSQL / SQL Server | 达梦 / 金仓 |
| --- | --- | --- |
| 引擎成熟度声明 | `ready` | `requires_runtime_validation` |
| 实例接入（建实例 + 测连通） | 可用 | 可用（`test_dm_connection` / `test_kingbasees_connection`） |
| Provider 结构契约 | 5 项能力全部通过 | 5 项能力全部通过（金仓身份链已补） |
| `server_certified` | `False` | `False` |
| 真机验证 | 有真实实例 | **无**（`dmPython` / `ksycopg2` 均未安装） |
| 运维路由 | 完整（Oracle 172 / MySQL 59 / PG 33 / MSSQL 38 条） | **0 条** |
| 开源版运维 | 全部放行 | 403 `edition_engine_unavailable` |

驱动现状（本机实测）：

| 引擎 | 声明驱动 | 可导入 |
| --- | --- | --- |
| Oracle | `oracledb` | 是 |
| MySQL | `pymysql` | 是 |
| PostgreSQL | `psycopg2` | `psycopg2` 缺失，但 `psycopg`(v3) 可用，连接器支持回退 |
| SQL Server | `pymssql` | 是 |
| 达梦 | `dmPython` | **否**（随 DM 客户端安装包，非 pip 包） |
| 金仓 | `ksycopg2` | **否**（随 libkci，非 pip 包） |

## 5. 代码与部署边界

### 5.1 归属

| 归属 | 路径 | 规模 |
| --- | --- | --- |
| **企业版** | `runtime/engine_host/` | 13 文件 4,138 行 |
| | `runtime/engine_packs/` | 8 文件 3,601 行 |
| | `runtime/engine_cluster.py` | 524 行 |
| | `runtime/platform_update.py` | 504 行 |
| | `runtime/platform_router.py` | 396 行 |
| | `services/engine_catalog_publisher.py` | 68 行 |
| | `services/engine_execution_transport.py` | 291 行 |
| | 主机侧 CLI（`scripts/engine_pack_manager.py` 等） | — |
| | **小计** | **9,522 行（6.3%）** |
| **开源版保留** | `runtime/engine_sdk/` | 955 行 |
| | `engine_providers/` | 249 行 |
| | `engine_registry/` | 109 行 |
| | `services/engine_execution.py` | 285 行 |
| | `services/engine_runtime.py`（协议层 + 空实现） | 131 行 |
| | 数据模型 + 迁移 + 只读目录页 | — |

### 5.2 关键设计点

- **`engine_execution.py` 留在开源版**。没有 `engine_host` 时它 `bridge()` 恒为 `None`，
  行为天然等价于"企业版未启用"——**开源版零 import 改动即可运行**。
- **`app/api/` 对 `engine_packs` / `engine_host` 零引用**，这是有意边界：
  引擎包生命周期只在主机侧 CLI，平台内唯一引擎 API 是只读的
  [engine_catalog.py](../../backend/app/api/v1/engine_catalog.py)（仅 `GET /` 与 `GET /drivers`）。
- **协议层 `services/engine_runtime.py`** 提供 `EngineRuntime` Protocol + `NullEngineRuntime`，
  未装 EE 时所有方法抛 `EngineRuntimeUnavailable`——**不返回伪造结果**。
- **迁移不可条件化**：`v20261027_01_engine_execution` 两边共用同一份迁移历史，
  开源版建空表。空表无害，两套历史分叉才是灾难。
- **生产模式准入**：`engine_pack_manager --mode production` 需要 `production_admission` 回调，
  缺失报 `production_management_admission_missing`。

### 5.3 开源版实测启动

`PYTHONPATH=runtime-backups/oss_sim:backend`（屏蔽 `engine_host` / `engine_packs`）下启动：

```
22 个迁移全部应用（含 20261027_01_engine_execution）
能力注册表同步 79 条 / 默认告警规则 107 条
Application startup complete
```

接口 `/health` 200、`/api/v1/engine-catalog` 200（无 token 时 401 而非 404，说明路由仍在）、
业务链 `801 passed / 4 failed`（那 4 项在完整环境下同样失败，属既有缺陷）。

## 6. 名义差异 vs 实际可交付差异

**这一节是本文最重要的部分。** 上面写的是"边界在哪"，这里写"边界另一侧到底有没有货"。

| 能力 | 名义归属 | 打开后真能用吗 | 依据 |
| --- | --- | --- | --- |
| 性能分析 15 个模块 | EE | **接口与菜单隔离已生效**，EE 侧功能本身未逐项验证 | `403 edition_capability_unavailable` 实测 |
| 数据保护 14 个模块 | EE | **同上**，未逐项验证 | `403 edition_module_unavailable` 实测 |
| 智能诊断 4 个模块（事故中心等） | EE | **接口与菜单隔离已生效**，页面功能未逐项验证 | `403 edition_module_unavailable` 实测；工作台侧 5 个接口实测 200 |
| 引擎热升级 | EE | **不能**。生产入口从未接线 | `wire_engine_business_runtime` 源码零调用者（ISS-75）；宿主生命周期部分修复（ISS-72）；fixture Provider 能力缺口（ISS-74） |
| 达梦 / 金仓接入 | EE | **只能建实例 + 测连通**，没有任何业务能力 | 运维路由 **0 条**；4 个 provider `server_certified` 全 `False`；驱动未装（ISS-71） |
| 多节点集群 | EE | **未验证** | `engine_cluster.py` 524 行代码在，无真机证据 |
| 引擎包签名与信任 | EE | 代码完整、测试绿，**但无生产入口消费** | `trust.py` / `contract.py` 测试通过 |

结论：**目前两个版本的真实差别是"性能分析、数据保护、诊断的事故现场在开源版被挡住" +
"达梦/金仓在开源版被挡住"。热升级这一项，企业版自己也还拿不到。**

### 6.1 已知的降级体验

菜单层已隐藏的页面，若在保留页面里硬编码了跳转入口，点击后会出现 403。
目前诊断工作台的主流程不依赖事故域接口（实测 5 个核心接口全部 200），
但**页面内是否有指向事故中心的按钮未逐一验证**。这属于体验问题，不是边界漏洞。

## 7. 判定口径

1. **lab 绿灯 ≠ 生产热更新**。必须走 `platform_router` + `--mode production` 的真实链路。
2. **源码一致性 ≠ 服务端认证**。6 个引擎的 `server_certified` 全部保持 `False`。
3. **测试变绿 ≠ 语义成立**。要能指出谁读它、怎么读。
4. **`app/api/` 对 engine_* 零引用是有意边界**，不是漏接。
5. **没有真机证据的能力不得写"已认证"**。

## 8. 怎么改这个边界

| 要做的事 | 改哪里 | 会被哪条断言拦 |
| --- | --- | --- |
| 某模块改成付费/免费 | `PAID_MODULE_PREFIXES` | `test_module_boundary_matches_default_modules_exactly`（必须与 `DEFAULT_MODULES` 逐项对齐） |
| 某接口改成付费 | `PAID_ROUTE_RULES` 增删一条规则 | `test_rule_segments_exist_in_real_routes`（段必须真实存在） |
| 增减开源版可运维引擎 | `OSS_OPS_ENGINES` **+ 前端** `menuConfig.jsx` 的 `engines: [...]` | `test_engine_whitelist_matches_frontend_declaration` |
| 新增付费域 | 同步 `module_config.DEFAULT_MODULES` 的 `module_key` | 同上第一条 |

改完必须同时更新：

1. `test_edition_boundary.py` 里的 `OSS_MODULES` / `EE_MODULES` 两个元组（各 30 / 43 项）
2. [edition-matrix.html](edition-matrix.html) 对应行
3. 本文 §1 / §2 的分组统计

六条防漂移断言（`test_edition_boundary.py` 共 13 项）覆盖"只改一半"的六种形态：

1. **默认不限制** —— 防止改坏了把现有部署锁死
2. **模块清单 = DEFAULT_MODULES** —— 防止漏项或凭空多出一项
3. **开源/企业互斥且各 30 / 43 项** —— 防止两边重叠或总数漂移
4. **规则段真实存在** —— 防止清单退化成"看起来像护栏的常量"
5. **前后端引擎白名单一致** —— 防止菜单可见但 403、或接口放行但不显示
6. **五个新开源项不被误拦** —— 慢 SQL / 锁等待 / 性能基线 / 容量分析 / AWR 综合
   是最后才划入开源版的，最容易被"整域按 `perf.` 前缀拦掉"的写法误伤

## 9. 边界确认清单

### 9.1 已明确（用户指定）

| 项 | 结论 |
| --- | --- |
| 数据库运维（`ops.*` 11 个模块） | **开源**，但只覆盖四种原生引擎 |
| 智能诊断 | **只保留诊断工作台**（`diagnose.smart`），其余 4 项企业版 |
| 性能分析（`perf.*` 15 个） | **企业版** |
| 数据保护（`backup.*` / `rescue.*` / `protection.*` 共 14 个） | **企业版** |
| 达梦 / 金仓的运维 | **企业版** |

### 9.2 我替你做的判断（待确认，改一行即可）

| 模块 | 我判为 | 依据 | 若改判影响 |
| --- | --- | --- | --- |
| `ops.governance` 数据治理 | 开源 | 菜单位置在「数据库运维 → 空间与对象」下 | 转企业版需从 `ops.` 拆分，接口 `data-governance` 加入付费前缀 |
| `ops.governance.objectStorage` 归档对象存储 | 开源 | 同上 | 同上 |
| `system.aiEvaluation` 诊断评测 | 开源 | 在「系统管理」下，属平台配置 | 转企业版需拦 `diagnosis-evaluation`（15 条路由） |
| `moduleConfig` 权限配置 | 开源 | 管理自身权限，锁掉会导致无法授权 | 转企业版会让开源版无法配置用户权限 |
| `platform.engines` 引擎与版本 | 开源 | 只读页，且是「企业版未安装」的唯一可见说明 | 转企业版后开源版看不到引擎状态 |
| `monitor.alert.db` 容量告警 | 开源；`perf.capacity` 容量分析为企业版 | 前者是告警规则，后者是分析报告 | 两个都叫「容量」，容易被误判为同一功能 |

### 9.3 尚未决定

| 项 | 当前处理 | 说明 |
| --- | --- | --- |
| `fragmentation` / `partition` / `optimize-table-plan` | 暂归运维（开源，未拦） | 介于空间回收与性能优化之间 |
| `tablespaces/sysaux-awr/*` | 暂归运维（开源，未拦） | 名字带 awr，语义是 SYSAUX 空间清理 |
| 首次启动无建号入口 | 未做 | 新库无管理员，需 ORM 直建；开源版发布前必须补 |
| `LICENSE` 文件 | 缺失 | 开源前必须选型 |

## 10. 未决问题
