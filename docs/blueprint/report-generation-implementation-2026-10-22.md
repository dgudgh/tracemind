# 统一运维报告与售后分析报告生成：实现文档

日期：2026-10-22  
状态：后端已实现并通过测试（2026-10-22）；前端入口走“任务清单 → 通用能力”面板，专用报告向导待接

## 1. 问题与目标

### 1.1 现状

- 快捷卡“报告 / 售后输出”的提示词是“我需要生成统一运维报告和售后分析报告”。
- 该提示词被路由到 `report.unified`，绑定 `report.catalog.read`（v2）。
- `report.catalog.read` 只读取 `report_artifact` 目录，不生成报告。
- 因此助手只能“打开报告页”，不能生成统一运维报告或售后分析报告；AI 也没有可调用的报告生成能力。

### 1.2 目标

新增一个可审批、可冻结、可回读的服务端能力：

```text
report.document.generate
```

一次生成两类报告：

- `unified`：统一运维报告，面向运维/售后工程师，包含范围、巡检、告警、容量/性能风险、证据；
- `after_sales`：售后分析报告，面向客户，包含现象、影响、根因候选、处理建议、证据摘要，隐藏内部敏感信息。

AI 只负责“分析与建议”的叙述；事实、数字、风险等级、证据引用由确定性装配器生成。

### 1.3 非目标

- 不执行数据库变更、不修复问题；
- 不伪造巡检/告警数据；
- 不把 AI 生成内容当作事实来源；
- 不替代报告中心现有的报告归档/下载/签名能力。

## 2. 总体流程

```text
用户/快捷卡
  -> 助手识别 report.document.generate
  -> 创建计划（实例、report_type、window、format、include_ai）
  -> 独立审批
  -> durable 执行
       1) report_composer 收集证据（只读）
       2) 规则引擎生成事实/风险/建议
       3) 可选 AI 生成分析叙述（严格 JSON + 引用校验）
       4) 生成 ReportArtifact（markdown/html/json + 证据清单 + 哈希/签名）
  -> 证据校验（artifact id、sha256、source_key、instance）
  -> 返回 artifact_id + 下载地址
```

AI 不直接调用页面卡片；AI 选择能力与参数，执行器调用能力。

## 3. 能力契约

### 3.1 参数

```json
{
  "report_type": "unified",
  "window_hours": 168,
  "format": "markdown",
  "include_ai": true
}
```

- `report_type`：`unified` | `after_sales`；
- `window_hours`：1–720，默认 168；
- `format`：`markdown` | `html`（JSON 始终保存）；
- `include_ai`：默认 true；模型不可用/输出不合规时按策略降级。

能力声明：

```text
kind = collect
requires_instance = true
engines = oracle/mysql/postgresql/sqlserver
timeout_seconds = 300
handler = report.document_generate
version = 2
```

审批：`collect` 属于 `APPROVAL_KINDS`，必须独立审批 + durable fence。

### 3.2 输出

```json
{
  "outcome": "success",
  "artifact_id": 123,
  "artifact_uid": "…",
  "sha256": "…",
  "report_type": "unified",
  "format": "markdown",
  "analysis_source": "ai",
  "model": "qwen3.8:27b",
  "evidence_count": 14,
  "generated_at": "…",
  "download_url": "/api/v1/reports/catalog/123/download"
}
```

- 无巡检、无告警、无容量证据时：`outcome=partial`，不生成空报告；
- AI 失败但规则报告可用：`outcome=success`，`analysis_source=rule_engine`，`ai_error` 记录原因；
- AI 输出引用不存在的证据：视为 AI 失败并按配置降级，不保存为 AI 报告。

## 4. 证据装配（report_composer）

输入：实例、时间窗、报告类型。只读查询：

| 证据 | 来源 | 关键字段 |
|---|---|---|
| 实例信息 | `db_instance` | 名称、类型、版本、主机、端口（售后报告脱敏） |
| 最近巡检 | `db_inspection_job` + `db_check_result` | job_id、状态、评分、严重/警告数量、Top 发现 |
| 告警 | `db_alert_event` | 时间窗内按 severity 统计、Top 事件 |
| 容量 | `db_capacity_alert` | open 告警按 level、对象 |
| 报告制品 | `report_artifact` | 可引用的既有巡检报告 artifact_id |

