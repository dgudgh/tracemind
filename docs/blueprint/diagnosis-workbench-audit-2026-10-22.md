# 诊断工作台审计与修复记录（2026-10-22）

## 1. 工作台实际链路

```text
LandingPage（客户/工程师）
  ├─ 快速卡：只填充 prompt 或导航（不直接执行）
  ├─ 任务清单：AssistantPlanWorkspace（清单/批次/固定流程/通用能力）
  ├─ 让 TraceMind 执行 → runDiagnosis
  │    ├─ resolveAssistantInspectionCommand → 巡检队列
  │    ├─ isReportGenerationCommand → 一键报告直生成（本轮新增）
  │    ├─ resolveRouteByAiIntent → 导航 / 服务端能力
  │    ├─ resolveAssistantActionRequest → 计划确认
  │    └─ 兜底：诊断中心聚合
  └─ 结果工作台（卡片/时间线/根因/报告）
```

## 2. 本轮发现的问题

| # | 严重度 | 问题 | 根因 | 状态 |
|---|---|---|---|---|
| 1 | 高 | 报告下载用 `<a href>`，浏览器不带 JWT，下载 401 | 直链未走带认证的 blob 下载 | 已修：改用 `downloadReportArtifact` |
| 2 | 高 | 报告部分生成（无证据）仍渲染空下载链接 | 前端未判断 `artifact_id` | 已修：显示“未生成/无证据” |
| 3 | 高 | AI 报告的 `risk_level` 恒为 normal | AI 返回对象没有 `risk`，覆盖了规则风险 | 已修：风险只由证据规则决定 |
| 4 | 中 | 售后报告缺“风险/根因候选”章节 | AI 输出字段未渲染 | 已修：新增 sections |
| 5 | 高 | AI prompt 把主机名/端口发给外部模型 | 直接序列化完整 evidence | 已修：只发实例名/类型/版本、证据 key 与可读事实 |
| 6 | 中 | 新能力未登记，admin 也报“未登记/无权” | 旧后端进程只有 77 条声明；DB 也只 77 条 | 已修：同步 78 条并重启后端 |
| 7 | 中 | 通用能力字段打开时全空 | 面板先设 capabilityId，目录后到，初始化 effect 只监听 id | 已修：监听 selected/fields |
| 8 | 中 | 计划工作区对 v2 读能力显示“不可执行” | 仍硬编码 3 个 v1 reader | 已修：按 kind/version/plan_executable 动态判断 |
| 9 | 中 | 计划工作区会把需必填参数的能力当“简单读” | 只校验 page/page_size | 已修：排除 required 非分页的读能力 |
| 10 | 中 | 默认 Ollama 不可达时永不尝试 DeepSeek | 只取默认 provider | 已修：按启用顺序回退，记录实际 provider |
| 11 | 中 | 报告生成与巡检交互不一致（表单/审批 vs 一键） | 报告走通用效果计划 | 已改为默认一键直生成；高级自定义仍走通用能力 |
| 12 | 中 | 切换实例后旧诊断结果仍可能落盘 | 普通诊断缺少 epoch guard | 已修：diagnosisEpoch |
| 13 | 低 | 报告下载固定 markdown，忽略 format | 前端按钮未用 item.format | 待优化 |
| 14 | 低 | 一键报告固定 both + 7 天，不能选单类型 | 产品默认值 | 可用高级面板覆盖 |

## 3. 设计结论

- **一键巡检**是只读/采集类，直接执行；**一键报告**是平台内派生制品，也直接执行。
- **会改变目标数据库的能力**仍必须走计划 + 独立审批 + durable fence。
- **AI 不决定事实**：数字、风险等级、证据引用由确定性装配器产生；AI 只写叙述，引用必须存在。
- **外部模型数据最小化**：不发送主机、端口、凭据、完整 SQL。

## 4. 证据

- 报告：`test_report_document_generate.py` **7 passed**
- 诊断/助手/权限：`test_authorization_boundary.py`、`test_assistant_api.py`、报告联合 **122 passed**
- 前端：**868 passed / 0 failed**（新增报告命令测试）
- `check_all.py --fast` 通过
- 真实 DeepSeek：`reports/release-gate/report-ai.json`

## 5. 仍需注意

1. 一键报告目前**不做审批**；如果合规要求生成报告也留审批，需要改回计划路径。
2. 报告批次不是事务：第一个成功、第二个失败时会留下一个制品；重试会生成新批次。
3. AI 摘要仍可能包含未经逐字引用的概括；当前只校验引用 key 存在，不校验每个数字。
4. 真实 Ollama 未启动；DeepSeek 已验证可用。


## 6. 按“AI 优先调度能力卡”重构（2026-10-22）

2026-10-23 复核修正：下面是当日接入记录，不能据此推断所有需求已遵守双路径。后续发现巡检/报告仍绕过模型、降级仍会执行浏览器动作，现已统一入口；每次固定单一 provider，不再跨 provider 重派。当前行为、回归结果及遗留代码边界见[客户需求入口](customer-requirement-entry-2026-10-23.md)。

用户要求：客户写需求 → 有大模型 API 时分析并调度后台能力卡；没有 API 时打开页面手动操作。

已新增：

```text
POST /api/v1/assistant/dispatch-intent
  body: { text, instance_id }
  1. 读取当前账号可见的服务端能力目录
  2. 优先使用默认 provider，失败后按启用顺序回退（Ollama → DeepSeek）
  3. 模型只能返回已登记的 capability_id + 声明参数
  4. 服务端按 PARAM_SCHEMAS 重新校验
  5. read/query → 创建并执行计划；effect → 创建计划等待确认/审批
  6. 无可用模型 → action=navigate，前端回退到本地页面路由
```

前端 `runDiagnosis` 已接入：AI 调度成功时直接打开对应计划结果；需要确认的能力打开计划/审批；AI 不可用时继续走本地页面路由，让用户自己点。

真实 DeepSeek 验证：

- 输入“帮我列出最近的统一报告”
- 模型选择 `report.catalog.read`
- 服务端创建并执行计划，结果 `settled / succeeded`
- 未执行任何目标库写操作

测试：`test_capability_dispatch.py` **4 passed**（读执行、效果确认、未知能力回退、无 provider 回退）。
