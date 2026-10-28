# C11 结构化模型候选校验器前置合同

日期：2026-10-20。来源：[U10](pending-development-backlog.md#u10-可选-ai-清单编译与降级p1c11c12)、
[C11/C12](capability-card-task-plan.md)、[C10](c10-workflow-library-2026-10-20.md)，以及现有
workflow_library/workflow_service/ai_config 源码。Obsidian《功能卡片与可选 AI 编排》仅作历史
原则参考：AI 可选、手工同源、模型不掌握控制权，当前实现事实以本轮源码/测试为准。

**本轮只新增纯函数、独立测试和本文，不修改既有文件，不接 API/UI/provider/计划通道。
这不是 C11/C12 已接线或已完成声明。** 本节先冻结接口，再实现并在末尾追加实测记录。

## 1. 源码边界

- workflow_library 是固定五 recipe 的纯编译器，定义 hash 有运行时固定版本门禁；返回候选
  accepted=false。保留三读取 @1、三读组合，以及 inspection.run.execute@2，不另造执行器。
- workflow_service 才会检查真实角色/资产/registry 可用性、冻结/持久化/审计；历史 receipt
  和重跑语义也属于该层，不能因为模型声称 matched 就跳过。
- ai_config 现有 provider 函数会发送网络请求；Ollama JSON 模式还可能先 /api/show 探测容量。
  其诊断 JSON 校验合同不同，不能代替本校验器。本模块不导入或调用 ai_config，不复用其宽松
  Markdown JSON 包装处理，不修改配置/端点/默认 provider 或发布开关。
- 校验器无 actor/db/session/handler/provider 参数，不鉴权、不读配置、不查目标、不取时间、
  不调网络/模型、不创建/执行/控制计划。授权目录的来源真实性和时效性由受信调用方保证；
  纯函数不能证明它没有伪造或过期，输出不得标记 authorized/approved/executed。

## 2. 纯函数合同

```python
validate_proposal(raw_text: str, *, catalog: dict, scope: dict,
                  allowed_target_ids: list[int], allowed_policy_ids: list[int],
                  confirmed_policy_id: int | None = None,
                  validator_version: int = 1) -> dict
```

异常沿 workflow_library.WorkflowError(code, fields=...)，不用 plan/数据库异常或新执行通道。
validator_version 仅支持整数 1，不接受 bool、字符串或自动 latest。

### 2.1 受信调用方输入

catalog 必须是 workflow_service 已按真实身份和当前可用性过滤过的
capability.workflow_catalog/v1 快照（schema/items），不是模型输出。只接受当前固定代码支持、
完整定义/hash 一致、无重复 ID/version 的条目；被排除的 recipe 即使静态代码存在也不能被选择。
目录最多 32 条，空目录合法但任何具体 workflow 都不允许。不得用未经授权的纯静态目录直接
接 HTTP；单测的纯静态快照只是受信输入 fixture，不是权限验收。

scope 由用户明确确认后由调用方传入：global 只允许 {mode: global}；explicit 为不重复、按原序
1..100 个 targets，每项 instance_id，可选 slots={policy_id: 已确认模板ID}。每个显式目标都必须
位于 allowed_target_ids；不能从允许集合自动选择第一台/全部资产，不能从模型文字提取目标。
{} 或 explicit 缺/空 targets 表示尚未确认，结果 needs_input，不生成计划输入。
global 是明确选择，不是有限 allowed_target_ids 的隐式扩大；调用方仍须核全局读取权限，后续
workflow_service 必须再次实时检查。纯函数无法从一个 ID 列表证明 global 授权。

allowed_target_ids/allowed_policy_ids 是调用方核实的可选 ID 白名单，不表示用户已经选中；
最多各 1000 个、唯一严格整数 1..2**63-1，拒绝 bool、字符串和重复项。common 模板选择由
confirmed_policy_id 提供，或每目标 scope.targets[i].slots.policy_id 提供；必须属于模板白名单。
即便仅有一个允许模板也不默认选择。common/local 同时提供模板拒绝（值相同也不覆盖）。
巡检缺确认模板返回 needs_input；读取 recipe 携带已确认模板拒绝，不悄悄丢弃用户选择。

### 2.2 不可信模型输出

matched 必须是且仅是：

```json
{"decision":"matched","workflow_id":"inspection.collect","workflow_version":1,"slots":{"timeout_seconds":900}}
```

needs_input 可只输出 {decision: needs_input}，或加完整 workflow_id/workflow_version 对和 slots。
not_matched 可只输出 {decision: not_matched}；允许显式 null 引用和空 slots，但不能同时给一个
具体流程或非空参数。引用必须成对且类型严格；matched 不接受 null；needs_input 不因参数碰巧
齐全被升级 matched。无具体引用时 slots 必须为空，不保留悬空参数供后续自动选配。

根字段仅 decision/workflow_id/workflow_version/slots；拒绝 scope/targets/actor/审批/SQL/
handler/URL/source_plan/operation/reason/confidence/任意步骤/定义 hash/模型自报 metadata。
slots 按原 recipe 输入 schema 白名单及原 Pydantic 模型验证，不猜 status/outcome 等词表。
模型不能填写 policy_id（即使与真实选择相同），也不能写服务端冻结 hash。
首版进一步禁止 trigger_type：它在原读取模型里是自由字符串，没有可复用固定词表；不靠
手写 SQL/URL 正则把自由文本变安全指令。手工 C10 仍可沿原模型显式填写，不改其合同。
其余字符串槽位只有已有模式/固定域词表，数字槽位沿原严格模型，未知 SQL/URL 字段及把这些
内容塞入 status/outcome/engine/db_type 都失败；不引入任意表达式解释器或 handler 入口。

### 2.3 解析与预算

只接受完整 JSON 对象文本，不剥 Markdown fence，不提取子串、不修复/补全截断 JSON、不接受
多段对象/尾随文字。raw 最多 16384 UTF-8 字节；先限制字符数及编码字节数，再扫描括号深度，
忽略字符串中的括号/转义，最多 12 层，然后 json.loads。使用 object_pairs_hook 检测所有层
重复键，parse_constant 拒绝 NaN/Infinity，树校验再拒绝数值溢出非有限浮点。
严格拒绝控制/格式/代理字符，包括 JSON 转义后隐藏的字符；标准 JSON 外部空白允许。
raw 树最多 4096 节点，其余快照树最多 12000 节点；对象宽度 64、list 最多 1000，字符串
单项最多 4096 字符/累计 65536 字符，规范 JSON 最多 131072 字符，整数预检最多 128 bit。
目标/模板再按前述 ID/数量收紧；不能让纯函数以无限遍历代替限流。
错误只含稳定 code/固定字段路径，不回显 raw 文本、未知键值或 provider 响应，抑制包含原始
校验值的异常 cause 展示。支持现有 AIProviderText 风格 str 子类，但只读取其内建字符串内容，
忽略 metadata，不由模型或包装对象的“approved”提示授权；真实 transport provenance 由后续调用方另管。

## 3. 编译与返回

引用必须同时在当前支持代码和传入快照中，定义/hash 完整相同；没有候选别名回退。
模型只提议业务槽位；校验器合入调用方已确认模板，沿 workflow_library.compile_workflow
生成同一固定步骤，不接任意步骤/handler，不新建第二执行器。确认目标顺序必须保留。

响应 schema=capability.workflow_proposal/v1，含 validator_version、accepted=false、
model_decision、decision、workflow（ID/version/hash 或 null）、missing_fields、candidate、
catalog_hash、proposal_hash、validation_scope=structure_only、requires_confirmation=true。
只有 model_decision=matched 且原编译无缺项时 decision=matched 并携带原 C10 candidate。
needs_input/not_matched 时 candidate=null，不能泄露可被误提交的完整 plan_inputs；缺项由代码
计算，模型 needs_input 且参数完整时仍缺 intent_confirmation，不擅自升级为可提交候选。

所有返回都是独立深复制；哈希用严格规范 JSON，无时钟、随机值或进程散列种子影响。
catalog_hash 绑定授权快照（不受目录顺序影响），proposal_hash 绑定规范模型对象、目录 hash、
已确认 scope/模板和允许 ID 集合。它们只作核验追踪，不是 preparation/admission 幂等 key，
更不是持久 receipt、审批、权限或“模型意图语义正确”的证明。

未来确认提交必须再进 workflow_service 的当前权限/版本/目标/模板/开关检查，之后才可能
prepare→复核/必要审批→显式 enqueue→原 worker。模型候选校验绝不隐式选择 rerun/source，
更不处理 pause/resume/stop/接管/对账；那些只能使用已存在的人工控制合同。

## 4. 未来 Provider 接线门禁（本轮不实现）

1. 在服务端先确定唯一、获准的 provider/config/model/endpoint，固定 digest，明确用户外发授权。
   AI disabled 或未授权时不发送；禁止把模型响应、用户 URL 或目标配置变成下一个推理端点。
2. 仅发送脱敏任务描述、授权可用流程合同及必要无秘密上下文；不发送凭据、连接串、原 SQL、
   现场结果/报告正文、内部主机地址、token 或跨用户资产。目标/模板可选 ID 只作本地确认，
   默认不发送真实 ID/完整资产；外发内容应有独立可核对 consent/digest，不用“已配置”代替同意。
3. 一个候选请求最多一次推理、选定一个端点；不自动重试、改 provider/model/URL、跟随重定向或
   用第二个模型修 JSON。Ollama /api/show 等预探测不是“免费的一次推理”，若保留也须纳入
   显式授权端点/请求数和统一预算，不能直接把现 ai_config 当作已符合此合同。
4. 统一 request 生命周期的 wall deadline、连接/读超时、输入/输出字节与 token、并发/速率预算；
   流式截断、超时、断开、非法输出都失败关闭。不能分 provider 各耗满预算或后台继续无限推理。
   此纯校验器不读时钟/实施网络 timeout，本轮只提供解析/结构预算，不能冒充这些已实现。
5. 日志/审计只记录实际 provider/config/model 版本与 digest、输入/输出 digest、校验 code、
   使用预算与人工后续动作，避免保存原秘密文本；服务端同意、来源和真实受理分别记录。
6. AI 离线、禁用、被撤权或失败均保留手工流程表单和既有已冻结任务；不重提执行、不换 key
   猜是否受理、不把 unknown 清除成未发生。用户确认候选也不能绕审批/业务权限/控制 fence。

## 5. 验收范围

本轮独立纯单测覆盖五 recipe/真实参数模型、受限目录、严格 JSON/重复键/预算、否定性
decision、越权目标/未确认模板/禁止字段、候选深复制、跨进程确定性与 DB/网络/时钟/模型/
执行器禁止调用。目录来源用受信 fixture，不冒充真实鉴权或 provider 调用验收。
最终原始命令、测试数字、静态门禁和未完成项在实现验证后追加；未调用现场或提交。

本文件也是写集内归档稿；本轮不写库外 Obsidian 或内置记忆，不修改 C10 历史设计文档。

## 6. 实现、原始验证与冻结

已按上述合同新增 workflow_proposal.py 和 test_workflow_proposal.py；没有编辑任何现存文件。
输出与解析错误均复用 WorkflowError，主要新 code 为 workflow_proposal_ 前缀的
validator_not_found、invalid_json、budget_exceeded、duplicate_key、forbidden_field、
invalid_decision、invalid_reference、invalid_slots、invalid_catalog、catalog_changed、
invalid_selection、invalid_scope、selection_not_allowed、selection_conflict、not_allowed。
固定版本漂移、原业务槽位校验等保留原 workflow_definition_changed/workflow_invalid_params/
workflow_invalid_scope 等 code，不把原编译器拒绝软化为“模型没命中”或自动重试。

原始执行命令（工作目录 /Users/zhaolongjie/codex/mesdbtools）：

```sh
env PYTHONPATH=backend DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false PYTHONDONTWRITEBYTECODE=1 .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_workflow_proposal.py
env PYTHONPATH=backend DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false PYTHONDONTWRITEBYTECODE=1 .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_workflow_proposal.py backend/tests/test_workflow_library.py backend/tests/test_workflow_binding.py
env PYTHONDONTWRITEBYTECODE=1 .venv-mac-primary/bin/python scripts/check_backend_static.py
```

最终独立：**134 passed in 1.61s**；联合：**298 passed in 14.68s**；静态检查：
**510 文件，0 finding**。三个新文件 whitespace 检查无发现。数字范围有重叠，不相加。

证据来源严格区分：

- 新增 134 项只使用受信目录/选择 fixture 和原参数模型，不创建任何数据库或计划，也不调用
  workflow_service 做真实鉴权。通过禁用 Engine.connect/Session、requests/socket、clock、
  AI provider、workflow_service、plan 创建/执行与 handler 入口，验证纯函数仍可完成。
- 新增独立解释器在数据库 connect/网络 connect 禁止条件下编译，两个 PYTHONHASHSEED 的
  输出和 hash 一致；没有读取真实模型响应或联系模型端点。provider 风格 str 包装测试仅用
  本地合成包装类，不 import provider 调用或信任 metadata。
- 联合范围中原 test_workflow_binding 的平台库/创建/执行验证使用其自己的 tmp_path SQLite
  及现有 collector fixture，与新增纯校验器的“无 DB/执行调用”断言分开；不是现场采集或
  C11 的持久/权限/provider 集成证据。
- 原始源码核对为 U10/C11/C12、当前 workflow_library/workflow_service 及 ai_config 的配置/
  provider/transport 边界；没有以 Obsidian 早期未实现条目覆盖当前共享代码事实。

冻结 SHA-256：

```text
workflow_proposal.py       1e99d978cfcd185a6f019c2b3391e88d8b46f9ec2001fe7aafba37bcacaf8561
test_workflow_proposal.py  f2aeca6c5bc7a32e31ee8b2a245b91837637c5d4ea883122213565eddd83b715
```

仍未实现：provider 选择/明确外发 consent/脱敏、统一真实 wall deadline/请求数/token/并发预算、
真实 transport provenance/审计、HTTP/UI 候选预览与显式确认、模型不可用降级接线，以及再次
实时授权/持久 prepare/receipt 的联合路径。本函数不能识别“模型把查看误判采集”等语义错误，
也不能证明快照还新鲜；必须保留人工确认及原服务端当前校验，不得直接据 matched 入队。
本轮不代表 C11/C12 完成，未改 API/UI/C10/模型/迁移/配置，未访问现场或 commit/push。

## 接续前置核对：受控推理生命周期

2026-10-20 再次只读核对现有 `ai_config`、诊断 orchestrator、`slow_sql_ai` 与审计清洗。
配置密文、单 Provider 选择、禁止重定向、慢 SQL 的显式上下文同意模式可以复用，但配置管理权
不等于本次发送同意；正则掩码不是完整的出站秘密过滤，模型名称也不等于不可变权重版本。

下一批必须先实现以下真实通道，再接候选 UI，不能把通用 `call_ai_provider()` 直接当安全编排器：

1. 服务端固定唯一已获准的本地配置，冻结 endpoint/config/model/catalog/input/budget digest；
   客户端和模型不能提交任意 URL 或 handler。配置变化重新确认，不把默认配置等同用户授权。
2. 显式预览实际脱敏输入及目的，默认不发送真实资产/模板 ID、连接参数、SQL、结果和报告正文；
   无法安全处理的输入拒绝，不用静默裁剪或仅靠日志掩码保证出站安全。
3. 一个候选仅一次生成，不换端点、不重试、不修 JSON。若保留 Ollama 容量探测，应明确总共
   最多两次 transport、一次生成，共享实际 wall deadline、请求数、响应字节、token、并发预算。
   现有各自 `timeout` 和 `resp.json()` 后的长度检查不满足慢滴流/超大响应预算。
4. 推理不持数据库事务锁；增加 owner/原 key/digest/阶段/取消/预算/有界候选请求账本。
   丢回包只原 key GET；进程重启不重放未知推理。关闭浏览器不等于服务端或模型停止。
5. 区分取消请求、transport 已关闭和模型停止未确认。撤销后晚候选不可发布，且不影响
   原来已经授权的确定性 plan。候选始终 `accepted=false`，仅用户显式采用到草稿，再走原
   prepare、receipt、审批和 enqueue；任何失败只回手工，不自动采用匹配结果。

验证需覆盖实际出站 payload、日志、跨 worker 抢占、慢滴流、响应过大、取消、重启/丢包、
身份变化及候选不能生成 plan。离线 transport fixture、浏览器 abort、真实模型停止 ACK
是三种不同证据，不相互替代。本节只有源码研究结论，**没有新增 Provider 调用或实现验收**；
不读取实际密钥，不访问模型或网络，不接 MES。
测试及其子进程均已退出，无留存活测试句柄。

## 5. 2026-10-21 候选入口接线（本文件的最新状态）

上面两节是"未接 UI"时点的设计与门禁。本轮把它接到页面上，并补上原来缺失的一半：

- **缺的那一半是权限与 consent 材料，不是页面。** 原先 `GET /ai-config` 既不暴露
  `provider_revision`，又受 `platform.settings` 限制；`POST /assistant/workflows/proposals`
  也未进 `_ASSISTANT_METHODS` / `request_scope` 白名单，且 body 走查会因
  `provider_config_id` / `confirmed_policy_id` 报 "Unresolved assistant resource"。
  结果是：**受限账号即使有页面也一定 403，管理员也拿不到 revision**。这三处同批修好。
- 新增只读 `GET /assistant/workflows/providers`（`capability.workflow_providers/v1`）：
  只回 `config_id/name/provider_type/model/revision/is_default`，**不含 base_url、密钥、超时**；
  仅列 `enabled=1`，上限 20 条，名称剥离不可打印字符。权限下限与其他 workflow 读取一致
  （`asset.read`），不扩大 `platform.settings`。
- `request_scope`：`providers`(GET) 与 `proposals`(GET/POST) 进白名单；`proposals` POST 记入
  读类 POST，body 走查对 `provider_config_id`/`policy_id`/`confirmed_policy_id` 只核"存在且启用"，
  `instance_id` 仍走 `native()` 资产范围。越界目标、停用/缺失模型与模板均拒绝。
- 前端 `AssistantProposalPanel`：选模型 → 勾选"允许发送" → 明确范围 → 请求候选；丢回包只用
  原 key GET，不重发；候选展示 `accepted=false` 与待补字段；"采用为手工草稿"只把
  `slots/scope` 填进原手工表单，冻结/审批/提交仍是原来的独立步骤。目录已撤下或 hash 变化时
  采用失败（`proposalAdoption` 只认当前授权目录）。

证据与边界：后端 `test_workflow_proposal_http.py` 21 项（含 providers 字段集合、停用排除、
revision 往返被接受、过期 revision 在任何模型调用前拒绝）；`test_request_scope_assistant.py`
173 项（含受限账号可读 providers、可提案、越界目标与停用引用被拒）；`test_assistant_contract.py`
3 项（新增 1 条 operation，floor 仍为 `asset.read`）；前端 `tests/assistant-proposal.test.mjs` 9 项；
`tests/assistant-workflow.test.mjs` 28 项；`vite build` 通过（`/tmp/mesdbtools-c11-dist`）。
**未做**：真实浏览器与真实模型端到端（无凭据/无现场模型）、出站 payload 逐字段留证、
wall deadline/字节预算的跨 worker 压力证据。这些仍属 C16 与 Provider 授权范围，不据本节勾选。