证据引用格式：

```json
{"key": "inspection:job:<id>", "type": "inspection_job", "id": "...", "observed_at": "..."}
{"key": "check:<result_id>", "type": "check_result", "id": 123}
{"key": "alert:<event_id>", "type": "alert_event", "id": 456}
{"key": "capacity:<alert_id>", "type": "capacity_alert", "id": 789}
```

### 4.1 统一运维报告章节

1. 执行摘要（范围/时间/总体风险）
2. 巡检结论（job、评分、严重/警告、Top 发现）
3. 告警态势（数量、级别、Top 事件）
4. 容量风险（open 告警）
5. 建议与下一步（规则/ AI）
6. 证据清单
7. 生成信息（模型、提示版本、时间、哈希）

### 4.2 售后分析报告章节

1. 客户现象与影响
2. 已执行检查与范围
3. 主要发现（客户可读，不含内部账号/主机凭据）
4. 根因候选与置信度（引用证据）
5. 处理建议与回退注意
6. 证据摘要（脱敏后的 job/alert/capacity id）
7. 声明：AI 辅助分析，事实以证据为准

## 5. AI 调用契约

- 使用默认启用的 `AIProviderConfig`（当前环境：Local Ollama `qwen3.8:27b`；DeepSeek `deepseek-flash`）；
- 输入只包含结构化证据摘要，不含密码、密钥、完整 SQL 文本；
- 输出必须为 JSON：

```json
{
  "summary": "…",
  "risks": [{"text": "…", "severity": "warning", "evidence_keys": ["…"]}],
  "recommendations": [{"text": "…", "evidence_keys": ["…"]}],
  "root_causes": [{"text": "…", "confidence": 0.6, "evidence_keys": ["…"]}]
}
```

- 校验：
  - 必填字段类型正确；
  - `evidence_keys` 必须全部存在于本次证据集合；
  - 不允许出现证据之外的具体数字/结论（数字校验以报告装配器为准）；
  - 失败重试最多 1 次；
- 失败降级：规则模板 + `analysis_source=rule_engine` + `ai_error`；
- 记录 `model`、`provider_name`、`prompt_version`、耗时。

## 6. 报告制品

新增 `report_center.create_document_artifact`：

- `source_key = report:document:<execution_id>`（幂等，重试不重复建）；
- `source_type = report_document`，`artifact_kind = document`；
- `report_type = unified | after_sales`；
- `content_markdown` / `content_html` / `content_json`；
- `evidence_manifest`；
- `content_sha256`、`sign_artifact`；
- `retention_until` 默认 365 天；
- `format_manifest = ["markdown","html","json"]`（Word/PDF 后续接导出）。

下载复用 `/api/v1/reports/catalog/{id}/download`。

## 7. 计划层校验

新增 `_validate_report_document_effect`：

- `outcome` ∈ success/partial/failed；
- success 必须有 artifact_id；
- 按 id 回查 `report_artifact`，`source_key` 与执行 id 一致；
- `sha256` 与行内一致；
- `instance_id` 与计划一致；
- partial 必须给出缺失证据原因；
- 失败不得有 artifact。

## 8. 前端与助手入口

- `assistantActions.js`：
  - `report.unified` 在命令含“生成/制作/出具”时绑定 `report.document.generate`；
  - 加入 `SERVER_CAPABILITIES`；
- 计划工作区/通用能力面板：
  - `params_schema` 渲染 report_type/window_hours/format/include_ai；
  - 默认值：unified、168h、markdown、true；
- 结果区显示报告标题、分析来源（AI/规则）、下载按钮、缺失证据提示；
- 快捷卡“报告 / 售后输出”不再只导航；生成后进入结果。

## 9. 测试与验收

必须包含：

