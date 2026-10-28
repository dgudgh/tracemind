# C07 完整控制合同与独立 Schema 依赖

最新人工入口接续见本文末尾及[C01-C16接续](c01-c16-execution-2026-10-20.md)。本地控制、HTTP与前端证据不代表远端停止/fence、跨owner接管或现场完成。

日期：2026-10-20。关联：[C01-C16 清单](capability-card-task-plan.md)、
[U07/U11 台账](pending-development-backlog.md)、[C06 worker](capability-worker-design-2026-10-20.md)。

## 1. 本次交付边界

本文件定义完整 pause/resume/stop/对账合同，不以“暂停下发”冒充完整暂停。
本阶段只新增独立控制命令、目标 epoch 占用、追加对账 ORM 模型及隔离 schema 测试。
**Schema 本身不完成 C07：没有控制服务、迁移注册、queue/API 接线、域停止协议或对外放行。**
不修改共享业务文件，不访问真实数据库/Agent，不重启平台，不 commit/push。

现有 C06 提供提交前 owner/lease 检查、目标排他、冻结 hash、独立心跳和 unknown 不重放；
它不证明远端动作已经停止，也没有 checkpoint/resume 或人工解除占用接口。
巡检强制关闭只表示平台拒绝结果，不证明查询已退出；Agent `context.cancel()` 返回也不证明
进程退出和资源清理。这些限制在完整驱动验收前不能由 UI 或数据库枚举抹去。

## 2. 不变量

1. 原始 plan/domain outcome 与控制状态分离。`accepted`、ACK received、worker 失主、
   发布被拒绝、租约过期都不等于 `paused`、`stop_confirmed` 或业务成功。
2. `unknown` 与含 unknown 的 partial 保持待对账和目标占用；cancel/resume/升级/超时
   都不能自动清除。人工确认“停止”仍不等于确认“副作用已查明”。
3. 每个业务动作的 `execution_id=plan_id:seq:revision` 跨 owner/epoch 稳定；换 owner
   不是新业务幂等键，也不是重新执行授权。新一次业务执行必须新计划/重新审批。
4. 目标 epoch 单调递增且不复用。平台库 fencing 只保护平台事务；外部执行端必须原子校验
   epoch/命令版本，或能证明旧执行端和在途投递已消失，才可解除目标占用。
5. 新下发、结果发布、控制观察、对账裁决分别校验身份和证据；不能以一次请求授权永久放行。
6. 两把事务锁始终 `capability_plan_state -> capability_queue`，再按 target_key 排序锁目标行；
   已有只取 queue 的路径不得反向申请 plan_state。锁内不做网络/采集/Agent 调用。
7. 对账和已确认停止不能覆盖此前已提交的成功、部分或失败产品；迟到回执保留原始关联。
8. 没有新控制合同/可信停止证据的旧数据明确 legacy/unverified，不补造 epoch 或停止确认。

## 3. 状态与转换

三个状态轴独立保存，不把控制词塞入 plan outcome：

- desired：`run / pause / stop`，由已认证用户请求决定。
- observed：`running / paused / stopped / completed / unknown`，由当前权威证据决定；
  尚无观察时为 NULL，不能用默认 stopped 填补。
- execution lifecycle：保留 queued/running/archived/reconciliation_required/cancelled/
  expired/blocked，未来增 paused；控制版本在 execution 上 CAS 递增。

