# TraceMind AI 外部生态适配路线图

## 1. 目标

TraceMind AI 后续要从数据库运维工具升级为数据库智能运维平台，外部生态适配需要围绕四条主线展开：

```text
告警联动线：Zabbix / Prometheus / 通知 / 工单
资产上下文线：CMDB / 主机 / 数据库实例 / MES 系统关系
诊断上下文线：日志 / APM / SQL 画像 / 主机指标
治理执行线：历史归档 / 对象存储 / 自动化执行 / 审批闭环
```

已完成的基础能力：

| 任务 | 名称 | 状态 |
|------|------|------|
| T-047 | Zabbix 数据接入与主机预警聚合 | 已完成 |
| T-048 | TraceMind 告警推送到 Zabbix | 已完成 |
| T-050 | Prometheus 指标导出与 Alertmanager 接入 | 已完成 |

本路线图定义 T-051 到 T-060 的生态适配方向。自 2026-07-21 起，这十项能力已归入各自业务模块，原“生态适配”汇总页正式退役。历史配置、运行记录和后端接口继续保留，并由对应业务页面直接使用。

## 2. 优先级路线

### P0：生产闭环优先

| 任务 | 名称 | 目标 |
|------|------|------|
| T-051 | 告警通知通道适配模块 | 企业微信、钉钉、飞书、SMTP、短信、通用 Webhook |
| T-052 | ITSM / 工单系统适配模块 | 告警转工单、巡检转工单、处理状态回写 |

### P1：平台上下文增强

| 任务 | 名称 | 目标 |
|------|------|------|
| T-053 | CMDB 资产同步与关系映射模块 | 工厂、产线、MES 应用、数据库、主机、监控对象关系 |
| T-054 | 日志平台适配模块 | ELK、OpenSearch、Loki、Graylog 日志关联分析 |
| T-055 | Grafana Dashboard 模板导出模块 | 导出可导入 Grafana 的 dashboard.json |
| T-057 | 归档文件对象存储适配模块 | CSV / EXPDP 文件上传 MinIO、S3、OSS、COS、OBS |
| T-059 | 企业认证与权限适配模块 | LDAP、AD、CAS、OAuth2、OIDC、企业扫码登录 |

### P2：高级自动化能力

| 任务 | 名称 | 目标 |
|------|------|------|
| T-056 | 备份平台适配模块 | 备份状态、失败告警、容量趋势、恢复抽检 |
| T-058 | 自动化执行与审批模块 | 脚本审批、SSH / Ansible 执行、输出留痕 |
| T-060 | APM 链路追踪适配模块 | SkyWalking、OpenTelemetry、Jaeger、Pinpoint、Zipkin |

## 2.1 领域交付状态

状态由各领域真实配置和运行记录动态汇总，以下为 2026-07-20 的代码级验收结果；客户环境中的连接状态以接入健康页实时结果为准。

| 任务 | 状态 | 归属页面 | 已实现适配 | 暂不支持或受控能力 | 最近验证 |
|------|------|----------|------------|--------------------|----------|
| T-051 | active | `integration.notify` | 企业微信、钉钉、飞书、SMTP、短信、Webhook；幂等、重试、审计 | 自动外发需启用通道和策略 | 2026-07-20 |
| T-052 | active | `integration.itsm` | ServiceNow、Jira、禅道、Webhook；状态同步与来源关闭 | 蓝凌/泛微使用通用 Webhook | 2026-07-20 |
| T-053 | configured | `asset.center` | 统一资产、关系、实例绑定、内部发现与外部 CMDB 同步 | 缺失资产只标记待确认，不自动删除 | 2026-07-22 |
| T-054 | configured | `monitor.observability / logAnalysis` | Elasticsearch、OpenSearch、Loki、Graylog；24 小时时间窗和故障时间线 | 不持久化原始日志正文 | 2026-07-20 |
| T-055 | configured | `monitor.observability / grafana` | 版本化 Dashboard 模板和 JSON 导出 | 不直接写入 Grafana | 2026-07-20 |
| T-056 | configured | `backup.overview / platform` | Veeam、Commvault、NetBackup 字段映射，通用 REST 同步、失败告警、容量趋势 | 厂商原生 SDK 尚未接入 | 2026-07-20 |
| T-057 | configured | `ops.governance / objectStorage` | S3 兼容地址、NAS、制品校验、恢复命令和生命周期审批 | 上传和删除仍为审批受控方案 | 2026-07-20 |
| T-058 | configured | `protection.automation` | SSH、Ansible 执行器边界，职责分离、白名单、脚本哈希、输出脱敏 | 全局真实执行开关默认关闭 | 2026-07-20 |
| T-059 | configured | `system.identity` | LDAP/AD Bind、OIDC 授权码登录、组映射、登录审计、本地管理员兜底 | CAS、扫码登录未实现 | 2026-07-20 |
| T-060 | configured | `monitor.observability / apmTrace` | Jaeger、Zipkin 查询，多格式 Trace 标准化，SQL 指纹与诊断证据 | SkyWalking 需 GraphQL 适配；OTel Collector/Pinpoint 无标准查询接口 | 2026-07-20 |

