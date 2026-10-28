# C10 固定版本流程库最小完整设计

日期：2026-10-20。对应 [C10](capability-card-task-plan.md)、[U09](pending-development-backlog.md#u09-固定流程库与确定性意图p1c10)。
本稿以当前共享源码为依据，不改既有 plan/API/路由/权限/前端文件，不触发实际采集、迁移或部署。
**这是完整设计合同；第 10 节记录已完成的纯静态第一步，不以静态目录或匹配单测通过宣称 C10 已完成。**

## 1. 当前事实与复用边界

- `diagnosis_center._local_route_intent/route_intent` 是页面/诊断分诊，可能调用模型；不是流程编译或执行授权。
- `DiagnosisCenterPage.runDiagnosis` 先走 `resolveAssistantInspectionCommand`，再走 AI 分诊及
  `resolveAssistantActionRequest`。这些关键词/页面分支没有持久 workflow 版本，不能直接充当 C10。
- 成熟静态能力为 inspection.run.query@1、inspection.template.read@1、
  operations.capability_matrix.read@1，以及 inspection.run.execute@2。
  `inspection.report.read` 仍为未接独立 handler 的声明，不得伪造报告生成/比较流程。
- `plan_service` 已有编译、冻结、DAG、revision、审批及 execute_plan；`plan_admission` 已有
  preparation/admission 原 key 回读；`plan_queue` 已有持久批次、worker 和未知目标占用。
  C10 只增加固定 recipe 与受约束编译，执行仍是这条链，不造第二执行器。
- Obsidian《功能卡片与可选 AI 编排》的“固定是语义/规则而非健康答案”作为历史原则；
  其中早期未实现条目不代表当前代码状态，当前能力以上述源码核对为准。

## 2. 固定的是什么

每个 recipe 具有稳定 workflow_id、整数 workflow_version、规范化 definition_hash，包含
意图代码、严格槽位 schema、显式默认值、固定步骤/能力版本、依赖、预算、风险与输出合同。
同一版本不得原地改槽位/默认值/顺序/能力版本；任何语义变化新增版本，保留旧版本只读历史。
不存在 latest 自动升级、任意 handler、SQL、脚本、URL 或自定义步骤入口。

matcher_version 与 workflow_version 分开。固定 matcher 版本 + 相同规范化输入，得到相同
语义决策和 workflow 引用；当前权限不足/停用时拒绝，不改选另一个流程作为降级执行。
固定的是业务程序，不是业务结果：重新读取实际数据可以变化，不能缓存一个“健康”答案。

## 3. 首批五个 Recipe

下表 recipe 均从 workflow_version=1 开始，引用的能力版本独立标明，不把巡检 v2 写成 v1。

| workflow_id | 固定步骤 | 显式槽位与边界 |
| --- | --- | --- |
| inspection.jobs.list | inspection.run.query@1 | page/page_size/status/outcome/trigger_type；只查询已保存作业，不采集 |
| inspection.templates.list | inspection.template.read@1 | db_type/enabled/page/page_size；不 seed 默认模板 |
| operations.capabilities.list | operations.capability_matrix.read@1 | engine；显示实际能力，不触发能力对应业务动作 |
| inspection.context.read | 依次 matrix@1 → templates@1 → jobs@1 | 固定三步读取，同一显式目标；仅整理三份真实产品，不声称跨步骤原子快照或健康结论 |
| inspection.collect | inspection.run.execute@2 | 必填 instance_id、policy_id；timeout_seconds 显式默认 900，必要审批、持久队列和发布开关照常生效 |

三读组合的依赖是固定串行依赖，failure_policy=stop，不根据模型/返回文本改 DAG。
巡检发布开关的代码默认值仍为 false；可以展示被阻断原因，不因此静默开启或声称部署已可采集。
单读可用已有读取计划短路径执行；采集只能复用 preparation→审批→enqueue→worker。
已有普通 GET 仍可轻量读取，不因新增流程库强迫全部旧读取建计划或入队。

默认值只允许目录声明的分页/超时等确定性配置，预览显示 effective value 与来源。
绝不默认选择第一台实例、当前用户全部资产、默认模板、最近一次结果或隐式授权范围。
policy_revision/runtime_digest/target_fingerprint 等冻结字段只由原 plan 冻结器生成，
不是客户端可填写的流程槽位。禁止 arbitrary SQL，即便看起来是 SELECT 也不解释/运行。

## 4. 确定性意图，不是 Prompt 别名表

先识别语义操作，再绑定 recipe；手工 workflow 选择直接进入同一编译器，不绕回自然语言。

1. 有界文本规范化采用固定规则，保留否定、ID 和操作词；拒绝超长/控制字符输入。
2. 区分 navigate、view_existing、prepare、rerun；再识别 jobs/templates/capabilities/context/collect。
   “查看任务”不等于“运行巡检”，“准备方案”不等于“立即执行”。
3. 否定、条件、多个操作、多个引擎/目标或只有“检查一下”时返回 needs_input/ambiguous；
   不按某个关键词优先执行。“不要巡检，只看报告”至少保证不产生采集候选。
4. 明确“打开页面”只产生导航建议，不创建 plan；“查看已有结果/报告”要求明确原引用，
   走既有鉴权回读，不能回退成巡检采集。“重新生成/比较报告”没有成熟处理器则 unsupported。
5. 不从自由文本猜实例 ID、范围、模板或结果 ID。文本只能指出缺失槽位；用户在受权表单明确
   选择后，服务端重新校验实际对象。引擎词只作显式约束，不能把不匹配目标改成另一台。
6. 返回 matched 也只是确定性选择；仍需显式参数、服务端校验、冻结预览与用户确认。
   未命中返回 not_matched，不偷偷进入旧泛化 action 自动执行。

语义决策记录 matcher_version、规则代码、workflow_id/version/hash、operation、missing_fields
与原因码；不把自然语言文本作为可执行代码或授权凭据，不保存凭据/原始 SQL。

## 5. 参数、范围与纯静态服务合同

拟新增 `backend/app/services/workflow_library.py`，只做不可变代码目录与有界纯函数；
不接 DB/session、actor、handler、回调，不调用 registry 执行器、网络、模型或时钟。
纯函数不宣称授权、持久化、受理、审批或业务成功。

```python
list_workflows() -> dict
get_workflow(workflow_id: str, workflow_version: int) -> dict
match_workflow(text: str, *, matcher_version: int = 1) -> dict
compile_workflow(workflow_id: str, workflow_version: int, *,
                 slots: dict, scope: dict) -> dict
```

目录 schema=`capability.workflow_catalog/v1`，单定义 schema=`capability.workflow/v1`；
match schema=`capability.workflow_match/v1`，decision 为 matched/needs_input/ambiguous/not_matched/rejected，
operation 为 navigate/view_existing/prepare/rerun 或 NULL，不返回“已执行”。
compile schema=`capability.workflow_candidate/v1`，返回 workflow_binding、plan_inputs、candidate_hash、
missing_fields 与 `accepted=false`。缺槽位不产生可执行 plan_inputs；非法字段则明确拒绝，不能静默丢弃。

scope 只接受 global 或 explicit：global 仅适用于声明支持的读取 recipe；
explicit 为有序、无重复且 1..100 个数值 instance_id 的目标列表，逐项带明确 slots。
例如 scope={mode: explicit, targets: [{instance_id: 10, slots: {policy_id: 7}}]}，
公共 slots 可给 timeout_seconds；同一参数同时出现在公共/目标层即拒绝，不做隐式覆盖。
巡检采集必须 explicit；“全部”也必须先由用户确认展开后的具体名单，不能以 all=true 延迟扩权。
target 顺序参与候选/受理身份，不悄悄排序或过滤越权目标。三读组合目标的实际 engine 由
服务端资产校验后绑定；客户端 engine 与实际资产不同则拒绝，不信任字符串自报引擎。

源码参数结构仅作第一层类型/白名单校验；数据库集成层仍必须调用原 registry/compile_inputs/
freeze_steps/validate_step_contracts，不能让静态目录成为动态权限或能力可用性的缓存。
分页/status/outcome 等范围以现有参数模型为真；新字段或不兼容变更必须目录升版。
definition_hash/candidate_hash 使用严格规范化 JSON，禁止 NaN、bool 冒充 int、未知键和不支持值。

## 6. 持久版本与审批绑定

最小方案不新建一个可编辑的运行时流程表。代码目录是版本来源，**新流程计划必须把服务端
取得的完整 recipe snapshot、workflow_id/version/definition_hash 写入原 plan snapshot 的
workflow_binding，并随 DBCapabilityPlanRevision 持久化**；只在前端记 workflow 名称不算完成。

必要集成点由主代理后续实现：

- create_plan/compile_inputs 增内部可选 workflow_binding，必须在 request identity、快照 hash、
  approval_binding_hash 和 revision 保存之前构造，不能创建/审批后再补字段。
- 新 workflow HTTP 不接客户端 steps/handler/schema/approval/hash；客户端只传 ID、版本、
  slots 和显式目标，服务端用固定目录生成步骤与完整 binding。
- 业务 request_hash 包含固定 recipe 身份/定义 hash 及规范化参数/目标；不把匹配别名、
  prepare/rerun 操作名或每次变化的 expires_at 混入同业务重跑身份。
- matcher/入口来源另存 provenance，不能作为审批授权；完整 recipe/槽位/步骤变化影响计划
  binding。修订必须重编译原 recipe 并重新审批，不接受把固定流程偷偷改成任意 DAG。
- 多目标复用原 prepare_batch 的原子冻结/receipt 机制，内部增加受校验 workflow binding，
  admission_hash 纳入 recipe 和目标有序输入；不得循环调用 commit=True 造成半批次落库。
- 启动同步/执行前复查同版本定义和能力可用性；退役/禁用/版本漂移拒绝新执行，旧快照仍可
  按原权限回读。不从今天目录替换旧运行的版本/步骤，也不根据数据库可写 JSON 造新处理器。

这些是尚未实现的必要合同；静态服务/测试不能替代持久集成与真实审批绑定验收。
若后续需要在线编辑/发布流程，另做 append-only version 表及迁移，不把本次静态目录变成脚本平台。

## 7. 查看、同请求防重与明确重跑

| 操作 | ID/请求身份 | 必须行为 |
| --- | --- | --- |
| 查看已有结果 | 明确 plan_id/job_id/report 引用 | 既有 GET + 当前授权；刷新只 GET，不执行、不自动选择最近一条 |
| 新建流程清单 | actor + 原 Idempotency-Key + recipe/输入 hash | 原子持久化后返回；同键同输入回读原 plan/receipt，异输入 409 |
| 丢创建/入队回包 | 原 key | 只调用现有 preparation/admission 回查，不换 key 重发，不把超时当未受理 |
| 明确重新运行 | 新 key + source_plan_id + 原固定 recipe/同业务输入 | 新 plan_id，必要重新审批、显式入队；旧结果保留，不复用旧成功答案 |
| 恢复 paused | 原 execution/checkpoint | 仍归 C07 resume，不伪装成 C10 rerun，也不新建作业重放旧步骤 |

当前 parent_plan_id UNIQUE 只允许每个父计划一个重跑子计划。第三次运行以最近已完成子计划
为 source 形成链；并发重跑同一个父计划冲突时展示已存在的子计划，不能删唯一键或永久命中根结果。
父计划必须真实 owner 可读、同 recipe/参数/范围且业务已终结；running/paused/stopped、unknown，
或 partial 内含 unknown 的源拒绝重跑，保留目标占用并进入既有待对账路径。
新运行获得新期限和新审批是用户的显式新执行，不是替旧暂停计划延长 TTL。
旧计划没有 workflow_binding 时可正常回读；标记 legacy_unlinked，不凭能力相同猜其流程版本。
旧清单重跑继续旧显式计划入口；不能在 C10 静默认领为某个固定流程。

MySQL CI 命中后的原 key 必须字节相等，或复用 preparation 的 lower SHA-256 key 身份；
不因大小写折叠串请求。真实 MySQL 大小写共存/迁移仍单列 C15 验收。

## 8. HTTP 与手工前端最小闭环

拟新增、由主代理接线：GET /assistant/workflows、GET /assistant/workflows/{id}/versions/{version}、
POST /assistant/workflows/match、POST /assistant/workflows/{id}/versions/{version}/prepare。
prepare 使用严格 body（operation、slots、scope、source_plan_id，仅重跑时使用）及必需
Idempotency-Key。只能创建/复核清单，不在 match/prepare 时执行。已有审核、enqueue、worker、
plan/job/result/report GET 仍为唯一业务通道；后续 C11 模型也只能输出同一候选 DTO。

目录/详情/匹配结果由服务端按真实用户和全部步骤能力过滤；prepare、审批、执行、回读分别
实时校验 owner、实际资产范围、动作权限、当前定义/模板/目标/开关，不信任请求 role。
匹配本身不是授权；越权/停用不能回退成另一条可执行流程或缩小为已授权子集合后偷偷运行。
非法 schema 422，未有权限 403/对象不可见 404，未知版本 404，版本/hash/幂等/源状态冲突 409，
存储或审计失败 503 且不产生成功 receipt；实际 HTTP 状态由主代理统一映射。

手工入口：授权目录选固定版本 → 明确参数与目标（含默认来源）→ 查看固定步骤/风险/停止边界 →
prepare 并持久回读同一 ID → 复核/必要独立审批 → 明确执行/入队 → 使用同 ID 看实际结果。
“查看原结果”“重新运行”“恢复暂停”是不同命令；刷新、换页、AI 离线或网络失败都不触发新执行。
前端保存原请求 key 和冻结输入摘要，遵守现有 auth login epoch 隔离；丢回包先查原 receipt，
403/404/409/503 显示明确错误不变空数据或成功。专业参数表单与当前居中确认视觉保持不变。

旧 PlanCreateInput 的 extra=forbid 不放宽，原 /plans、/capabilities 与批次 v1/v2/header 合同不变；
旧 route-intent 保持导航/诊断分诊，不能把 workflow 回包硬塞旧 action=navigate 字段。
未来接入新路由时，先覆盖本设计五类确定性意图；未覆盖请求仍明确导航/补参，不能误走旧自动动作。

## 9. 验收与实施次序

1. 本轮先交设计与主代理工具消息。随后可独立新增纯静态 workflow_library.py 和单测，
   不改任何既有 plan/API 主文件；先验证目录/槽位/哈希/决策确定性，不把它称为 C10 已接线。
2. 主代理实现持久 binding、原子 preparation、审计/权限/HTTP，不复制执行器；用隔离 SQLite
   真表测试版本冻结、审批撤销、存储失败回滚和丢回包原 key 回读。
3. 前端无模型手工联调及浏览器验收通过后，才能报告本范围 C10 闭环；其余能力不因登记而开放。

| 层次 | 成功与失败必须证明 |
| --- | --- |
| 静态库 | 同输入跨进程同版本/hash/步骤；代码目录返回值不能被调用方修改；未知版本/未知槽位/bool数值/越界拒绝；无 handler/SQL/动态导入执行入口 |
| 匹配 | 同义明确输入同 recipe；导航/查看不触发采集；否定/混合指令/多引擎/隐式全部/缺目标/缺模板补参；未接报告生成不假支持 |
| 持久版本 | 新 plan/revision 存完整 recipe；改参/改目标/改版本使审批失效；旧版本退役仍可读；legacy 不补造关联；并发与原子回滚无半批次 |
| 身份与范围 | 伪造role/actor/steps/hash拒绝；目录与全部步骤权限一致；owner/operate 撤销立即生效；多目标一项越权全批拒绝；跨用户原 key 不泄露 |
| 查看/重跑 | 查看与刷新零新job；同key同输入回原ID、变参冲突；丢回包只查原key；明确rerun新plan/新job且旧产品不变；未知/暂停源不重放 |
| HTTP/前端 | 严格body与重复key header拒绝；AI禁用时手工全部完成；403/404/409/503与无结果区分；刷新恢复原ID；前端旧v1/专业页面不回归 |

当前未执行上述 C10 实现/HTTP/浏览器验收，无现场数据库/模型/Agent、迁移、平台重启或 commit/push。
本文件同时是工作区归档稿；受本轮写集/权限限制，未写 Obsidian，未更新内置记忆。

交接状态：已尝试用 app send_message_to_thread 向主代理发送本设计；工具拒绝向 native ancestor
发送，要求改用当前会话的原生协作消息或最终结果。本会话可用工具未暴露原生发送入口，
故不能确认工具消息已送达，按工具提示以最终交接返回；尚未新增静态服务/单测，未越过发送前置。

## 10. 第一阶段实现与冻结交接（2026-10-20）

主代理随后已实际读完设计并明确批准纯静态服务与独立单测；以上“尚未新增”是上一轮历史状态，
不是当前状态。本轮只新增 workflow_library.py、test_workflow_library.py 并接续本文件，
没有修改既有 registry、plan、admission、API、权限、前端或迁移。无须跨 thread 工具消息，
由本轮最终交接给主代理。**实现仅为静态依赖，不完成持久版本、受理或 C10 完整验收。**

### 10.1 实际服务合同

四个函数签名保持第 5 节所列；没有 actor/db/session/clock/handler/callback 参数。
异常为 WorkflowError(ValueError)，有 code 与 fields: tuple[str, ...]，不含原始非法输入值。
关键错误：workflow_not_found、workflow_invalid_reference、workflow_definition_changed、
workflow_matcher_not_found、workflow_invalid_text、workflow_invalid_json、workflow_input_too_large、
workflow_unknown_field、workflow_invalid_input、workflow_invalid_params、workflow_invalid_scope、
workflow_duplicate_target、workflow_slot_conflict。具体 HTTP 状态仍由主代理统一映射。

- get_workflow：schema=capability.workflow/v1，含 ID/version/hash、slots_schema、scope_modes、
  固定 steps、failure_policy=stop、max_targets=100、valid_for_seconds=3600、request_approval、
  runtime_checks_required=true 与 release_gate。schema 直接来自原 PARAM_SCHEMAS 的 Pydantic
  输入模型；作业 CSV 的 x-csv-values 直接取 jobs.ALL_STATUSES / run_status.OUTCOMES，
  不从计划层猜词表，不接受 succeeded/completed 等错误域别名。
- list_workflows：schema=capability.workflow_catalog/v1，items 按五个 recipe 固定顺序。
  这是未经授权过滤的内部静态目录，不能直接作为“当前用户可执行目录”对外发布。
- match_workflow：schema=capability.workflow_match/v1，含 matcher_version=1、accepted=false、
  normalized_text、decision、operation、workflow（仅 ID/version/hash 或 null）、missing_fields、
  reason_code。不返回 plan_inputs；裸“任务”返回 needs_input，即使知道可能的 recipe 也不设置
  prepare 操作。导航没有执行输入；已有结果只给 view_existing+result_ref 缺项。
  否定/条件先补参，多操作/多类对象/多引擎歧义拒绝选择；停止/恢复/取消返回 rejected，
  reason_code=workflow_control_not_supported，交由原 C07 明确控制通道，不当作新采集。
  未实现的报告生成/比较/导出为 rejected+workflow_unsupported，不造报告 handler。
- compile_workflow：schema=capability.workflow_candidate/v1；accepted 恒 false，含完整
  workflow_binding={workflow_id,workflow_version,definition_hash,recipe_snapshot}、candidate_hash、
  missing_fields、原 slots/scope 的独立副本、effective_inputs 与 plan_inputs。
  每个 effective_inputs 项为 {instance_id,step_params:[原模型归一化值]}。
  每个 plan_inputs 项含原创建器可接的 capability_id/definition_version/instance_id/steps/
  valid_for_seconds/request_approval。steps 含 seq/title/capability_id/definition_version/params/
  depends_on/input_refs/fanout/retries；没有任意步骤输入入口。缺任意一项目标/参数时整个
  plan_inputs/effective_inputs 都是 []，不返回可误用的半批候选，不填假 policy_id。

三读组合公共及逐目标 slots 均为 {matrix:{...},templates:{...},jobs:{...}}，各组使用原模型。
可以跨公共/逐目标层补不同叶子字段；同一叶子字段重复出现即拒绝，即使两值相同。
单读/采集直接使用平铺原字段。采集只公开 policy_id/timeout_seconds；原冻结 hash 字段
一律未知字段拒绝，不替服务端冻结器填 null。global 只允许前三个独立读取 recipe；组合与
采集仅 explicit。仅缺 scope.mode/targets 返回补参；非法目标、重复项、越界与未知键明确拒绝。

candidate_hash 纳入固定 recipe/hash、scope mode、按目标顺序排列的实际默认值/步骤参数；
显式填写默认值与省略默认值、公共参数与等价逐目标参数位置得到相同业务 hash。
原模型接受的 CSV 拼写按原值保留，不额外猜测别名；因此 CSV 顺序/空白没有隐式等价合并。
改变参数、目标顺序或 recipe 身份会改变 hash。hash 不是服务端 receipt、授权或审批凭证。

内部 recipe/step 使用 frozen dataclass+tuple，版本 hash 表也是 tuple。返回符合 ->dict 的普通
JSON 树，调用方可修改自己的副本，但不能修改库定义或后续返回值；binding 与各返回字段也不
共享可变子对象。这是“不可变目录+深复制返回”，不是不可序列化的 MappingProxy 响应。
每次 get 都重新核验当前 schema/词表，无缓存窗口掩盖漂移；编译也经过此核验。

### 10.2 固定 v1 身份及升版门禁

| workflow_id | workflow_version | definition_hash |
| --- | --- | --- |
| inspection.jobs.list | 1 | 35b844072e1937d6226361b9d74b44da7e5ca0e8358db7e2d43fbefdf8ce5453 |
| inspection.templates.list | 1 | aaf90e4f66069c1ca282b40f2d55b5c83a783a53c7a4852dd18126604bcef6ed |
| operations.capabilities.list | 1 | 6293c4bd02bf18aa642f614ecf7099bc9d767addbc0e565aa73e01dfc7ca39b3 |
| inspection.context.read | 1 | eee95f21c7ffa0f1749536897e4141cc408f18aeb170baaea27aba1d3e060f57 |
| inspection.collect | 1 | c6162a7a6c67e1c181305220ca978f423e1913ecb0698f875bcec5586d238c30 |

get_workflow 在返回前计算定义 hash，并对照源码固定 (ID,version)→hash；不同则抛
workflow_definition_changed，不得返回“v1 新含义”。测试既固定上述完整基线，也分别修改
模型 schema/status/outcome 集合，验证曾读取后的后续 get/compile/match 仍拒绝漂移，旧副本不变。
将来增加版本应新增条目及相应历史 schema/语义支持，不替换旧 hash；若原参数模型已不支持
旧定义，旧版本拒绝新编译，历史仍从已保存快照回读，不能凭今日模型重建旧版本。
此门禁覆盖定义/schema/词表漂移，不等于校验依赖库全部源码；validator/匹配器/编译算法等
语义变化即使 JSON schema 不变，也必须人工升相应 workflow/matcher 版本并补回归。

输入 budget：JSON 树深度最多 12、节点最多 50000、字典宽度最多 128、单 list 最多 1000、
单字符串最多 16000 字符、累计字符串最多 200000 字符、最终规范 JSON 最多 200000 字符。
结构预检先限预算再序列化，拒绝非字符串键、非 JSON 对象、NaN/Infinity；scope 另有 100
目标硬上限，instance_id 使用 type(value) is int 与 1..2**63-1，bool 不作为 ID。
文本规范化前后均限 4000 字符，NFKC+casefold+折叠空白；除换行/tab/回车外拒绝控制/格式字符。

### 10.3 实测与未完成边界

执行环境为本机 .venv-mac-primary，PYTHONPATH=backend、DATABASE_URL=sqlite://、
DB_TYPE=sqlite、APP_MODE=quality_test、DEBUG=false、PYTHONDONTWRITEBYTECODE=1；
pytest 禁用 cacheprovider。原计划测试使用自己的 tmp_path 隔离 SQLite，不连接配置平台数据库。

本轮联合测试：test_workflow_library.py + 原 test_capability_plan.py，**164 passed in 20.31s**。
最终独立复跑 test_workflow_library.py：**124 passed in 1.24s**，子进程全部正常退出，未留测试句柄。
check_backend_static.py：**507 文件，0 finding**。
包含固定 hash 基线/运行时漂移、真实 capability 版本与输入模型、domain success/unknown 过滤、
非法别名、类型/预算/目标/冻结字段、缺参无半候选、100 目标三步 DAG、深复制、跨 hash seed
独立子进程一致性；运行时把 Engine/Session/网络/时钟/registry 执行及 plan 创建执行入口置为
禁止访问，四个纯函数仍可完成。新进程导入+编译亦禁止 DB connect 和 socket connect。

**仍未完成且必须由主代理后续验收：** 持久 workflow binding/revision/request_hash/审批绑定、
原子 preparation/幂等回查、实时权限与资产/模板/开关核验、真实 source_plan 重跑约束、HTTP
及前端手工/浏览器流程。未注册新 API、未新增表/迁移、未放行真实采集，也未验证现场/MySQL
隔离级别或部署效果。没有平台重启、现场访问或 commit/push；未写库外 Obsidian。

## 11. 持久服务接续（2026-10-20）

本轮写集仅新增 backend/app/services/workflow_service.py、backend/tests/test_workflow_service.py，
并追加本节；未修改 plan_service/validation/governance、模型、plan_admission、HTTP 或权限范围。
workflow_binding.make_binding 和 core 的 workflow_binding 内部参数由 Einstein 提供，HTTP 由
主代理接线。本节更新第 10 节的“纯静态阶段”边界，不回写或冒充此前已完成持久集成。

### 11.1 实际服务签名

```python
list_workflows(db, *, actor) -> dict
get_workflow(db, workflow_id, workflow_version, *, actor) -> dict
match_workflow(db, text, *, matcher_version=1, actor) -> dict
prepare_workflow(db, workflow_id, workflow_version, *, actor, idempotency_key,
                 slots, scope, operation="prepare", source_plan_id=None) -> dict
read_preparation(db, *, actor, idempotency_key) -> dict
revise_workflow(db, plan_id, *, actor, expected_revision, slots, scope) -> dict
```

异常复用 plans.PlanError(code) / workflow_library.WorkflowError(code)，不引入新的未捕获异常类；
registry 原异常仍走既有 CapabilityError 映射。绑定 helper 的异常包装为 PlanError。

### 11.2 选择层与历史层分离

- list/get/match 先核真实 principal，按全部步骤真实 action 与共同授权 engine 判断，不能只查
  主步骤/请求 role。再逐步调用 effective_capability，核当前 capability version、availability、
  unavailable_reason 和 plan_executable；list 过滤不可用 recipe，detail/match 返回原 typed
  阻断码，不能改选另一流程。GET 不 sync/seed，不因未配置自动创建 registry 行。
- 不修改纯 DTO 或 definition hash，不新增“ready”等字段。目录在权限和可用性上过滤，但仍不是
  目标授权、审批或受理；match 仍 accepted=false、无 plan_inputs，prepare 前仍须目标级复查。
  此规则替代本轮早期“可见但 prepare 拒绝”的选择策略；不将 disabled/未发布流程作为可选项。
- 历史 receipt 不调用今日 get/compile/可用性目录；使用持久单计划 binding、revision 和快照
  自校验，仍核当前 owner/每步 action/实际资产权限。停用不使历史回执不可读，撤权限仍拒绝。
  既有 plan/revision 历史 GET 没有在本服务增加目录依赖；新 revise 是写操作，仍要求原固定版本
  当前可用，不能借“读历史”绕开创建/修订门禁。

### 11.3 原子创建、哈希与回执

prepare 全目标先调用原 compile_inputs/freeze_steps/_check_plan（含真实 actor），不复制执行器。
显式 engine/db_type 过滤与实际目标冲突时拒绝，不替换目标；缺参数返回 workflow_missing_input，
不创建 settled/needs_input 占位计划。core 创建后若仍出现 blocked 等非 validated 状态，返回实际
outcome_kind 并整体 rollback，不能把“保存被阻断清单”当 prepared 成功。

每计划绑定使用 make_binding(candidate,index)，仅保留本目标归一化业务输入、固定定义及 hash；
单计划回读不会携带同批兄弟目标。请求身份为：

```text
input_hash = hash({family: "capability.workflow_preparation/v1", workflow: ID/version/hash,
                   candidates: [每计划 canonical workflow_binding，按原位置],
                   operation, source_plan_id})
request_key_hash = hash({request_key: 原 key})
client_plan_key[i] = hash({batch_key: 原 key, position: i})
stored_plan_key[i] = hash({actor: 真 owner ID, client_key: client_plan_key[i]})
```

candidates 含归一化单目标候选 hash、有效默认值和 scope，因此等价公共/目标槽位及默认值拼写
不串成新业务，目标顺序仍参与身份。family 与实际 binding 双重验证，旧 C06 receipt 不能读成
workflow receipt；共用 key 跨 family 输入冲突拒绝。查到原 receipt 还须 Python 严格比较原 key，
不同大小写不借数据库 collation 错绑。未变更原 preparation 表/唯一约束或旧 C06 global 合同。

使用原 DBCapabilityPreparation 表；core create_plan(commit=False,admission_hash=input_hash,
workflow_binding=本目标 binding)，所有计划/revision/receipt/审计在一个事务一次 commit。
prepare 锁序 state→create→audit，重跑证据只读 queue；revise 复用治理层 state→queue→audit。
持锁期间仅本地有界冻结和平台库操作，无现场 I/O/执行/入队。dirty session 写入口拒绝，避免
把调用方其他未提交改动一同提交。提交前失败 rollback 全部本轮写入，审计失败不吞。
若 commit 结果或响应不确定，rollback 不能证明尚未落库，必须按原 key 回查 receipt，不换 key 猜测重发。

read_preparation 根据真 owner+原 key 找 receipt，校验原位置 plan key、admission_hash、
revision/binding_hash、所有单计划 workflow 元数据一致，再从真实 parent 推导 operation/source，
重算 family 请求 hash。GET 不写，不查今日 recipe，也不按同参数猜另一份 receipt。

响应 schema=capability.workflow_preparation/v1，字段严格为 schema/accepted=false/status=prepared/
preparation_id/workflow/operation/source_plan_id/total/items/notice。workflow 仅 ID/version/hash；
items 是原 SUMMARY_FIELDS 十字段，仅 instance_id 允许 global 的 null。reused 是回读投影，
prepared 不表示审批、入队、业务成功；global 历史不以假数值目标兼容旧 v1。

### 11.4 重跑、修订与审计

rerun 必须一个真实 owner 源计划，编译恰好一计划，canonical 单目标 binding 与源相同；core
再次核真实 business request_hash/目标指纹及 parent 唯一链。源必须 settled 且有 queue._evidence
核验过的真实终态产品；running/paused/stopped/reconciliation_required、unknown 或 partial 内含
unknown 全部拒绝，不仅凭 outcome 枚举放行。prepare 不接 source；rerun 不作为 resume，
不清除 unknown、不释放目标、不操作外部任务。新 key+源得到新 child；同 key 重放先回原 receipt，
不因源已有 child 误判；第三次显式运行以已完成 child 为源，保留唯一父子约束。

revise 从原持久 binding 推断固定 ID/version，仅编译一个目标；原 hash 漂移拒绝，调用
governance.revise_plan(...workflow_binding=...)。CAS、活跃 queue 拒绝编辑、旧批准清除、revision
保存和 revise_plan 同事务审计都复用治理层，没有提前 commit、第二次审计或另一套审批。
修订后旧 receipt 绑定不再匹配，回原 key 返回 preparation_binding_changed，不偷换成新 revision。

prepare 审计用 append_security_audit，记录真 actor、workflow 身份、operation/source、input_hash、
各 plan 的 ID/revision/binding_hash，并与 receipt 一起提交。None/异常 fail closed 为
workflow_audit_unavailable；存储异常为 workflow_storage_unavailable。治理层审计错误回滚 CAS
及全部 revision/批准状态；service 不绕过或重复其审计。

### 11.5 错误与集成建议

| code | HTTP 建议 |
| --- | --- |
| workflow_not_found / workflow_matcher_not_found / workflow_source_not_found / preparation_not_found | 404 |
| permission_denied / scope_denied | 403 |
| workflow_missing_input | 422 |
| workflow_definition_changed / workflow_binding_invalid / workflow_input_mismatch | 409 |
| workflow_rerun_source_unsettled / rerun_request_mismatch / parent_plan_already_retried | 409 |
| idempotency_conflict / preparation_binding_changed / plan_revision_conflict | 409 |
| capability_disabled / capability_not_released / definition_not_synced / definition_out_of_sync / stale_definition_version / handler_not_wired | 409 |
| workflow_storage_unavailable / workflow_audit_unavailable（及原 plan_audit_unavailable） | 503 |

非法 typed body/槽位沿现有 400/422 策略；未新增任意 handler/steps/actor/冻结 hash 输入。
主代理当前 _guard 尚可决定 workflow_input_mismatch、capability_not_released/handler_not_wired 的
最终映射，本服务不改 API。preparation_dirty_session 沿旧 receipt 防污染合同，不吞掉调用方改动。

最终有效回归与冻结摘要接本节末尾；早期 289 项联合通过发生在选择可用性门禁补充之前，
不能替代补充后的最终复跑。无现场库/模型/Agent、平台迁移或重启、commit/push；未写库外知识库。

### 11.6 最终有效验证与冻结

选择可用性门禁及 fixture 修正后的全新进程联合：test_workflow_service.py、主代理
test_workflow_http.py、Einstein 的 test_workflow_binding.py、原 test_workflow_library.py，
**228 passed in 70.56s**。最终 check_backend_static.py：**509 文件、0 finding**。
新增 service/test 及本文 whitespace 检查无发现；测试、重启回读子进程均已退出，无遗留活测试句柄。

覆盖真实平台 SQLite 表、全目标原子冻结、默认值防重、跨 family/key/owner/顺序/revision 绑定、
同 key 两线程、独立新进程找回丢响应、audit/commit/第二计划失败回滚、晚权限/能力撤销、
真实终态重跑链、partial 含 unknown 保留、真实本地 pause/stop checkpoint 不当 rerun，
以及 collect fixture 必要审批、改参后批准失效且零采集/零 job。新增目录门禁覆盖
disabled/unsynced/out-of-sync/release gate、组合任一步阻断、GET 零 DML及停用后历史 receipt 回读。

此前更大范围原 plan/governance/admission 联合为 289 passed，但在本轮最终目录门禁前，
作为历史回归保留，不与上述 228 相加，不冒充最终源码全仓验收。

冻结 SHA-256：

```text
workflow_service.py       2e27ccd44606727af65b738a7bacd8c04cec0918bd39644c727decd54a6ef76c
test_workflow_service.py  2f0d6a1ef0e398c4dab5707fe3ba548d17af7f2af1f9b49a6b76c5bf91af7984
```

残余：主代理最终 HTTP 错误映射/前端手工与浏览器验收仍按其写集收口；本轮联合 HTTP 不等于
前端闭环或现场放行。真实 MySQL RepeatableRead/当前读/不同大小写 key、客户库升级、
性能/长稳仍待 C15/C16 独立实机验证。没有更改迁移、平台参数或默认关闭的采集发布开关。

## 12. 全流程浏览器矩阵接续

本节晚于上述服务冻结记录。API/手工 UI 和首批 jobs/global 浏览器已经接续，证据见
[总实施记录](c01-c16-execution-2026-10-20.md)，但 **C10 仍未全部验收**。2026-10-20
只读复核现有脚本与夹具发现：不能用首批 jobs、换账号隐藏和保存报告回读代替五流程矩阵。
本节新增的是待执行设计，没有新浏览器 PASS，不重跑或接触正式 3300/8801。

| 流程 | 待补场景 | 审批与范围边界 |
| --- | --- | --- |
| `inspection.jobs.list` | 分页/状态/结果/触发方式、单/多目标、专用修订、同输入重跑、各阶段关窗/刷新/晚回包 | 可 global 或显式范围；recipe 不强加审批 |
| `inspection.templates.list` | enabled 严格 bool、空结果与失败分开、模板不隐式 seed、修订/重跑及会话隔离 | 可 global 或显式范围；读取不强加审批 |
| `operations.capabilities.list` | 引擎不匹配/未支持、真实矩阵回读、修订/重跑及迟到请求隔离 | 只读说明，不执行矩阵里的动作 |
| `inspection.context.read` | 固定三步/依赖/顺序、嵌套槽位、修订/重跑与报告引用 | 仅显式范围；三次读取不伪称原子快照 |
| `inspection.collect` | 实际独立审批/撤销/过期/变更失效、专用修订后重新审批、实际 job/result/report 链、partial/failed/unknown | 仅显式目标及已确认模板，原发布开关/独立审批不能绕过 |

每条适用流程都需覆盖：准备/修订/入队提交后丢响应只原定位 GET、双击一次、关窗不假停止、
刷新找回、旧确认不跨身份、目录停用时仍能核对已保存修订但不能新执行、未知运行拒绝重跑。
1440px/390px 对实际表单、目标选择、审批和结果状态逐项截图，不只检查首个结果表。
通过项必须绑定 actor/scope/recipe/version/输入/原 key 摘要/实际 HTTP 数量和持久引用。
合成 403/504 是 UI 失败路径，不能代替真实权限/提交证据；报告渲染不等于导出成功。

当前隔离夹具还缺采集 owner 写权限、有效模板与独立 worker 的 collector/网络隔离保证。
下一批应扩充专用隔离夹具，复用真实队列、writer、规则及报告签名，只替换明确标记的采集器；
API 和 worker 两端都禁止真实 DB/Agent/模型调用。不得用现有默认读身份加权限后冒充最小
权限验收，不导出或记录 session/token，不重置已有验收库，也不把另一组报告夹具当采集闭环。
准备材料在 `/tmp/c10-workflow-matrix-plan.md` 和默认拒绝执行的脚本草案；仅语法检查，
不是已运行证据。正式记录必须逐流程逐场景给 PASS/FAIL/NOT_RUN 与原因。
