# TraceMind AI API 参考

文档基线：2026-08-13

## 1. 基础约定

| 项目 | 值 |
| --- | --- |
| API 根路径 | `/api/v1` |
| 健康检查 | `GET /health` |
| Prometheus 指标 | `GET /metrics` |
| OpenAPI JSON | `GET /openapi.json` |
| Swagger UI | `GET /docs` |
| 默认后端地址 | `http://127.0.0.1:8800` |

除登录、安装、Agent enrollment/heartbeat/upload 等明确公开的协议入口外，业务 API 需要平台认证。最终路径、参数和 Schema 以运行时 `/openapi.json` 为准；本文件负责说明稳定领域和关键调用链。

### 1.1 请求与响应

- JSON 使用 UTF-8。
- 时间使用 ISO 8601，跨 Agent 时间线按 UTC 归一，并保留时钟偏移与质量。
- 分页接口通常返回 `items`、`total`、`page` 和 `page_size`。
- 删除成功可以返回 `204 No Content`。
- 长任务返回任务 ID 或 Snapshot ID，前端通过详情/进度接口轮询。
- 密码、Token、密钥和私钥字段不在读取响应中回显。

### 1.2 错误

FastAPI 标准错误：

```json
{
  "detail": "可读的错误原因"
}
```

| 状态码 | 含义 |
| --- | --- |
| `400` | 请求不满足业务规则 |
| `401` | 未认证或令牌失效 |
| `403` | 权限、许可证或功能开关禁止 |
| `404` | 资源不存在 |
| `409` | ID 冲突、幂等冲突或非法状态迁移 |
| `413` | 上传或 manifest 超过限制 |
| `422` | 请求 Schema 校验失败 |
| `429` | 配额、并发或速率限制 |
| `502` | 目标数据库或外部平台调用失败 |
| `503` | 功能关闭、依赖不可用或服务未就绪 |

连接失败不得返回业务成功。允许展示历史数据时，响应必须带缓存时间、数据新鲜度和实时错误。

## 2. API 域目录

| 域 | 前缀 | 说明 |
| --- | --- | --- |
| 初始化与认证 | `/setup`、`/auth`、`/system/init` | 安装、登录、OIDC 和当前用户 |
| 实例 | `/instances` | 数据库连接配置、测试和主机画像 |
| 统一资产 | `/assets` | 五层资产、拓扑、发现、校正和审计 |
| 外部 CMDB | `/cmdb` | 外部来源、资产、关系、绑定和同步 |
| 巡检 | `/inspection` | 执行、任务、结果、模板、策略和交换 |
| 报告 | `/reports` | 统一报告、巡检报告、详情和导出 |
| 四引擎运维 | `/oracle`、`/mysql`、`/mysql-ops`、`/operations/*` | 数据库采集和运维能力 |
| 性能 | `/performance`、`/slow-sql`、`/sql-profiles` | SQL、索引、表、基线、趋势和建议 |
| 调度 | `/scheduler` | 调度任务和运行状态 |
| 监控 | `/alert-events`、`/capacity`、`/observability` | 告警、容量、指标、日志、Trace 和采集任务 |
| 外部监控 | `/zabbix`、`/zabbix-push`、`/prometheus` | Zabbix、Prometheus 和 Alertmanager |
| AI 诊断 | `/ai`、`/diagnosis`、`/diagnosis-center` | 诊断、事故、方法论、数据源和自动策略 |
| 现场快照 | `/incident-agents`、`/incident-snapshots`、`/incidents` | Agent、触发、上传、证据和重分析 |
| 备份与恢复 | `/backup-center`、`/recovery` | 方案、平台同步、恢复任务和审批 |
| 数据治理 | `/data-governance` | 候选、计划、归档和对象存储 |
| 自动化 | `/automation` | 计划、审批、执行、运行记录和回滚 |
| 通知 | `/notify` | 通道、策略、模板、事件、发送和重试 |
| ITSM | `/itsm` | 配置、字段映射、工单、回写和规则 |
| 企业认证 | `/identity` | Provider、映射、认证和审计 |
| 系统 | `/system`、`/settings`、`/users`、`/modules`、`/ai-config` | 参数、用户、模块和模型配置 |
| 兼容接口 | `/ecosystem` | 历史数据只读和迁移，不创建第二套业务记录 |