| 当前事实 | 请求/事件 | 允许转换及必要证据 |
| --- | --- | --- |
| queued、无已发业务 intent | pause | 原子禁止认领并保存 pre-start checkpoint，才确认 paused；不新占用目标 |
| running | pause | 记 pause_requested，禁止后续下发；当前步骤继续心跳和可信结果发布 |
| pause_requested | 当前步骤结束 | 原子保存产品/检查点；证明子作业和投递都静止后 paused；已完成全部步骤则 completed 优先 |
| pause_requested | 域支持真实挂起 | 只有驱动返回可校验暂停 checkpoint、无在途写入，才 paused；否则仍等待/不支持 |
| paused | resume | 复核权限/owner/冻结/审批/期限/预算/检查点；新 token 和目标 epoch，按 checkpoint 继续 |
| queued、确认未开始 | stop | 建立不可再下发屏障，记录 not_started 证据后取消投递；不得伪造业务已运行再取消 |
| running/paused | stop | stop_requested，阻止下一步；下发有界控制命令，不直接设置 stopped |
| stop_requested | received ACK | 仅表示执行端收到；保持未确认，继续观察/控制超时计时 |
| stop_requested | terminated/quiesced 且清理完成 | 核对执行身份、所有子作业和在途投递后 stop_confirmed；副作用未明仍待对账 |
| running/stop_requested | 真实业务终态先到 | 保留真实 succeeded/partial/failed/cancelled；停止请求记 moot/superseded，不改写产品 |
| active/pause_requested/stop_requested | 失主、超时、未知回执 | reconciliation_required，禁止自动重放，目标继续占用 |
| reconciliation_required | pause/resume/cancel | 不解除占用，不回到 queued；只接受观察或有证据的人工裁决 |
| archived/confirmed terminal | 迟到停止/旧 owner 回执 | 追加审计/隔离证据，不改写当前状态或已有产品 |

批次级控制展开成当前授权成员的独立命令，最多 100 个显式成员，逐项回读实际状态。
“批次已受理暂停/停止”不得表述为“所有子任务已暂停/已停止”。已启动 paused 保留目标占用，
但释放 worker 运行槽；paused/停止未决/对账未决都计入有界持久积压预算。

## 4. 命令、CAS 与控制通道

拟议入口为 `request_control(execution_id, action, expected_control_version,
idempotency_key, reason, actor)`；action 仅 pause/resume/stop/probe，不接任意 handler、
模块路径、SQL、脚本或外部地址。reconcile 是独立审计裁决写入口，不伪装成普通控制 ACK。

短事务内校验当前身份/范围/冻结绑定，CAS expected_version，保存 command 与控制投影，
commit 后才返回 command_id。相同 actor/key/hash 回放原命令，不同输入拒绝；
同 execution/control_version 只能存在一个受理命令。命令状态与业务 outcome 分开。

固定驱动的专用有界 outbox 消费者优先处理 stop，不排在采集、模型和普通业务队列后。
网络在锁外执行；只重投同 command_id 的幂等控制消息，不新建业务运行。
执行端检查 target epoch、command_id 和单执行 control_version，拒绝迟到旧 resume/pause。
received、quiesced、terminated、resumed、observed、completed 回执分别存储/验证；
command.status=applied 也只表示控制命令完成，probe 返回 unknown 不变成业务成功。

dispatch guard 检查 desired 状态及最新命令版本；publication guard 检查该动作原有授权、
当前 lease/token/epoch、冻结和实时权限，允许 pause/stop 请求期间当前在途动作提交真实结果。
不能在请求 pause 时立即吊销当前 token，造成合法在途结果丢失并扩大 unknown。
真正失主则禁止旧 worker 提交任何业务结果/失败；迟到可信域回执进入隔离观察，由当前
对账主体读取，不允许旧 worker 借“证据上报”绕过发布权限。

## 5. Step Checkpoint 与 Resume

新增的 checkpoint 持久记录应绑定 execution、plan/revision/hash、target_key、epoch、
业务 execution_id，以及 DAG 已完成/已跳过步骤、typed 产品引用、下一组可运行步骤、
驱动版本、输入输出 schema、已耗预算与 checkpoint hash。禁止凭客户端给出的完成列表续跑。

- 普通安全点是一个步骤的产品和状态完成原子提交之后、下一动作 dispatch intent 之前。
- 发出任何外部动作前，先持久保存固定驱动的 effect intent/稳定幂等键及目标绑定；
  域返回后再绑定真实 domain_run_ref。不能靠 started_at=NULL 推断丢失窗口里绝未外发。
