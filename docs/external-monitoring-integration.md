# TraceMind AI 外部监控联动方案

## 1. 建设目标

TraceMind AI 当前负责数据库运维、巡检、SQL 分析、表空间、归档、历史库、AI 诊断等专业能力。生产环境中的数据库故障通常还会受到主机、磁盘、网络、存储、容器、系统负载等因素影响，因此需要与客户现有监控体系打通。

后续通知、工单、CMDB、日志、Grafana、对象存储、企业认证和 APM 等生态适配路线见 [external-integration-roadmap.md](external-integration-roadmap.md)。

目标是形成以下职责边界：

| 系统 | 职责 |
|------|------|
| Zabbix | 主机、网络、硬件、传统系统告警联动 |
| Prometheus | 时序指标、趋势监控、Grafana 展示、云原生指标 |
| Alertmanager | Prometheus 告警路由、分组、去重、静默和通知 |
| TraceMind AI | 数据库专业诊断、SQL 分析、归档治理、AI 分析和处置方案生成 |

最终效果：

```text
数据库告警不孤立
主机告警能关联数据库
数据库巡检结果能进入客户现有告警体系
Prometheus 能展示 TraceMind AI 指标趋势
AI 诊断能同时结合数据库、主机、系统、告警上下文
```

## 2. 总体架构

```text
                ┌──────────────────────────────┐
                │           Zabbix              │
                │ 主机 / 网络 / 硬件 / 传统告警 │
                └──────────────▲───────────────┘
                               │
                Zabbix API 拉取│        Zabbix Sender 推送
                               │
┌──────────────────────────────┼──────────────────────────────┐
│                          TraceMind AI                         │
│ 数据库巡检 / SQL画像 / 表空间 / 归档 / 历史库 / AI诊断 / 报告 │
└──────────────────────────────┬──────────────────────────────┘
                               │
                        /metrics 指标暴露
                               │
                ┌──────────────▼───────────────┐
                │          Prometheus            │
                │      指标采集 / 时序存储       │
                └──────────────┬───────────────┘
                               │
                ┌──────────────▼───────────────┐
                │   Grafana / Alertmanager       │
                │ 趋势展示 / 告警路由 / Webhook  │
                └──────────────┬───────────────┘
                               │
                    Alertmanager Webhook 回调
                               │
                               ▼
                         TraceMind AI
```

TraceMind AI 不重复建设完整 Zabbix 或 Prometheus，而是作为数据库专业运维分析中心、外部监控上下文聚合中心、数据库告警标准化输出中心。

## 3. 菜单位置

推荐保留现有业务菜单，并新增或调整以下入口：

```text
系统设置
├── Zabbix 配置
├── Zabbix 推送配置
├── Prometheus 配置
├── Alertmanager Webhook
└── 指标导出配置

资产与连接
├── 数据库实例
├── 主机资产
├── Zabbix 主机映射
└── 实例主机绑定

监控告警
├── 综合告警
├── 数据库告警
├── 主机告警
├── Zabbix 联动
├── Prometheus 告警
├── Alertmanager 告警
└── 联动记录

AI 智能诊断
├── 数据库 AI 诊断
├── 巡检 AI 分析
├── 慢 SQL AI 分析
└── 综合故障诊断
```

## 4. 任务拆分

| 任务 | 名称 | 目标 |
|------|------|------|
| T-047 | Zabbix 数据接入与主机预警聚合 | 从 Zabbix 获取主机、Problem、指标，并与数据库实例绑定 |
| T-048 | TraceMind 告警推送到 Zabbix | 将 TraceMind 内部数据库告警推送到 Zabbix Trapper |
| T-050 | Prometheus 指标导出与 Alertmanager 接入 | 暴露 `/metrics`，接收 Alertmanager Webhook |

## 5. T-047：Zabbix 数据接入与主机预警聚合

### 5.1 功能目标

```text
Zabbix → TraceMind AI
```

用途：

```text
数据库实例关联主机状态
AI 诊断增加主机上下文
巡检报告展示主机风险
综合告警统一查看
```

### 5.2 主要功能

```text
1. 配置 Zabbix API 地址
2. 配置用户名密码或 API Token
3. 测试 Zabbix API 连接
4. 同步 Zabbix 主机
5. 同步 Zabbix 当前 Problem
6. 获取 CPU / 内存 / 磁盘 / 网络指标
7. 绑定 Zabbix 主机与 TraceMind 数据库实例
8. 数据库实例详情页展示关联主机状态
9. AI 诊断时读取关联主机告警
```