1. composer 在真实巡检/告警/容量数据下生成章节与证据；
2. 无证据 → partial，不产生空报告；
3. AI 真实本地/回环服务成功 → `analysis_source=ai`，引用全部有效；
4. AI 超时/返回非 JSON/引用不存在 → 降级规则，`ai_error` 存在；
5. 计划审批 → 执行 → artifact 回读 → sha256/source_key/instance 一致；
6. 重复执行同一 execution_id 不重复建 artifact；
7. 售后报告脱敏（无密码/连接串/内部账号）；
8. HTTP 下载返回 markdown/html；
9. 前端计划创建与结果展示（真实浏览器/单测）。

## 10. 实施顺序

1. `report_center.create_document_artifact`；
2. `report_composer` + `report_ai`；
3. `report_document.generate` handler + 声明 + 注册 + 校验；
4. 测试（无 AI 先绿，再接 AI）；
5. 前端能力映射与参数表单；
6. 文档证据与边界。

## 11. 边界

- 不在本次实现 Word/PDF 版式；
- 不自动覆盖历史报告；
- 不把 AI 作为事实来源；
- 没有证据时宁可 partial，不生成“看起来完整”的空报告。


## 12. 当前实现结果（2026-10-22）

- 新增 `report.document.generate`（v2，kind=collect，70 个 server handler 之一）。
- 新增 `report_document`：确定性证据装配 + 规则分析 + AI JSON 引用校验 + 规则降级。
- 新增 `report_center.create_document_artifact`：markdown/html/json、证据清单、sha256、签名。
- 计划层 `_validate_report_document_effect`：按 artifact id/source_key/sha256/instance 回查；
  `partial/no_evidence` 不产生空报告。
- 真实 DeepSeek 已验证：默认 Ollama 不可达时按启用顺序回退到 `deepseek-flash`，
  真实生成 `analysis_source=ai`、`evidence_count=4`、报告摘要包含巡检/告警/容量结论；
  证据 `reports/release-gate/report-ai.json`（不含密钥）。
- 证据：`test_report_document_generate.py` **9 passed**
  （统一/售后变体、无证据 partial、AI 有效引用、AI 非 JSON/未知引用降级、provider 顺序回退、同 execution_id 幂等）。
- 前端：能力已加入 `SERVER_CAPABILITIES`；快捷卡/意图命中“生成报告”时，
  自动打开“任务清单 → 通用能力”并预选 `report.document.generate`，
  参数表单支持 report_type/window_hours/format/include_ai，审批后执行。
- 能力账：78 总、70 wired（51 读、19 效果）、2 client_bound、6 declared_not_wired。

### 未完成 / 边界

1. 售后报告固定版式（公司抬头、客户模板）与 Word/PDF 导出未做；
2. 敏感字段脱敏目前覆盖密码/连接串/密钥模式，未做逐字段白名单审计；
3. 真实模型已用 DeepSeek 验证；本机 Ollama 不可达，回退逻辑已覆盖。


## 13. 诊断工作台审计后的修复（2026-10-22）

- 报告下载改为带 JWT 的 `downloadReportArtifact`，不再用裸 `<a href>`；
- partial/无证据不再渲染空下载链接；
- AI 报告风险等级只由证据规则决定；
- 售后报告增加“风险/根因候选”章节；
- AI prompt 不再发送主机名、端口；只发实例名/类型/版本与证据 key；
- 同一实例切换后的迟到诊断/报告结果用 epoch 丢弃；
- 报告生成对普通 `report.read` 角色开放（派生制品），不改目标库。

详细审计：[diagnosis-workbench-audit-2026-10-22.md](diagnosis-workbench-audit-2026-10-22.md)。


## 14. 收尾修复（2026-10-22）

- 一键批次改为单事务：任一报告失败，整批回滚，不留半批制品；
- 下载按 `format` 选择 Markdown/HTML；
- AI 文本数字必须出现在证据中，编造数字（如 `99999%`）触发降级；
- 新增批次回滚、编造数字、AI 隐私 prompt 测试。

证据：报告测试 **9 passed**。


## 15. AI 优先调度入口

新增 `POST /api/v1/assistant/dispatch-intent`：模型从已登记能力中选择一个并填参数，
服务端校验后，读能力直接执行计划，写能力进入确认/审批；无模型时回退页面导航。
真实 DeepSeek 已验证选择 `report.catalog.read` 并执行成功。