- 步骤中途暂停仅由声明可恢复的域驱动提供 checkpoint；仅设置 worker Event 不算暂停。
- 恢复从持久 typed 产品重建 outputs 和 DAG，不依赖旧进程内存，不重跑 succeeded/partial
  或已有副作用的步骤；原 failure_policy 仍决定是否停止/跳过。
- 存在 running/unknown 步骤、丢失引用、绑定变化、驱动版本不兼容或审批/TTL 失效时拒绝恢复，
  转对账/重新审批。暂停不延长 TTL、不扩大预算、不自动修订参数。
- 现有 execute_plan 遇 running 会早退，未来必须在同一执行器增加受控 checkpoint-resume
  分支；不能把 running 改回 validated 或复制一套执行器来继续。

Checkpoint 与 effect-intent 表是后续完整迁移/实现依赖；本轮不创建它们的占位运行服务。

## 6. 本轮三个模型

模型文件为 `backend/app/models/capability_control.py`，沿用平台 Base、无 FK 的现有惯例；
**不加入 app.models.__init__，不注册迁移，不改现有 execution/queue/API。**

| 表/模型 | 主要字段与约束 | 事实边界 |
| --- | --- | --- |
| db_capability_control_command / DBCapabilityControlCommand | 固定 action、execution/plan/revision/hash/target、actor、expected/new control_version、epoch、幂等 key/input_hash、status、ack_stage/observed_state、时间、typed evidence ref；actor/key 唯一、execution/version 唯一、新版本=旧+1 | 受理与最新回执投影，不是执行端已停止的证明 |
| db_capability_target_fence / DBCapabilityTargetFence | target_key 主键、epoch、row_version、占用 execution/plan/hash、hold_state/reason、占用/释放时间和 release_evidence_ref | TTL 不自动释放；released 必须有证据；legacy_unverified 可无 epoch，但不得充当已 fence |
| db_capability_reconciliation / DBCapabilityReconciliation | execution/plan/revision/hash/target/observed_epoch、连续 sequence/previous_decision、runtime_observation、effect_outcome、resolution、记录人/复核人、三类证据、release_target | 追加历史，不覆盖原始 plan/domain unknown；确认停止不等于副作用查明 |

Evidence ref 只接受固定类型的 kind/id/schema 可选 sha256 指针，不接受 inline 任意载荷、
模块路径或命令。它只是引用；未来服务仍须加载实际记录并验证 owner、目标、时间、epoch、
控制命令、冻结参数和真实终态，不能相信用户给出的 verified=true 或非空 JSON。

对账 release_target 需要：终止/静止或未开始的 runtime 证据、副作用已知、旧 owner/旧投递
屏障证据、服务端校验时间、独立复核人；still_running/unresolved/disputed/unknown 禁止释放。
提出者和复核者不同。已确认停止但 effect_outcome=unknown 只能追加事实，不能释放目标。

模型跟随既有 InspectionTemplateVersion 使用 mapper update/delete 防护保证普通 ORM
对象的对账历史不可变。**这不是数据库级防篡改**：Core/bulk SQL、DDL、DBA 可绕过；
生产接线前须落实数据库权限/追加写接口和各引擎适用的防更新删除措施。
CHECK 在 MySQL < 8.0.16 不生效；枚举/ORM 校验不替代权限、CAS、真实证据与域驱动。

## 7. 权限与审计

新增动作键由主代理登记，例如 control.pause/resume/stop、reconcile.propose/review/release；
不是当前已授权权限。默认 owner 且具备相应动作及资产 operate 范围；跨 owner 操作需明确
人工接管权限和原因。admin 可以发起合规接管，不能跳过证据、审批或擅自清空 unknown。

每次请求、投递、确认、恢复、发布和裁决均实时复核用户 active/locked、角色、资产范围、
计划 owner/版本/hash/审批/期限；系统 worker 使用受限固定身份与原申请人绑定，不信任
请求体中的 requested_by、owner_token、epoch 或审批标记。

审计记录原命令、前后版本、冻结绑定、审核身份、证据引用与操作原因；不保存凭据、
原始连接串、任意 SQL 或秘密。批量请求先校验完整显式成员范围，不靠 UI 可见性授权。