### 5.3 API 能力

| 功能 | Zabbix API |
|------|------------|
| 主机列表 | `host.get` |
| 监控项列表 | `item.get` |
| 当前问题 | `problem.get` |
| 触发器 | `trigger.get` |
| 历史数据 | `history.get` |
| 趋势数据 | `trend.get` |

数据库实例详情页增加“关联主机状态”区域：

```text
关联主机状态
├── CPU 使用率
├── 内存使用率
├── /u01 使用率
├── /oradata 使用率
├── /arch 使用率
├── 当前 Zabbix Problem
└── 最近 24 小时趋势
```

### 5.4 可观测与采集中心落地状态

当前已新增独立入口“可观测与采集中心”，用于统一承接 Zabbix、Prometheus、Alertmanager、SkyWalking、中间件、Exporter、主机与数据库直连采集。

Zabbix 侧已具备以下只读采集链路：

```text
Zabbix host.get
  -> /api/v1/observability/zabbix/sync-hosts
  -> observability_collect_log

Zabbix problem.get
  -> /api/v1/observability/zabbix/sync-problems
  -> observability_alert_event
  -> db_alert_event

Zabbix item.get
  -> /api/v1/observability/zabbix/sync-metrics
  -> observability_current_metric
  -> observability_metric_history
```

前端入口：

```text
监控告警
└── 可观测与采集中心
    ├── 数据源
    ├── 指标映射
    ├── 当前指标
    ├── 历史指标
    ├── Zabbix 接入
    ├── Prometheus 接入
    ├── Alertmanager 接入
    ├── 告警事件
    ├── 采集策略
    ├── 采集任务
    └── 采集日志
```

安全边界：

```text
所有 Zabbix 同步均为只读 API 调用。
不修改 Zabbix Host、Item、Trigger、Action。
不执行外部系统变更。
```

## 6. T-048：TraceMind 告警推送到 Zabbix

### 6.1 功能目标

```text
TraceMind AI → Zabbix
```

推荐链路：

```text
TraceMind AI 告警
    ↓
Zabbix Sender 协议
    ↓
Zabbix Trapper Item
    ↓
Zabbix Trigger
    ↓
Zabbix Problem
    ↓
Zabbix Action
```

Zabbix Trapper 用于接收外部系统主动发送的数据。TraceMind AI 只负责推送状态、级别、消息和详情，Problem 的产生与恢复仍由 Zabbix Trigger 处理。

### 6.2 Zabbix 侧建议配置

创建 Host：

```text
TraceMindAI
```

创建 Trapper Item：

```text
mesguard.alert.status[alert_id]
mesguard.alert.level[alert_id]
mesguard.alert.message[alert_id]
mesguard.alert.detail[alert_id]
```

Trigger 示例：

```text
mesguard.alert.status[alert_id].last() = 1
```

恢复条件：

```text
mesguard.alert.status[alert_id].last() = 0
```

### 6.3 告警推送格式

告警产生：

```json
{
  "host": "TraceMindAI",
  "alert_id": "oracle_tablespace_MESDB_USERS",
  "status": 1,
  "level": 4,
  "message": "MESDB USERS 表空间使用率超过 95%",
  "detail": {
    "db_type": "oracle",
    "instance": "MESDB",
    "module": "tablespace",
    "metric_name": "used_pct",
    "metric_value": "96.3%",
    "threshold": "95%",
    "suggestion": "建议检查大对象增长，必要时扩容或清理历史数据"
  }
}
```

告警恢复：

```json
{
  "host": "TraceMindAI",
  "alert_id": "oracle_tablespace_MESDB_USERS",
  "status": 0,
  "level": 0,
  "message": "MESDB USERS 表空间告警已恢复"
}
```

### 6.4 告警 ID 规范

```text
oracle_tablespace_{instance}_{tablespace}
oracle_fra_{instance}
oracle_asm_{instance}_{diskgroup}
oracle_dg_lag_{instance}
oracle_blocking_session_{instance}
mysql_replication_{instance}
mysql_connection_{instance}
mysql_slow_sql_{instance}
archive_job_failed_{job_id}
backup_job_failed_{job_id}
host_filesystem_{host}_{mount}
```

### 6.5 告警级别映射

| TraceMind 等级 | Zabbix Severity | 说明 |
|---------------|----------------:|------|
| normal | 0 | 正常 |
| info | 1 | 信息 |
| warning | 2 | 警告 |
| average | 3 | 一般严重 |
| high | 4 | 严重 |
| critical / disaster | 5 | 灾难 |