## 3. 认证与系统

关键接口：

```text
POST /api/v1/auth/login
POST /api/v1/auth/external-login
GET  /api/v1/auth/oidc/providers
GET  /api/v1/auth/oidc/{provider_id}/start
GET  /api/v1/auth/oidc/callback
POST /api/v1/auth/oidc/exchange
POST /api/v1/auth/logout
GET  /api/v1/auth/current-user
GET  /api/v1/system/config
GET  /api/v1/system/dashboard-stats
```

OIDC callback 返回一次性交换信息，前端再调用 exchange 获取平台会话。Provider 密钥只在写入时接收。

## 4. 实例与五层资产

### 4.1 实例连接

```text
GET    /api/v1/instances
GET    /api/v1/instances/{instance_id}
POST   /api/v1/instances
PUT    /api/v1/instances/{instance_id}
DELETE /api/v1/instances/{instance_id}
POST   /api/v1/instances/connection-test
POST   /api/v1/instances/{instance_id}/test
GET    /api/v1/instances/{instance_id}/host/overview
POST   /api/v1/instances/{instance_id}/host/refresh
```

`connection-test` 用于新增表单未保存前测试，`/{instance_id}/test` 用于已保存实例。测试只更新测试状态，不应隐式重建资产拓扑。

### 4.2 统一资产中心

```text
GET  /api/v1/assets/overview
GET  /api/v1/assets/inventory
GET  /api/v1/assets/tree
GET  /api/v1/assets/topology
GET  /api/v1/assets/clusters
GET  /api/v1/assets/clusters/{cluster_id}
GET  /api/v1/assets/instances
GET  /api/v1/assets/instances/{instance_id}
GET  /api/v1/assets/hosts
GET  /api/v1/assets/hosts/{host_id}
GET  /api/v1/assets/discovery-batches
POST /api/v1/assets/discovery/preview
POST /api/v1/assets/discovery/apply
GET  /api/v1/assets/corrections
POST /api/v1/assets/corrections
GET  /api/v1/assets/audit
```

`discovery/preview` 只生成候选、冲突和建议，不修改正式资产。`discovery/apply` 才提交用户确认的变更。`corrections` 支持改角色、集群归属、拆分/合并和人工字段锁定，并写审计。

外部 CMDB 使用 `/api/v1/cmdb/sources`、`/assets`、`/relations`、`/bindings`、`/sync`、`/topology` 和 `/instance-context/{instance_id}`。

## 5. 巡检与模板

```text
POST /api/v1/inspection/{db_type}/{instance_id}/run
POST /api/v1/inspection/custom-metrics/test
GET  /api/v1/inspection/tasks
GET  /api/v1/inspection/tasks/{task_id}
GET  /api/v1/inspection/results
GET  /api/v1/inspection/report/{task_id}
GET  /api/v1/inspection/policies
GET  /api/v1/inspection/policies/{policy_key}
GET  /api/v1/inspection/policy-templates
POST /api/v1/inspection/policies
PUT  /api/v1/inspection/policies/{policy_id}
POST /api/v1/inspection/policies/{policy_id}/clone
DELETE /api/v1/inspection/policies/{policy_id}
GET  /api/v1/inspection/policies/{policy_id}/export
POST /api/v1/inspection/policies/export
POST /api/v1/inspection/policies/import/preview
POST /api/v1/inspection/policies/import
```

模板导入流程：

```text
文件选择 -> import/preview -> 显示模板、ID 冲突和跳过项 -> 用户确认 -> import
```

默认策略：稳定 ID 相同则跳过并提醒，不静默覆盖；内置模板不能删除或直接交换，必须 clone 后形成自定义模板。

报告接口包括 `/reports/inspections`、`/reports/{task_id}/detail`、`/reports/{task_id}/export`、Word 导出和统一报告。报告详情必须保留实际执行 SQL/命令与结果证据。