## 8. 人工对账与释放

1. probe 只读获取当前权威域状态、稳定业务幂等键、epoch、执行端/目标标识及产品引用；
   “查不到作业/连接”不是未执行证据，旧请求仍可能排队或目标已经产生副作用。
2. 操作者提出追加记录；另一名授权审核者复核 runtime、effect、barrier 三类证据及范围。
3. 在原锁序内 CAS 目标 epoch/holder/row_version 与执行 control_version，追加已复核决议并
   释放占用；任何差异拒绝，不能套用针对旧目标的裁决。
4. 原始 unknown 不修改；另展示 resolution、resolved_outcome、证据与审核人。确认已停止但
   副作用未知仍待对账。没有“忽略未知/强制标成功”的通用按钮。
5. 已核实无执行或需重做，也不自动重投；新的业务动作必须显式新计划/重新审批。

## 9. 域 Driver 能力矩阵

以下按本次源码核对，均不代表实机验收：

| 域 | 当前可复用 | pause/resume 边界 | 停止与 fence 边界/放行条件 |
| --- | --- | --- | --- |
| 平台本地只读/元数据 | 现有 plan、事务锁、owner/CAS、before_commit guard | 步骤提交后的安全点；持久 outputs 仍待实现 | 本地事务屏障可阻止新写入；不能由此推断外部动作停止 |
| Oracle/MySQL/PostgreSQL/SQLServer 巡检 | jobs 稳定幂等/租约/取消请求、runner、staged writes、结果/报告、typed 真表核对 | 首批只承诺当前叶子调用结束后的安全点；中途恢复需连接/查询驱动明确 checkpoint | 当前取消/拒绝结果并不证明远端查询退出；需按引擎验证取消句柄、连接/事务结束、清理与所有子调用；通用远端 epoch fence 未实现 |
| Scene Agent capture | 命令队列、认证 Agent 身份、CaptureJob 和迟到终态保护 | 当前无可靠挂起/恢复协议 | context.cancel/received ACK 不够；需等待采集协程/进程 done、资源清理、命令版本/epoch 校验后回传证明 |
| 将来的备份/恢复/HA/写 SQL | 只复用已验证的专用域入口，不复用通用 SQL/脚本后门 | 驱动逐项声明不可逆阶段、checkpoint、补偿范围 | 没有目标侧 fence 或已证实停止/投递排空屏障则 blocked/待对账，不承诺 exactly-once，不接 AI 自动执行 |

固定驱动合同至少有：describe_control、request_pause/request_stop、resume_from_checkpoint、
observe、validate_receipt/validate_fence；这些是待实现接口，不是任意可调用 handler 字符串。
业务取消只在真实域取消证据通过后成立；batch cancel_requested 不等于 business cancelled。
force_closed、失主未确认或取消宽限期耗尽仍 unknown，不作为目标释放凭据。

## 10. 增量迁移与集成顺序

1. 本阶段只交付模型/设计。schema 测试在独立子进程直接导入新模型，避免未接线模型污染
   其他测试的应用 metadata；正常启动不会因本文件被写入而创建新表。
2. 后续主代理分配新 revision，先创建三表，再补 execution 控制版本/desired/observed/
   epoch/命令关联/checkpoint、checkpoint 和 effect-intent 结构，更新状态 CHECK 与索引。
3. 模型 import、迁移注册、schema baseline、权限键和发行门禁一并明确注册；当前
   check_backend_static 的未注册模型提示是待接线事实，不修改门禁规避。
4. 旧 queued 可进入显式新控制流程；旧 running/unknown 先 legacy_unverified/待对账。
   不把 DB 租约转换成远端 epoch，不伪造 ACK/停止证据，不重写历史结果。
5. 目标唯一占用和 epoch 在同一事务 CAS；释放后保留单调 epoch，行不可删除重置。
   command intent 不变，最新投递投影可更新；对账修正只能追加新 sequence。
6. 先接本地控制/checkpoint，再接巡检各驱动，最后接 Agent 和写动作；只有完整证据路径
   通过才逐域开放。未实现的操作返回 unsupported/待对账，不标 full_control。