## 7. T-050：Prometheus 指标导出与 Alertmanager 接入

### 7.1 功能目标

```text
TraceMind AI → Prometheus
Prometheus / Alertmanager → TraceMind AI
```

TraceMind AI 暴露标准 `/metrics`，由 Prometheus 定时抓取。Alertmanager 通过 Webhook 回调 TraceMind AI，形成告警上下文闭环。

### 7.2 指标接口

新增接口：

```text
GET /metrics
```

示例：

```text
mesguard_oracle_tablespace_used_pct{instance="MESDB",tablespace="USERS"} 96.3
mesguard_oracle_fra_used_pct{instance="MESDB"} 82.5
mesguard_oracle_blocking_sessions{instance="MESDB"} 3
mesguard_mysql_replication_lag_seconds{instance="MESMYSQL01"} 120
mesguard_slow_sql_count{instance="MESDB",db_type="oracle"} 35
mesguard_archive_job_status{job="archive_20260525",instance="MESDB"} 1
mesguard_backup_job_status{job="backup_20260525",instance="MESDB"} 0
```

Prometheus scrape 配置示例：

```yaml
scrape_configs:
  - job_name: "mesguard-db"
    metrics_path: "/metrics"
    static_configs:
      - targets:
          - "192.168.1.100:8000"
```

### 7.3 指标分类

Oracle 指标：

```text
表空间使用率
FRA 使用率
ASM 磁盘组使用率
归档增长量
阻塞会话数量
活动会话数量
Data Guard 延迟
无效对象数量
慢 SQL 数量
巡检 critical 数量
巡检 warning 数量
```

MySQL 指标：

```text
连接数使用率
主从复制延迟
复制线程状态
慢 SQL 数量
大表数量
表碎片大小
锁等待数量
```

平台任务指标：

```text
巡检任务状态
备份任务状态
归档任务状态
历史库迁移任务状态
AI 诊断任务数量
Zabbix 推送失败次数
```

### 7.4 Alertmanager Webhook

新增接口：

```text
POST /api/v1/prometheus/alertmanager/webhook
```

Alertmanager 配置示例：

```yaml
receivers:
  - name: "mesguard-webhook"
    webhook_configs:
      - url: "http://192.168.1.100:8000/api/v1/prometheus/alertmanager/webhook"
```

## 8. 综合告警中心

### 8.1 告警来源

```text
TraceMind 数据库巡检告警
TraceMind 历史归档任务告警
TraceMind 备份任务告警
Zabbix Problem
Prometheus / Alertmanager Alert
主机资源异常
```

### 8.2 统一字段

| 字段 | 说明 |
|------|------|
| alert_id | 统一告警 ID |
| source | mesguard / zabbix / prometheus |
| severity | 告警等级 |
| status | firing / resolved |
| instance_id | 关联数据库实例 |
| host_id | 关联主机 |
| alert_name | 告警名称 |
| metric_name | 指标名 |
| metric_value | 指标值 |
| threshold_value | 阈值 |
| first_seen | 首次发现时间 |
| last_seen | 最近发现时间 |
| suggestion | 处置建议 |
| raw_data | 原始告警 JSON |

### 8.3 页面结构

```text
监控告警 → 综合告警
├── 当前活动告警
├── 已恢复告警
├── 数据库告警
├── 主机告警
├── Zabbix 告警
├── Prometheus 告警
└── 联动记录
```

## 9. AI 诊断联动

AI 诊断输入需要扩展为：

```text
数据库巡检结果
慢 SQL 分析结果
表空间 / FRA / ASM 状态
Data Guard 状态
Zabbix 当前 Problem
Prometheus 当前告警
主机 CPU / 内存 / 文件系统
归档 / 备份任务状态
```

示例输出：

```text
TraceMind 发现 USERS 表空间 96%
Zabbix 发现 /oradata 文件系统 95%
Prometheus 指标显示磁盘持续增长

AI 诊断：
根因不是单纯表空间问题，而是数据文件所在文件系统空间不足。
建议先扩容文件系统或释放 /oradata 空间，再执行表空间扩容。
```

## 10. 数据表规划

