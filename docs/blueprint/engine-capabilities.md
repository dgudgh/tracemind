# 运维能力矩阵（15 项 × 4 引擎）

> **本文是"能力说明书"，不是操作页。** 原「数据库运维 → 能力矩阵」页面已于 2026-10-27 移除：
> 这张表的内容属说明性信息，适合放在文档；页面本身在运维菜单里与业务操作页混排，误导性大于价值。
>
> **能力没有被删除。** `operations.capability_matrix.read` 仍然是已登记、已接线的只读能力，
> 继续供 AI 调度（`operations.capabilities.list` 工作流）和 API 直接调用，只是不再有菜单入口。
>
> 权威源是代码，不是本文：
> [operations_capabilities.py](../../backend/app/services/operations_capabilities.py)（`CAPABILITY_CATALOG`）
> 与 [capability_declarations.py](../../backend/app/services/capability_declarations.py)。
> 本文表格由运行中的 `/api/v1/operations/capability-matrix` 实际返回生成（2026-10-27）。

## 1. 三种执行模式

| 模式 | 含义 | 系统行为 |
| --- | --- | --- |
| `read_only` | 只读查询 | 直接连目标库采集，不产生变更 |
| `plan_only` | 生成方案 | 只产出变更/验证/回滚脚本与前置检查，**不从运维页面直接改生产** |
| `approval_required` | 审批后执行 | 必须进审批链，审批通过后才真正调用执行器 |
| `unsupported` | 不支持 | 能力对当前引擎不提供，平台拒绝执行，**不得用审批代替放行** |

`plan_only` 和 `approval_required` 的区别是硬性的：前者永不落变更，后者的变更有审计链。

## 2. 能力矩阵

图例：**R** = `read_only`，**P** = `plan_only`，**A** = `approval_required`，**U** = `unsupported`

| # | 能力 | 名称 | 领域 | Oracle | MySQL | PostgreSQL | SQL Server |
| --- | --- | --- | --- | :---: | :---: | :---: | :---: |
| 1 | `runtime.overview` | 实例运行概览 | 运行与会话 | R | R | R | R |
| 2 | `runtime.sessions` | 会话、事务与阻塞链 | 运行与会话 | R | R | R | R |
| 3 | `runtime.terminate_session` | 终止会话与解除阻塞 | 运行与会话 | U | U | U | U |
| 4 | `configuration.parameters.read` | 参数与配置漂移 | 参数与配置 | R | R | R | R |
| 5 | `configuration.parameters.change` | 参数变更方案 | 参数与配置 | P | P | P | P |
| 6 | `storage.capacity` | 空间容量与增长 | 空间与对象 | R | R | R | R |
| 7 | `storage.expand` | 容量扩展方案 | 空间与对象 | P | P | P | P |
| 8 | `objects.statistics` | 对象与统计信息 | 空间与对象 | R | R | R | R |
| 9 | `objects.maintenance` | 对象维护方案 | 空间与对象 | P | P | P | P |
| 10 | `logs.runtime` | 数据库日志与归档 | 日志与恢复 | R | **U** | R | R |
| 11 | `ha.health` | 高可用健康诊断 | 高可用 | R | R | R | R |
| 12 | `ha.switchover` | 计划切换与故障切换 | 高可用 | P | P | P | P |
| 13 | `protection.backup` | 备份执行 | 数据保护 | A | A | A | A |
| 14 | `protection.restore` | 恢复与演练 | 数据保护 | A | A | A | A |
| 15 | `governance.archive` | 归档与生命周期治理 | 数据治理 | P | P | P | P |

60 个单元格的分布：`read_only` 27、`plan_only` 20、`approval_required` 8、`unsupported` 5。

## 3. 三处需要特别注意的地方