7. 有命令/占用/对账历史不得自动降级删表；需要显式备份和人工发行裁决，保留回读兼容。

## 11. 验收矩阵与证据层级

| 层级 | 必须证明 | 本阶段状态 |
| --- | --- | --- |
| Schema | 枚举、唯一键、绑定/版本/epoch、applied ACK 证据、目标占用/释放约束、追加历史 ORM 防护、SQLite 重开持久性、并发 epoch CAS | 本轮隔离测试；不是控制服务 |
| 服务 | 权限/范围/审批撤销、幂等冲突、控制 CAS、重复/乱序 ACK、无证据拒绝、控制通道背压 | 未实现/未执行 |
| Pause/resume | queued 与 running、叶子前后/产品提交竞争、多步 DAG outputs 重建、不重放成功/partial、TTL/版本变化、重启失主 | 未实现/未执行 |
| Stop | received 不当 stopped、不可中断调用、所有子作业退出、清理失败、完成先于取消、迟到旧 ACK、断网/超时保持 unknown | 待专用驱动/故障实测 |
| 对账 | 无记录不推断未开始、停止但副作用未知不释放、错 owner/target/hash/epoch 拒绝、独立审核、CAS 竞争、历史 unknown 不改写 | 待真实服务实现 |
| 域与 C16 | 四引擎隔离级别/取消与事务清理、Agent done/cleanup、外部 fencing、双主/旧 owner、控制延迟、负载与 168h | 均未实机验收 |

Schema 中的“applied/释放”行只是约束示例，测试构造合法行不等于已验证真实外部停止。
独立 schema 通过也不能关闭 C07/U07/ISS-31/ISS-32。

## 12. 归档说明

本设计是共享工作区内的归档稿；本轮写集不含 Obsidian，未向知识库写入。
历史入口 `10-Projects/mesdbtools/主页.md` 强调隔离回归不等于现场验收；上述当前边界
来自本轮源码核对。后续应把真实实现和实测证据追加到本合同，而非预先勾选完成。

## 13. 接续证据（2026-10-20）

本代理已新增三个模型及独立子进程 schema 测试，首轮 24 passed（12.86s）。
其中双线程 SQLite 只证明 epoch/row_version 条件更新的单赢家原语，不证明控制服务已实现；
MySQL/PostgreSQL 只做 DDL 编译，未做实机隔离级别/停止/fencing 验收。
序列前驱是否属于同 execution、是否连续、epoch 是否单调、引用是否真实都仍需服务事务核对；
mapper 防更新/删除仍不是数据库级不可变保证。

初次 static 500 文件仅报未注册模型 1 项。随后主代理已补模型 import、
`v20261020_05_capability_control` 迁移及 runner 注册；这些共享文件不是本代理改动。
schema 测试的初始未注册检查现改为 `registered_contract`，明确断言三表已注册且迁移唯一。
接线后该 schema 测试重跑 24 passed（11.73s），后端 static 501 文件、0 findings。
上述是后续接线进展，不改写前述独立交付的历史边界；未执行平台迁移，也未开放 C07 控制能力。

冻结快照联合回归：queue 67 + workflow 8 + 真实取消 56 + schema 24，
共 **155 passed（143.30s）**。取消 helper/测试来自 Mencius，本代理只收紧 queue._evidence；
正常取消真证据允许归档，无 ref/错 kind/缺 plan 汇总引用拒绝，force_closed/unknown 保留占用。
此前在途进程收集旧 `unregistered` 参数后遇文件更新，150 passed/1 failed（174.86s）；
它正常退出后才执行上述全新冻结快照，未因观察超时重启，也不把旧阶段失败隐藏为通过。
该 155 项证据绑定 plan_service `32bbd6f7`、queue `08935084`、取消测试 `0222ee49`。
随后 Mencius 获新授权修 force-close 的 unknown 步骤保留真实 job 引用，尚未纳入本次结果；
后续应按其新快照独立复核，不把上述通过外推为新变更已验收。

## 14. 本地控制运行时增量（2026-10-20）