旧 `/api/v1/ecosystem/*` 数据不会删除。迁移接口写入幂等版本标记，旧配置、事件和方案只读展示；旧执行入口返回归属模块，不再产生第二套业务记录。

## 3. T-051 告警通知通道适配模块

菜单位置：

```text
系统设置
├── 通知通道配置

监控告警
├── 告警通知记录
```

支持通道：

| 通道 | 用途 |
|------|------|
| 企业微信 | 国内企业常用即时通知 |
| 钉钉 | 制造业客户常见 |
| 飞书 | 新型团队常用 |
| SMTP 邮件 | 通用通知 |
| 短信网关 | 严重故障升级 |
| 通用 Webhook | 对接客户自研平台 |

功能目标：

```text
1. 配置多通知通道。
2. 支持测试发送。
3. 支持按告警级别、模块、实例路由通知。
4. 记录通知发送历史。
5. 支持失败重试和失败原因记录。
```

可接入事件：

```text
TraceMind 告警中心
Zabbix 推送失败
Prometheus 告警
巡检 critical
归档失败
备份失败
```

## 4. T-052 ITSM / 工单系统适配模块

菜单位置：

```text
监控告警
├── 告警转工单
├── 工单记录
└── 处理闭环

系统设置
└── 工单系统配置
```

适配对象：

```text
ServiceNow
Jira
禅道
蓝凌 OA
泛微 OA
客户自研工单系统
通用 Webhook 工单接口
```

功能目标：

```text
1. 告警一键转工单。
2. 巡检报告生成工单。
3. AI 诊断结论生成工单描述。
4. 工单编号回写告警。
5. 工单状态同步回 TraceMind AI。
6. 工单关闭后更新告警处理状态。
```

闭环流程：

```text
发现告警
    ↓
AI 诊断生成处置建议
    ↓
生成工单
    ↓
分派人员
    ↓
处理并回写
    ↓
关闭告警
```

## 5. T-053 CMDB 资产同步与关系映射模块

菜单位置：

```text
资产与连接
├── 主机资产
├── 应用系统
├── 工厂 / 产线
├── CMDB 同步
└── 资产关系图
```

核心关系：

```text
工厂
  ↓
产线
  ↓
MES 应用
  ↓
数据库实例
  ↓
主机
  ↓
Zabbix Host / Prometheus Target
```

功能目标：

```text
1. 同步 CMDB 主机、应用、业务系统、组织关系。
2. 维护数据库实例与主机、应用、产线的映射。
3. 告警展示影响范围。
4. 巡检报告展示业务归属。
5. AI 诊断读取资产上下文。
```

## 6. T-054 日志平台适配模块

菜单位置：

```text
性能诊断
├── 日志关联分析
└── 故障时间线
```

适配对象：

```text
ELK / Elasticsearch
OpenSearch
Grafana Loki
SkyWalking 日志
Graylog
```

功能目标：

```text
1. 配置日志平台连接。
2. 按实例、应用、主机、时间范围查询日志。
3. 慢 SQL 时间点关联应用日志。
4. Oracle Alert Log / MySQL Error Log 纳入故障时间线。
5. AI 诊断融合日志上下文。
```

故障时间线示例：

```text
14:03 数据库出现锁等待
14:04 MES 过站接口超时
14:05 Zabbix CPU 飙高
14:06 Oracle Alert Log 出现 ORA 错误
```

## 7. T-055 Grafana Dashboard 模板导出模块

菜单位置：

```text
系统设置
└── Grafana 配置

报告中心
└── Grafana 面板模板
```

功能目标：

```text
1. 生成 TraceMind AI Overview Dashboard。
2. 生成 Oracle 表空间 Dashboard。
3. 生成 MySQL 复制 Dashboard。
4. 生成历史归档任务 Dashboard。
5. 生成 Zabbix 推送失败趋势 Dashboard。
6. 导出 dashboard.json。
```

前提能力：

```text
TraceMind AI 暴露 /metrics
Prometheus 抓取 TraceMind AI 指标
Grafana 使用 Prometheus 数据源
```

## 8. T-056 备份平台适配模块

菜单位置：

```text
日常运维
└── 备份恢复

监控告警
└── 备份告警
```

适配对象：

