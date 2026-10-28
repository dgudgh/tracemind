# 客户需求入口实施进度（2026-10-23）

2026-10-25 已形成下一轮[开发与测试交付方案](requirement-delivery-2026-10-25/README.md)，开发任务 D01—D18、共性用例 TC001—TC116 均须按当前环境重新验收。编制方案不提升本页旧任务完成状态。

对应计划：[customer-requirement-task-plan-2026-10-23.md](customer-requirement-task-plan-2026-10-23.md)

最新复核见：[MySQL 复核与执行缺口修复](../archive/acceptance/2026-10-23-requirement-mysql-execution-gaps.md)。本轮补齐报告与 17 张效果卡的真实执行连接；真实模型首次输出仍被拒绝，实际 8800 尚待授权备份和显式迁移，完整交付仍未关闭。

此前实现与验收见：[自然语言需求入口实现与验收](../archive/acceptance/2026-10-23-requirement-goal-implementation.md)。下文保留各轮历史进度，不将此前测试或运行状态外推为当前现场验收。

客户入口和任务流体验接续：[客户需求入口与任务流体验](../archive/acceptance/2026-10-23-requirement-customer-experience.md)。

## 已完成 / 部分完成

### RQ02 人工导航权限与跨引擎目标（完成）

- 诊断工作台的本地匹配器只消费当前账号可见菜单；别名候选也受可见菜单限制；
- 跨引擎导航不再自动选第一台实例：当前实例不匹配时显式清空；
- 菜单要求特定引擎但当前实例不匹配时清空，要求人工重新选择；
- `navigateTo` 区分“未传实例”和“明确清空”。

证据：`frontend/tests/menu-navigation.test.mjs`（隐藏菜单不推荐）、前端全量 881 passed。

### RQ03 调度合同（部分）

- 调度请求 schema 使用 `extra=forbid`、严格类型；
- 模型输出使用独立 `DispatchSelection` 校验：能力 ID 模式、参数对象、置信度、原因长度；
- 低置信度、无匹配、非法 JSON、未登记能力、非法参数均不会执行；
- 已补稳定 `reason_code`：`ai_not_configured`、`ai_unavailable`、`budget_exhausted`、`no_capability`、`no_match`、`low_confidence`、`invalid_output`、`invalid_params`、`missing_instance`；
- 前端按 `reason_code` 显示不同提示；
- 仍待补：完整状态机、OpenAPI 示例。

### RQ05 目标与范围（部分）

- 调度按实例引擎过滤能力目录；
- 跨引擎实例不再借用旧实例；
- 仍待补：全部实例/批量范围语义、范围与能力的显式匹配。

### RQ06 参数提取与人工补参（部分）

- 模型提取但未通过 schema 校验的参数会随 `action=navigate` 返回；
- 前端把 `params` 传入通用能力面板，作为初始值保留；
- 用户补齐后仍由服务端重新校验。

证据：`backend/tests/test_capability_dispatch.py` 18 passed。

### RQ07 请求防重（部分）

- 前端每次需求生成 `request_id`；
- 后端将其纳入计划幂等键，重复提交复用同一计划；
- 已补模型调用前持久回执：同一 `request_id` 直接回放上次 `assistant.dispatch` 审计记录，不再调用模型；
- 回执包含 action/capability/params/plan，重复请求返回同计划；
- 仍待补：跨重启回查页面、回执清理策略、断线恢复 UI。

证据：`test_capability_dispatch.py` 18 passed；前端 `assistant-requirement.test.mjs` 13 passed。

## 验证

- 后端调度测试 23 passed；
- 前端全量 881 passed / 0 failed；
- `check_all.py --fast` 通过；
- 后端 8800、前端 3300 正常。

### RQ08 模型配置与预算（部分）

- 一次调度固定一个启用 provider，不在计划/执行失败后换模型重派；
- provider id/name/model 写入结果与回执，便于审计关联；
- 复用共享预算、响应上限、禁止重定向和超时；
- 已补 provider 配置 revision（id/type/base_url/model/update_time 的哈希）和 duration_ms；
- 仍待补：入口级截止时间、指标导出。