本节接续前述 schema-only 历史，实际实现范围为同一执行器的本地多步安全点控制；
**不是外部查询/进程强停、远端 epoch fence 或完整人工对账放行。** 不修改 jobs/report/admission/API。

### 服务与版本合同

```python
request_control(db, execution_id: int, *, actor: dict, action: str,
                expected_control_version: int, idempotency_key: str, reason: str) -> dict
read_control(db, execution_id: int, *, actor: dict,
             idempotency_key: str | None = None) -> dict
enqueue_batch(db, plan_ids, *, actor, idempotency_key, deadline_seconds=3600, schema_version=1) -> dict
cancel_batch(db, batch_id, *, actor, schema_version=1) -> dict
read_batch(db, batch_id, *, actor, schema_version=1) -> dict
list_batches(db, *, actor, page=1, page_size=50, schema_version=1) -> dict
```

控制入口位于 `app.services.plan_control`。action 仅 pause/resume/stop/probe；实时检查可信 user_id、
当前 active/locked、原 plan owner、冻结 revision/hash、`control.<action>` 与资产 operate 范围。
回读要求 control.probe。resume 还检查原业务权限、审批、原 plan/batch TTL 和持久证据。
权限目录/HTTP/scope/前端由主代理接线，不自动扩大内置角色权限。

返回 schema=`capability.control/v1`，包含 execution_id、plan_id、control_enabled、control_version、
desired_state、observed_state、queue_state、checkpoint_hash、fencing_epoch、reused 和 command。
command 为 command_id/action/status/ack_stage/control_version；未有命令可为 NULL。
返回固定 scope=`local_step_boundary`，external_stop/external_fence=`unsupported`。
accepted 只表示命令已持久提交；paused/stopped 也仅表示此 execution 的本地调度静止，
不表示目标上所有外部动作已经停止，不改写 plan/domain outcome 为 cancelled。

GET 无写入。提供 Idempotency-Key 时，只按真实 owner+原 key 找 Command，再比对 execution、
plan/revision/hash，返回原命令（含 superseded）及当前控制投影。不按 action/version 猜关联；
不存在或属于另一个 execution 返回 control_command_not_found。受理回放还比较完整输入 hash。
Batch/Command 都在 SQL 命中后严格 Python 原 key 相等检查，CI 命中错大小写时 fail closed；
不修改老表唯一键，真实 MySQL CI 大小写共存仍待 C15，不把 SQLite 模拟比较当实机验收。

主代理用 `X-Assistant-Batch-Version: 1|2` 协商后传 service schema_version，不加 body/query 旁路。
默认未控制 v1 字段保持原样；v2 明确返回 batch_read/v2、batch_list/v2 和成员 execution_id、
control_enabled/version、desired/observed_state、checkpoint_hash、fencing_epoch。
有权访问的 controlled 批次若以 v1 回读/list/幂等回放/cancel，则在提交前拒绝
batch_control_requires_v2，不能伪装 queued/blocked、跳过成员或改变 v1 词表。

### 字段与增量迁移

仅在 db_capability_execution 增加下列 9 列；不新增 checkpoint/effect-intent 模型。
步骤 running intent 在叶子调用前提交；进程在该窗口死亡只能待对账，不自动重放。

| 列 | 类型/默认 | 含义 |
| --- | --- | --- |
| control_enabled | Boolean NOT NULL DEFAULT false | 默认旧执行路径，无控制副作用 |
| control_version | BigInteger NOT NULL DEFAULT 0 | 命令 expected-version SQL CAS |
| desired_state | String(8) NOT NULL DEFAULT run | run/pause/stop |
| observed_state | String(16) NULL | running/paused/stopped/completed/unknown；无观察保留 NULL |
| control_command_id | String(30) NULL | 当前改变调度的命令；probe 不替换在途 pause/stop |
| fencing_epoch | BigInteger NOT NULL DEFAULT 0 | 本地目标代次；0 为未持有目标，不是远端 fence |
| checkpoint_json | JSON NULL | 最新持久安全点，包含完整已提交产品引用及 outputs/failure |
| checkpoint_hash | String(64) NULL | checkpoint 内容 hash，恢复前重建并核对 |
| checkpoint_at | DateTime NULL | 安全点存储时间 |