```sql
CREATE TABLE zabbix_config (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  config_name VARCHAR(100) NOT NULL,
  base_url VARCHAR(500) NOT NULL,
  username VARCHAR(100),
  password_cipher TEXT,
  api_token_cipher TEXT,
  enabled INT DEFAULT 1,
  remark VARCHAR(500),
  create_time DATETIME,
  update_time DATETIME
);

CREATE TABLE zabbix_host_mapping (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  zabbix_config_id BIGINT NOT NULL,
  zabbix_hostid VARCHAR(64) NOT NULL,
  zabbix_host_name VARCHAR(200),
  visible_name VARCHAR(200),
  ip_address VARCHAR(100),
  db_instance_id BIGINT,
  enabled INT DEFAULT 1,
  create_time DATETIME,
  update_time DATETIME
);

CREATE TABLE zabbix_problem_cache (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  zabbix_config_id BIGINT NOT NULL,
  eventid VARCHAR(64),
  objectid VARCHAR(64),
  hostid VARCHAR(64),
  host_name VARCHAR(200),
  problem_name VARCHAR(500),
  severity INT,
  severity_name VARCHAR(50),
  clock DATETIME,
  status VARCHAR(30),
  raw_data JSON,
  sync_time DATETIME
);

CREATE TABLE zabbix_push_config (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  config_name VARCHAR(100) NOT NULL,
  zabbix_server VARCHAR(200) NOT NULL,
  zabbix_port INT DEFAULT 10051,
  zabbix_host VARCHAR(200) NOT NULL,
  enabled INT DEFAULT 1,
  timeout_seconds INT DEFAULT 10,
  remark VARCHAR(500),
  create_time DATETIME,
  update_time DATETIME
);

CREATE TABLE zabbix_push_log (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  alert_id VARCHAR(200),
  alert_name VARCHAR(500),
  alert_level VARCHAR(50),
  zabbix_host VARCHAR(200),
  zabbix_key VARCHAR(500),
  push_status VARCHAR(30),
  response_text TEXT,
  error_message TEXT,
  push_time DATETIME
);

CREATE TABLE prometheus_config (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  config_name VARCHAR(100),
  prometheus_url VARCHAR(500),
  alertmanager_url VARCHAR(500),
  enabled INT DEFAULT 1,
  remark VARCHAR(500),
  create_time DATETIME,
  update_time DATETIME
);

CREATE TABLE prometheus_alert_cache (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  alert_name VARCHAR(200),
  alert_status VARCHAR(50),
  severity VARCHAR(50),
  instance_name VARCHAR(200),
  db_instance_id BIGINT,
  labels_json JSON,
  annotations_json JSON,
  starts_at DATETIME,
  ends_at DATETIME,
  raw_data JSON,
  create_time DATETIME,
  update_time DATETIME
);
```

## 11. 后端 API 规划

> 本章是**规划**清单。2026-09-21 逐条核对：下列 Zabbix 路由中除最后一条
> `GET /api/v1/zabbix/hosts/{hostid}/overview` **未实现**（后端无此路由）外，
> 其余均已在 `app/api/v1/zabbix.py` 注册。未实现项保留在此作为待办，不要当成可用接口。

### 11.1 Zabbix API 接入

```text
GET    /api/v1/zabbix/configs
POST   /api/v1/zabbix/configs
PUT    /api/v1/zabbix/configs/{id}
DELETE /api/v1/zabbix/configs/{id}
POST   /api/v1/zabbix/configs/{id}/test
POST   /api/v1/zabbix/configs/{id}/sync-hosts

GET    /api/v1/zabbix/hosts
POST   /api/v1/zabbix/hosts/{host_mapping_id}/bind-db-instance

GET    /api/v1/zabbix/problems
POST   /api/v1/zabbix/problems/sync

GET    /api/v1/zabbix/hosts/{hostid}/items
GET    /api/v1/zabbix/hosts/{hostid}/history
GET    /api/v1/zabbix/hosts/{hostid}/trends
GET    /api/v1/zabbix/hosts/{hostid}/overview      ← 未实现
```

### 11.2 Zabbix 推送

```text
GET  /api/v1/zabbix-push/config
POST /api/v1/zabbix-push/config
PUT  /api/v1/zabbix-push/config/{id}
POST /api/v1/zabbix-push/config/{id}/test

POST /api/v1/zabbix-push/alert/test
POST /api/v1/zabbix-push/alert/{alert_id}/push
POST /api/v1/zabbix-push/alert/{alert_id}/recover

GET  /api/v1/zabbix-push/logs
```

### 11.3 Prometheus / Alertmanager

```text
GET  /metrics

GET  /api/v1/prometheus/config
POST /api/v1/prometheus/config
POST /api/v1/prometheus/test-metrics

POST /api/v1/prometheus/alertmanager/webhook

GET  /api/v1/prometheus/alerts
GET  /api/v1/prometheus/instance/{instance_id}/alerts
```