### RQ11 固定工作流（部分）

- 当模型没有匹配到单卡能力时，调度器会调用已登记固定工作流匹配；
- 命中后按冻结版本 `prepare_workflow`，返回准备单和首个计划，前端打开计划结果；
- 伪造版本/步骤仍由工作流库哈希与 `prepare_workflow` 拒绝；
- 未命中或缺少实例时明确转人工，不执行。
- 证据：`test_capability_dispatch.py` 新增工作流准备、缺少实例两个用例；
- 前端新增“workflow → planId → 打开计划结果”测试，不执行本地动作。

### RQ12 批量范围（部分）

- 对需要实例的能力，用户说“所有/全部实例”时不再缩成当前实例执行；
- 返回 `batch_scope_required`，要求在批量任务中确认目标；
- 读能力“全部实例”现会创建持久批次并返回 batch_id，前端批次工作台按 batch_id 回读；
- 写能力仍要求人工确认批量范围；
- 新增真实 worker 测试：两个实例的读批次由 `process_one` 依次处理，全部进入终态，批次持久化；
- 待补：长任务超时、worker 重启/丢回包恢复、逐实例 UI 状态。

### RQ14 调度审计（部分）

- 回执保存 input_hash、provider id/name/model/revision、duration_ms、选择结果和计划/批次引用；
- 同一 request id 回查可定位模型、输入、耗时和业务结果；
- 新增管理员聚合指标 `GET /assistant/dispatch-metrics`：总调度数、按 action、按 reason_code、按 provider、平均耗时；
- 证据：`test_capability_dispatch.py` 指标聚合用例。

### RQ17 真实模型评测（部分）

- 本机 Ollama 不可达；已将 DeepSeek 设为默认 provider（key 未写入任何文档）；
- 真实 DeepSeek 实测：
  - “列出巡检任务” → `inspection.run.query`，计划执行成功；
  - “生成当前实例最近7天的售后分析报告” → `report.document.generate`，返回 confirm，需审批；
- 证据：`reports/release-gate/requirement-ai.json`；
- 仍待补：多轮评测集、错误/低置信样本、耗时统计。

### RQ13 无 API 人工路径（部分）

- 无模型/传输失败时先回查页面，不执行任务；
- 覆盖提示词扩展为巡检、售后报告、慢 SQL、故障诊断、添加实例、慢 SQL 页面；
- 测试断言只发生 dispatch + 本地匹配 + 导航，不创建计划、不执行；
- 专业页面仍调用自身业务 API；通用能力表单仍走计划内核。

### RQ16 真实浏览器（部分）

- 真实 Chromium 打开生产前端 3300，注入 admin 会话；
- 仅拦截 `/assistant/dispatch-intent` 模拟“无模型”，其余请求走真实后端；
- 输入“查看慢 SQL 分析页面”后：打开页面、无计划调用、无业务 POST；
- 证据：`reports/browser/rq16-manual-path.png`；
- 真实模型成功路径也已在 Chromium 验证：
  - 输入“列出巡检任务”；
  - DeepSeek 选择 `inspection.run.query`；
  - 浏览器打开计划结果，显示“计划成功 / 读取成功 / succeeded”；
  - 证据：`reports/browser/rq16-ai-path.png`；
- 写能力审批状态已在真实浏览器验证：
  - 输入“生成当前实例最近7天的售后分析报告”；
  - DeepSeek 选择 `report.document.generate`；
  - 计划进入 `approval_state=pending`，显示“申请审批”；
  - 未执行、无报告制品；
  - 证据：`reports/browser/rq16-approval-pending.png`。
- 待补参状态已在真实浏览器验证：
  - 模拟模型返回 `invalid_params` 和已提取参数；
  - 页面打开通用能力面板；
  - Report Type 预填 `after_sales`、Window Hours 预填 `72`；
  - 用户可补齐后提交，服务端仍重新校验；
  - 证据：`reports/browser/rq16-params-prefill.png`。
- 待补：断网、刷新恢复、账号切换。

## 下一步