state CHECK 增 paused/stopped，新增控制取值/非负代次 CHECK，以及 paused/stopped 必须有
checkpoint/hash/time 且无 owner/lease 的 CHECK。Command/Fence/Reconciliation 表本轮无新字段。
迁移文件 `v20261020_07_capability_control_runtime.py`，REVISION 同名去前缀 v；主代理注册 runner。
旧行保留原状态、token、时间、unknown 与全部产品，control_enabled=false，不补造控制证明。
SQLite 对真实旧表加列并复制重建 CHECK，保留索引；有自定义 trigger 则先拒绝，避免静默丢弃。
不支持有损自动降级。MySQL/PostgreSQL 只做编译/源码检查，实际升级与隔离级别验收未执行。

### 事务与安全点

锁序 state→queue→target→security_audit；请求提交前 append_security_audit 记录真实 actor、
plan/revision/hash、command/version/action/reason/key/input_hash，审计空返回/异常使全部回滚。
相同 key 的真实回放不新增命令/审计；不同输入、过期版本拒绝。

dispatch gate 在同一短事务内检查 desired 状态并提交下一步 running intent；然后释放锁才调用
原 handler。publication guard 仍只核当前 owner/token/lease/epoch/冻结/实时授权，不能因为
pause/stop 请求就拒绝当前在途动作的真实产品。commit fence 仍使用 RuntimeError→显式
PlanOwnershipLost/rollback 合同，不改异常继承，不让失主发布 failed。

暂停只在所有已发本地调用返回、产品提交后确认；checkpoint 绑定 plan/revision/hash、
本地 epoch、原 batch deadline、逐步状态/产品/时间、完整 outputs、failure。恢复前从真实
step 和 inspection_job 重建证据再逐项核对 hash/内容；每次新认领才增长目标 epoch。
plan 保持 running，started_at 不变，不重置 validated；已完成 succeeded/partial 不重放，
failure_policy=stop 仍生效。paused 保留目标占用；resume 排队期间也保留占用，但不占 worker 槽。
pause 不延长期限、不增加预算。最后一步完成优先于暂停，命令可 superseded 而业务成功保留。

stop 在安全点停止后续本地下发，保存 stopped checkpoint；已知产品保留，plan outcome 不伪造。
unknown/失主/证据缺失始终 reconciliation_required，不 stop 清空、不 resume、不释放目标。
真正远端停止/fence 和人工释放仍无驱动/对账实现，返回 unsupported/未决，不能据此关闭完整 C07。
当前 checkpoint 为最新投影，不是追加式全历史存储；命令/安全审计保留关联 hash，生产审计级
历史 checkpoint 归档仍需后续实现，不能把现有 mapper guard 当数据库防篡改保证。

### 稳定交接与实测

本地控制服务/执行器源码冻结于本次交接；不修改 assistant.py、jobs、report、admission、
runner、models.__init__、权限目录或前端。主代理负责 HTTP 错误码映射：
control_command_not_found 为 404、control_audit_unavailable 为 503、状态/CAS/版本冲突为 409。
有权访问与具体 HTTP 状态由主代理接口层处理；服务的 fail-closed 事务不依赖这些映射。

- 完整联合：**210 passed（176.40s）**，含本地控制 39、旧 schema 迁移 4、queue 67、
  workflow 8、真实取消 60、控制 schema 24、当时控制 HTTP 8。
- 随后只增加 SQLite 表重建对表达式索引的拒绝保护及负例，防止反射略过后静默丢索引。
  最新迁移 5 + 注册检查 8，**13 passed（6.08s）**；该单项补充不冒充已纳入前述 210 项。
- 后端 static：**506 文件、0 findings**。主代理后续新增/修改的 HTTP 30 项归其独立结果，
  不用本代理较早 HTTP 8 项的通过替代。