## 6. 性能、监控与诊断

### 6.1 性能

```text
GET  /api/v1/performance/overview
GET  /api/v1/performance/profile/{instance_id}
GET  /api/v1/performance/sql-profile/{instance_id}
POST /api/v1/performance/sql-profile/{instance_id}/collect
GET  /api/v1/performance/index-findings/{instance_id}
POST /api/v1/performance/index-findings/{instance_id}/collect
GET  /api/v1/performance/table-profile/{instance_id}
POST /api/v1/performance/table-profile/{instance_id}/collect
GET  /api/v1/performance/baseline/{instance_id}
POST /api/v1/performance/baseline/{instance_id}/collect
GET  /api/v1/performance/problem-trend
GET  /api/v1/performance/suggestions
```

Oracle AWR 使用 `/api/v1/oracle/*` 下的 AWR 相关路径，详见 [AWR 文档](awr.md)。

### 6.2 可观测

```text
GET  /api/v1/observability/overview
GET  /api/v1/observability/sources
POST /api/v1/observability/sources
POST /api/v1/observability/sources/{source_id}/test
POST /api/v1/observability/logs/query
POST /api/v1/observability/fault-timeline
GET  /api/v1/observability/grafana/templates
POST /api/v1/observability/grafana/export
POST /api/v1/observability/apm/trace
POST /api/v1/observability/apm/sql-evidence
GET  /api/v1/observability/metrics/current
GET  /api/v1/observability/metrics/history
POST /api/v1/observability/metrics/ingest-batch
GET  /api/v1/observability/alerts
POST /api/v1/observability/alerts/ingest
GET  /api/v1/observability/linkage-health
```

Zabbix 和 Prometheus 的配置、测试、同步、查询与 Alertmanager Webhook 同时保留在 `/observability` 统一入口和各自兼容域。

### 6.3 诊断中心

```text
POST /api/v1/diagnosis-center/route-intent
POST /api/v1/diagnosis-center/diagnose
GET  /api/v1/diagnosis-center/incidents
GET  /api/v1/diagnosis-center/incidents/{incident_id}
POST /api/v1/diagnosis-center/incidents/{incident_id}/ticket
POST /api/v1/diagnosis-center/incidents/{incident_id}/notify
POST /api/v1/diagnosis-center/incidents/{incident_id}/review
GET  /api/v1/diagnosis-center/instances/{instance_id}/arch-profile
POST /api/v1/diagnosis-center/instances/{instance_id}/arch-profile/refresh
GET  /api/v1/diagnosis-center/instances/{instance_id}/auto-policy
PUT  /api/v1/diagnosis-center/instances/{instance_id}/auto-policy
POST /api/v1/diagnosis-center/auto/run-now
GET  /api/v1/diagnosis-center/methodology
POST /api/v1/diagnosis-center/methodology
GET  /api/v1/diagnosis-center/data-sources
POST /api/v1/diagnosis-center/data-sources
POST /api/v1/diagnosis-center/data-sources/{ds_id}/test
```

`route-intent` 只负责识别用户意图和决定直接调用或导航。可直接执行的功能应返回动作及进度标识，不应只返回页面跳转。

## 7. Incident Snapshot 协议

### 7.1 Agent 平面

```text
POST /api/v1/incident-agents/enroll
POST /api/v1/incident-agents/{agent_id}/heartbeat
GET  /api/v1/incident-agents/{agent_id}/config
GET  /api/v1/incident-agents/{agent_id}/commands/next
POST /api/v1/incident-agents/{agent_id}/commands/{command_id}/ack
POST /api/v1/incident-agents/{agent_id}/triggers
POST /api/v1/incident-snapshots/uploads
PUT  /api/v1/incident-snapshots/uploads/{upload_id}/chunks/{sequence_no}
POST /api/v1/incident-snapshots/uploads/{upload_id}/complete
```

Agent 请求携带 Agent 身份、请求时间和签名。命令带 nonce、过期时间和签名，Agent 必须拒绝重复、过期或签名错误的命令。

### 7.2 Console 平面