RQ03 稳定原因码 → RQ07 持久回执 → RQ09/RQ10 单卡执行与前端状态 → RQ11/RQ12 固定流程/批量。

### RQ09/RQ10 本轮补充

- 调度 `confirm` 产生的效果计划已验证：申请审批 → 另一账号批准 → 执行 → 结果回读成功；
- 前端调度请求加 120 秒超时；超时按传输失败进入人工页面，不会无限转圈；
- 仍待补：断线后刷新恢复到原请求回执页、按钮级并发测试。

## RQ18/RQ20 阻断条件（2026-10-23）

本轮可本地验证的范围已推进到：

- 单卡读/写/审批/补参/断网回查/刷新恢复；
- 固定流程准备与前端打开计划；
- 全部实例读的持久批次与 worker 结算；
- 真实浏览器无模型路径、AI 读路径、AI 写审批路径、补参预填、失败态；
- 真实 DeepSeek 两条请求；
- 审计回执、input_hash 冲突、provider revision/duration、聚合指标。

无法在本机完成：

1. **RQ18 预生产联合验收**：没有独立预生产目标库、预生产审批账号、运维/领域责任人，不能替代现场联合验收；
2. **RQ20 灰度与回退演练**：没有发布窗口、部署权限、真实流量与回退环境；不能以源码/单测代替发布演练；
3. **RQ19 独立制品验证**：当前共享工作区有并发未提交改动，未获代码清理/所有权授权，不能宣称干净制品；
4. **RQ16 剩余状态**：账号切换、执行中/待核对全状态的真实浏览器证据仍需可控账号/长任务环境。

这些阻断不是代码内可绕过项；需要用户提供预生产/部署环境或明确调整验收口径。


## 审核 P1 修复（2026-10-23 追加）

- **F01 目标引擎/范围**：调度前解析文本中的引擎；已选实例与需求引擎冲突返回 `target_engine_mismatch`；批量目标取用户指定引擎 ∩ 能力支持引擎 ∩ 当前范围，不再用其他引擎代替。
- **F02 并发防重**：模型调用前用 `assistant.dispatch_claim` 审计占位 + 唯一用户/请求键原子认领；同请求并发只有一个进入模型，其余返回 `request_in_progress`。
- **F03 回执再鉴权**：回执 GET/POST 回放都重新加载计划并执行当前能力权限/范围检查；撤权后不再返回旧计划内容。
- **F04 受限账号回查**：回执 GET 登记到范围路由；request_id 不被当作资产 ID，范围中间件放行后由回执服务复核持久计划/批次。
- **F05 发送前保存请求号**：页面在 POST 前生成并保存 request_id，所有返回分支保留同一键；断网回查和刷新恢复使用同一编号。
- **F06 工作流导航**：只有 `operation=prepare` 才建计划；`navigate` 只返回导航目标，不再准备计划；巡检 policy_id 参数从文本带入 slots。
- **F07 可见菜单**：`openNavigationTarget` 查不到可见菜单项时不再设置 activeKey；“新增实例”直接导航也先检查 `asset.center` 可见。
- **F08 恢复竞态**：刷新恢复绑定启动时会话、实例和 epoch；实例/账号变化后的晚回执丢弃，不覆盖当前工作区。
- **F09 无请求号**：服务端为缺省请求生成唯一键；工作流/批次不再共用 `:none`。


## MySQL 控制库补充验证（2026-10-23）

用户澄清：平台控制库是 MySQL，Oracle 是被诊断目标；SQLite 仅为隔离验收。已在本地 MySQL 8.0 新建独立库验证：

- 同 request_id 并发：模型调用 1 次，第二个请求 `request_in_progress`，计划 1 条，回执 1 条；
- 全部实例读批次：创建 2 个计划，embedded worker 领取 2 次，两个成员都进入终态（archived）。

证据：[mysql-concurrency-queue.txt](../../reports/release-gate/mysql-concurrency-queue.txt)。

这补充了 MySQL 下的原子认领与队列结算验证，但仍不是生产控制库压测、客户现场或预生产联合验收。