控制覆盖真实事务 CAS/同键回放、原 key 精确查询与丢回包恢复、只读 GET、owner/operate/grant、
审计失败回滚、请求和发布分离、最后一步完成竞争、DAG outputs、四引擎真实巡检 job 引用、
新进程恢复不重放、真实子进程在 intent 后死亡、失主成功/失败均不发布、epoch 失配拒绝、
未启动安全恢复与取消、unknown 目标占用、v1/v2 不混用。
迁移从手写真实旧列/旧 CHECK/旧索引构建，带 queued/running/unknown 历史后升级，重复升级
保留数据及普通索引；旧表有 trigger、表达式索引或自定义外键依赖时先拒绝人工适配，
不静默破坏定制。迁移不自动清空 owner、unknown 或历史产品，不做有损降级。

冻结 SHA-256：

```text
plan_control.py 9a77959e9be28d1e1baaaa5c9888c70d0f294150086dc2f77cd1c19cf75fd90a
plan_queue.py 876d436e81665a40161991839e6598201311d4ef86d42a2efdfc9de2b702800d
plan_service.py 93ff32fc599aedd5ed155731051356168d685195f2d1c99984a88a806ba11a60
capability_execution.py 90227329c67b59cefe4f9361a25eab8d73924a9684009bbe1a256dc2c12eeec8
v20261020_07_capability_control_runtime.py a69236731ab52c068283ea3042f69ae52e1e7a759f0be015ed266c45db05b15e
```

所有测试使用隔离 SQLite/夹具，无现场 DB/Agent、真实平台迁移、平台重启、commit/push。
未执行 MySQL/PostgreSQL 实机隔离级别/DDL/跨节点 fence 验收，未实现真实远端强停或人工
对账释放，不能把本地运行时覆盖标为完整 C07/C16。归档稿仍留本工作区，本轮未写 Obsidian。

## 15. 人工入口与独立前端复核

主代理已接真实JWT保护的控制HTTP，以及批次弹窗显式“运行控制”tab。v1不会自动转v2；
用户按已知批次号取v2，再选持久execution_id。面板提供pause/resume/stop/probe、当前投影、
checkpoint、原命令GET与错误状态；点击确认不代表停止成功。完整v2历史选择器仍待C09。

- `AssistantControlPanel.jsx`、`modules/assistantControlApi.js`：严格控制DTO，当前投影与原key
  历史命令分开。只有确切原command关联可以解除提交unknown；404/409/503不自动换key。
- `AssistantBatchControlView.jsx`：独立显式v2批次读取；同目标/会话/活性/请求epoch检查，90秒
  超时无自动重试；不能用v1伪造paused/stopped。
- `api.js`新增内部可选authSession绑定，在构造请求后再校验并使用捕获token，不向fetch透传
  会话对象。Batch/Plan/Control变更均绑定原确认会话；无该选项的旧调用和后端接口保持原合同。

独立复核首次确证两个P2，原复现留在/tmp：rc-dialog隐藏时缓存children导致晚回包仍更新
journal；locator渲染后过期但recover仍使用缓存的recoveryUsable。主代理不是只加JSX条件或
延长TTL：整个Batch在closed时返回null，关闭事件先更新动态活性ref，View向Panel传读取该ref
的函数；Panel所有入口/发布重查活性。recover在点击及回包时重查真实expires_at，过期仅可
人工只读且不解除unknown。修复后的独立Chrome/动态复验以主执行记录最终轮为准。

相关实现还修复跨tab旧确认提交、失败存储回用旧token；不能从本地失效机制推导所有持久写删
都拒绝之后reload已清除旧记录。已发出的网络请求也不会因为client epoch改变自动撤回；
实际后端owner/权限/版本/审计/执行fence始终是必须保留的门禁。

本段集成和Obsidian摘要由主代理负责；前述“本代理未归档”仅指该独立子任务写集，不代表
主代理未更新指定知识库。真实服务链、原请求key哈希、桌面/移动截图和最终验收结果见
[实施接续](c01-c16-execution-2026-10-20.md)。未放行远端强停、现场迁移或完整C07。