```text
Veeam
Commvault
NetBackup
国产备份平台
对象存储 S3 / MinIO
NAS / NFS
```

功能目标：

```text
1. 同步备份任务状态。
2. 备份失败告警。
3. 备份容量趋势。
4. 备份文件有效性检查。
5. 抽检恢复任务记录。
```

## 9. T-057 归档文件对象存储适配模块

菜单位置：

```text
数据治理
└── 历史归档中心
    └── 归档文件存储
```

适配对象：

```text
MinIO
S3
阿里云 OSS
腾讯云 COS
华为 OBS
本地 NAS
```

功能目标：

```text
1. CSV / EXPDP 文件上传对象存储。
2. 文件 MD5 / SHA256 校验。
3. 文件生命周期管理。
4. 文件下载地址记录。
5. 归档文件恢复命令生成。
```

安全原则：

```text
上传、删除、生命周期变更默认只生成方案。
后续结合审批模块后，审批通过才允许执行。
```

## 10. T-058 自动化执行与审批模块

菜单位置：

```text
自动化中心
├── 脚本方案
├── 审批执行
├── 执行记录
└── 回滚方案
```

适配对象：

```text
Ansible
SaltStack
JumpServer
堡垒机
SSH 白名单执行
客户审批系统
```

审批执行流程：

```text
生成脚本
    ↓
提交审批
    ↓
审批通过
    ↓
通过 SSH / Ansible 执行
    ↓
记录输出
    ↓
失败回滚
```

适用操作：

```text
表空间扩容
归档清理
统计信息收集
索引重建
历史归档
备份验证
```

安全原则：

```text
真实执行开关默认关闭；关闭时保持 plan_only。
开启后仍必须先审批，并校验当前登录身份、目标白名单和脚本哈希。
所有执行动作必须记录执行人、审批人、脚本内容、输出和结果。
```

## 11. T-059 企业认证与权限适配模块

菜单位置：

```text
系统设置
└── 企业认证配置

授权与用户
└── 外部身份映射
```

支持方式：

```text
LDAP
Active Directory
CAS
OAuth2
OIDC
企业微信扫码
钉钉扫码
MES 单点登录
```

功能目标：

```text
1. 对接企业统一身份源。
2. 外部用户映射 TraceMind 角色。
3. 支持部门、岗位、用户组映射权限。
4. 支持本地管理员兜底登录。
5. 记录登录审计。
```

## 12. T-060 APM 链路追踪适配模块

菜单位置：

```text
性能诊断
├── APM 链路分析
└── SQL 关联链路
```

适配对象：

```text
SkyWalking
OpenTelemetry
Jaeger
Pinpoint
Zipkin
```

功能目标：

```text
1. 通过 Trace ID 关联接口、SQL、数据库实例。
2. 查询慢接口对应 SQL。
3. 将 SQL_ID 与表访问画像、执行计划、索引建议联动。
4. 构建接口慢、SQL 慢、主机忙的完整证据链。
```

典型场景：

```text
MES 接口慢
    ↓
定位到具体接口
    ↓
定位到具体 SQL
    ↓
TraceMind 分析 SQL 执行计划、索引、表访问画像
```

## 13. 最终菜单结构建议

```text
系统设置
├── Zabbix 配置
├── Zabbix 推送配置
├── Prometheus 配置
├── 通知通道配置
├── 工单系统配置
├── CMDB 配置
├── 对象存储配置
└── 企业认证配置

资产与连接
├── 数据库实例
├── 主机资产
├── CMDB 资产同步
├── Zabbix 主机映射
└── 实例主机绑定

监控告警
├── 综合告警
├── 数据库告警
├── 主机告警
├── Zabbix 联动
├── Prometheus 告警
├── 告警通知记录
└── 告警转工单

性能诊断
├── SQL 分析
├── 表访问画像
├── 日志关联分析
└── APM 链路分析

数据治理
├── 历史归档中心
├── CSV / EXPDP 归档
├── 对象存储归档
└── 归档校验

自动化中心
├── 脚本方案
├── 审批执行
├── 执行记录
└── 回滚方案
```

## 14. 开发原则

```text
1. 先做配置、测试连接、记录日志，再做自动化执行。
2. 所有外部系统异常不能影响数据库巡检主流程。
3. 所有 Token、密码、Webhook Secret 必须加密存储。
4. 不在前端展示明文密钥。
5. 告警通知和工单创建需要支持重试和幂等。
6. 自动化执行必须先经过审批设计。
7. 涉及清理、扩容、DDL、归档删除等操作默认 plan_only。
8. /metrics 禁止输出 SQL 文本、密码、Token、长日志。
9. Prometheus label 禁止使用高基数字段。
10. 修改 main.jsx 必须使用 UTF-8，避免中文乱码。
```