```text
GET  /api/v1/incidents
GET  /api/v1/incidents/{incident_id}/snapshot-context
POST /api/v1/incidents/{incident_id}/snapshots/capture
POST /api/v1/incident-snapshots/capture
GET  /api/v1/incident-snapshots/{snapshot_id}
GET  /api/v1/incident-snapshots/{snapshot_id}/progress
GET  /api/v1/incident-snapshots/{snapshot_id}/timeline
GET  /api/v1/incident-snapshots/{snapshot_id}/metrics
GET  /api/v1/incident-snapshots/{snapshot_id}/evidence
GET  /api/v1/incident-snapshots/{snapshot_id}/causal-graph
GET  /api/v1/incident-snapshots/{snapshot_id}/raw/{section}
GET  /api/v1/incident-snapshots/{snapshot_id}/download
POST /api/v1/incident-snapshots/{snapshot_id}/reanalyze
POST /api/v1/incident-snapshots/{snapshot_id}/retention
POST /api/v1/incident-snapshots/{snapshot_id}/annotations
GET  /api/v1/incident-agents
POST /api/v1/incident-agents/enrollment-tokens
POST /api/v1/incident-agents/{agent_id}/revoke
GET  /api/v1/incident-trigger-rules
POST /api/v1/incident-trigger-rules
POST /api/v1/incident-trigger-rules/{rule_id}/test
GET  /api/v1/incident-audit
```

Snapshot 状态只能沿合法状态机前进。`partial` 必须列出缺失流和时间范围，不能伪装完整。原始下载、留存变更和重分析均写审计。

## 8. 通知、工单、备份和自动化

### 8.1 通知与 ITSM

通知域提供 `/notify/channels`、`/policies`、`/templates`、`/recipients`、`/events` 和 `/logs/{log_id}/retry`。

ITSM 域提供 `/itsm/configs`、字段映射、`/tickets/manual`、`/tickets/from-alert`、`/tickets/from-inspection`、`/tickets/from-ai-diagnosis`、状态同步、评论、关闭和回调。

### 8.2 备份与恢复

```text
GET  /api/v1/backup-center/overview
GET  /api/v1/backup-center/catalog
POST /api/v1/backup-center/plan
GET  /api/v1/backup-center/reports
GET  /api/v1/backup-center/platform/configs
POST /api/v1/backup-center/platform/configs/{config_id}/sync
GET  /api/v1/recovery/engines
POST /api/v1/recovery/tasks
POST /api/v1/recovery/tasks/{task_id}/approve
POST /api/v1/recovery/tasks/{task_id}/reject
```

### 8.3 自动化

```text
GET  /api/v1/automation/plans
POST /api/v1/automation/plans
POST /api/v1/automation/plans/{plan_id}/submit
GET  /api/v1/automation/approvals
POST /api/v1/automation/approvals/{request_id}/approve
POST /api/v1/automation/approvals/{request_id}/reject
POST /api/v1/automation/approvals/{request_id}/execute
GET  /api/v1/automation/runs
POST /api/v1/automation/plans/{plan_id}/rollback
```

当全局执行开关关闭时，`execute` 必须拒绝真实执行，并保留计划与审批记录。

## 9. 尚未开放的规划 API

State Intelligence、深度内存/线程/动态探针和 Linux eBPF 仍处于设计阶段。它们的目标路径在架构文档中定义，但在实现进入 registry 和 OpenAPI 前不属于公开 API：

- [State Intelligence 与现场重建](architecture/state-intelligence-and-scene-reconstruction.md)
- [eBPF 内核动态观测](architecture/ebpf-kernel-observability.md)
- 开发计划

## 10. API 变更门禁

1. 新路由必须加入 `app/api/v1/registry.py` 的明确领域。
2. method/path 和 operationId 不得重复。
3. 修改 Agent 协议时先更新 Python/Go 契约样例和双端测试。
4. 删除接口前提供兼容期和迁移说明。
5. 写接口必须定义幂等、权限、审计和失败语义。
6. 长任务必须定义进度、终止、超时和重试边界。
7. 运行 `python3 scripts/check_backend_routes.py` 和相关测试后再更新本文件。