**① 终止会话全部不支持（4 格）。**
Oracle / MySQL / PostgreSQL / SQL Server 都是 `unsupported`，即使 SQL Server 与 PostgreSQL 的连接器里已有 `KILL` / `pg_terminate_backend` 语句。
原因是"语句存在"不等于"已授权执行"：真实会话终止尚未完成实机验收，平台保持拒绝，**不允许用审批链绕过**。

**② MySQL 缺日志能力（1 格）。**
`logs.runtime` 对 MySQL 为 `unsupported`，其余三引擎为 `read_only`。MySQL 的日志读取走的是独立路径（binlog 相关），未纳入这张统一矩阵。

**③ 备份与恢复是唯一需要审批的两项（8 格）。**
它们成熟度**不由本矩阵声明**，而是按各引擎真实执行能力派生——矩阵里写 `approval_required` 只代表流程形态，不代表该引擎真的能跑。

## 4. 可用性判定（运行时状态）

模式是静态契约，**可用性**是每次请求实时算的。判定顺序（`_availability`）：

| 状态 | 触发条件 |
| --- | --- |
| `unsupported` | 模式为 `unsupported`，或成熟度为 `planned` |
| `blocked` | 成熟度 `blocked`（引擎级拒绝，任何凭据都救不回）、实例停用、凭据未配置、最近连接失败 |
| `not_configured` | 该引擎没有任何实例 |
| `permission_denied` | `approval_required` 但当前账号无运维执行权限 |
| `plan_available` | `plan_only`（可出方案；连接失败时也仍可出方案，但必须先修连接） |
| `unverified` | 尚无有效连接测试 |
| `degraded` | 可用，但本地凭据已进入轮换提醒 |
| `partial` | 成熟度 `partial`，仅覆盖已实现的采集或方案范围 |
| `available` | 上述都不成立 |

**顺序不能调。**`blocked` 在 `unverified` 之前，所以"连接失败"不会被报成"待验证"；
`plan_available` 在凭据检查之前，因为出方案不需要连上目标库。这两条是最容易被改错的。

## 5. 怎么查

```bash
GET /api/v1/operations/capability-matrix                    # 平台矩阵（全部实例）
GET /api/v1/operations/capability-matrix?engine=oracle      # 只看某引擎
GET /api/v1/operations/capability-matrix?instance_id=7      # 只看某实例，附带连接状态
```

响应含 `summary`（能力数、引擎数、范围内实例数、模式分布、可用性分布、当前账号是否有执行权限）
与 `items`（每项能力 × 每引擎的 `mode` / `maturity` / `availability` / `ready_instances` / `configured_instances`）。

## 6. 为什么没有菜单入口

| 原因 | 说明 |
| --- | --- |
| 内容性质 | 15 × 4 的静态契约表，读一次就够，不需要反复操作 |
| 混排误导 | 它和「空间管理」「会话事务」并列在运维菜单里，但那两项需要选实例才可用，它却是平台级视角 |
| 已在别处 | 「打开」入口在各自能力页里都有；执行前的范围与风险由 AI 调度的能力清单给出 |
| 能力仍在 | 删的是页面，不是能力。AI 调度、API、计划编排都不受影响 |

对应的代码变更：`module_config` 移除 `ops.matrix`（模块 74 → 73）、
`capability_declarations` 该能力的 `target_key` 置空（避免模型导航到不存在的页面）、
`capability_dispatch.WORKFLOW_NAV` 移除该导航项。

## 7. 防漂移

`backend/tests/test_capability_declarations.py` 里有一条断言把"无页面的能力"钉死：

```python
pageless = {x["capability_id"] for x in CAPABILITY_DECLARATIONS
            if x["bindings"] and not any(b["target_key"] for b in x["bindings"])}
assert pageless == {"operations.capability_matrix.read"}
```

新增"有接口没页面"的能力会让它变红——**逼人确认这是有意为之，还是漏做了页面**。
本文表格若与代码不一致，以代码为准；改矩阵时同步本文与 `edition-matrix.html` 的相关行。