## 12. 文件规划与实际落点

> ⚠️ **本章是最初的规划，实现时做过合并。** 下表已按 2026-09-20 的实际布局核对，
> 但仍请以代码为准，不要按"规划"去找文件。

**模型：规划 7 个独立文件，实际集中在 1 个文件。** 7 张表
（`zabbix_config`、`zabbix_host_mapping`、`zabbix_problem_cache`、`zabbix_push_config`、
`zabbix_push_log`、`prometheus_config`、`prometheus_alert_cache`）全部定义在
`backend/app/models/external_monitoring.py`。

**后端：规划即实际**，以下 12 个文件均已存在：

```text
backend/app/services/zabbix_client.py
backend/app/services/zabbix_service.py
backend/app/services/zabbix_sender.py
backend/app/services/zabbix_push_service.py
backend/app/services/prometheus_metrics.py
backend/app/services/alertmanager_service.py

backend/app/schemas/zabbix.py
backend/app/schemas/zabbix_push.py
backend/app/schemas/prometheus.py

backend/app/api/v1/zabbix.py
backend/app/api/v1/zabbix_push.py
backend/app/api/v1/prometheus.py
```

**前端：规划 11 个按实体拆分的组件，实际合并为按集成方组织的页面。**
组件平铺于 `frontend/src/`（无 `pages/` 子目录）：

```text
frontend/src/ZabbixIntegrationPage.jsx
frontend/src/PrometheusIntegrationPage.jsx
frontend/src/UnifiedAlertEventPage.jsx
frontend/src/AlertCenterPage.jsx
frontend/src/ExternalMonitoringPages.jsx
frontend/src/MonitoringPages.jsx
```

规划中的 `ZabbixConfigPage`、`ZabbixHostPage`、`ZabbixProblemPage`、`ZabbixPushConfigPage`、
`ZabbixPushLogPage`、`PrometheusConfigPage`、`PrometheusMetricPage`、`PrometheusAlertPage`、
`UnifiedAlertPage`、`InstanceHostStatusCard`、`ExternalMonitorOverviewCard` **均未落地**
（全 `frontend/src` 搜不到定义）。按实体拆分的页面被合并进上表的集成页。

## 13. 安全要求

```text
1. Zabbix API Token、密码必须加密存储。
2. 不在前端展示明文 Token 或密码。
3. Zabbix API 请求必须设置超时。
4. Zabbix / Prometheus 异常不能影响数据库巡检主流程。
5. Zabbix 推送失败只记录日志，不中断巡检。
6. Zabbix 推送失败最多重试 3 次。
7. /metrics 不输出 SQL 文本、密码、Token、长日志。
8. Prometheus label 禁止使用 sql_text、alert_message、随机 batch_id 等高基数字段。
9. Alertmanager Webhook 接入需要支持简单鉴权或内网白名单。
10. 修改 main.jsx 必须使用 Python UTF-8 脚本，避免中文乱码。
11. 保持端口架构不变：开发 3000/8000，EXE 18080。
```

## 14. 实施阶段

### 阶段一：Zabbix 推送优先

```text
Zabbix 推送配置
测试推送
TraceMind 告警推送到 Zabbix
推送日志
告警恢复推送
```

价值最直观，客户可以先把数据库告警纳入现有通知体系。

### 阶段二：Zabbix 数据接入

```text
Zabbix API 配置
同步主机
同步 Problem
绑定数据库实例
数据库实例详情展示主机状态
```

用于提升数据库诊断与 AI 分析的上下文质量。

### 阶段三：Prometheus 指标导出

```text
/metrics
Oracle / MySQL / 平台任务指标
Prometheus scrape config
Grafana Dashboard 指标说明
```

适合趋势图、长期指标和客户已有 Prometheus 环境。

### 阶段四：Alertmanager 接入

```text
Alertmanager Webhook
Prometheus 告警入库
Prometheus 告警关联数据库实例
AI 综合诊断读取 Prometheus 告警上下文
```

## 15. 最终价值

```text
数据库告警进入 Zabbix
Zabbix 告警进入 TraceMind
TraceMind 指标进入 Prometheus
Prometheus 告警进入 TraceMind
AI 诊断综合数据库 + 主机 + 系统 + 告警上下文
```

最终形成：

```text
统一监控
统一告警
统一诊断
统一报告
统一处置建议
```

定位：

```text
Zabbix / Prometheus 负责发现问题
TraceMind AI 负责解释问题、生成方案、沉淀知识
```
