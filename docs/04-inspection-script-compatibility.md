# TraceMind AI 巡检脚本对标说明

更新日期：2026-06-16

2026-09-11 维护补记：已将前端展示路径更新为当前组件。下列章节覆盖数量与验收结果保留原记录口径，不代表本轮重新完成四引擎现场验收；文件清理与当前验证见精简记录。

本文记录 TraceMind AI 一键巡检对 `mysql_check.sh`、`pg_check.sh`、`mssql_check.sh` 等脚本的对标实现方式、章节覆盖范围、结果结构和验收状态。

## 1. 模块定位

一键巡检的目标不是简单返回少量健康指标，而是把传统 DBA 巡检脚本中的完整章节纳入平台：

```text
脚本化巡检内容
  ↓
结构化 Finding
  ↓
统一巡检报告
  ↓
证据链 / AI 诊断 / 工单
```

当前实现重点：

| 数据库 | 对标脚本 | 覆盖状态 | 入口 |
| --- | --- | --- | --- |
| MySQL | `mysql_check.sh` v3.5 | 已覆盖 20 个采集点 | `collect_mysql_script_findings` |
| PostgreSQL | `pg_check.sh` v1.0 | 已覆盖 13 个章节 | `collect_postgresql_script_findings` |
| SQL Server | `mssql_check.sh` | 已覆盖 17 个章节 | `generic_db_ops.inspection_findings` |

## 2. 代码位置

核心实现：

```text
backend/app/services/inspection/script_db_checks.py
```

统一巡检入口：

```text
backend/app/services/inspection/runner.py
backend/app/services/inspection/collectors/postgresql_inspection_collector.py
```

前端结果展示：

```text
frontend/src/InspectionReportDetailPage.jsx
frontend/src/ReportPages.jsx
```

报告列表与任务/一键巡检详情均使用现有报告组件；无消费者的旧结果组件已退役，后端兼容接口保留。

## 3. MySQL 巡检覆盖矩阵

MySQL 一键巡检对标 `E:/chromedownload/mysql_check.sh`，输出 20 个结构化章节：

| 序号 | 章节 | 主要内容 |
| --- | --- | --- |
| 1 | 基础信息 | 版本、主机名、端口、server_id、字符集、basedir、datadir、Uptime |
| 2 | 连接配置 | max_connections、Threads_connected、Threads_running、Max_used_connections、timeout、skip_name_resolve |
| 3 | 内存配置 | buffer pool、sort/join/read buffer、tmp_table_size、table cache、thread cache |
| 4 | 日志配置 | slow query、binlog、long_query_time、redo log、flush/sync 策略 |
| 5 | InnoDB 配置 | file_per_table、flush_method、doublewrite、IO threads、dirty page、AHI、page size |
| 6 | 性能指标 | 查询统计、InnoDB 性能、锁/临时表/连接/流量 |
| 6.5 | 实时采样指标 | 1 秒采样 QPS、TPS、读写 QPS、InnoDB IOPS、Buffer Pool 实时命中率 |
| 7 | 数据库信息 | 数据库大小、当前进程列表、存储引擎 |
| 8 | 安全信息 | mysql.user、复制状态、复制配置 |
| 9 | InnoDB 深度诊断 | InnoDB Status、最近死锁、长事务、锁等待 |
| 10 | 表空间分析 | 大表、无主键表、碎片、未使用索引、冗余索引、表级 IO 热点 |
| 11 | 等待事件 | performance_schema 等待事件 Top 20 |
| 12 | SSL/TLS 安全状态 | SSL 配置、当前连接 SSL 状态 |
| 13 | 密码策略与认证安全 | validate_password、旧认证插件、无密码账号 |
| 14 | auto_increment 使用率 | 自增列当前值、最大值、使用率预警 |
| 15 | 长查询 | 运行超过 30 秒的非 Sleep 查询 |
| 16 | 复制深度诊断 | Binlog 状态、从库列表、复制状态、半同步、Group Replication、过滤器、GTID、复制通道 |
| 17 | 备份检查 | Binlog 备份参数、Binlog 文件、InnoDB 持久化、备份状态指标、备份事件 |
| 18 | 系统资源 | 数据库可见主机信息；CPU/内存/磁盘/IO/网络需绑定 SSH、Zabbix 或 Prometheus 补齐 |
| 19 | 专家级调优指标 | Handler 读取模式统计，对标 MySQLTuner 风格分析 |

## 4. PostgreSQL 巡检覆盖矩阵

PostgreSQL 一键巡检对标 `E:/chromedownload/pg_check.sh`，输出 13 个结构化章节：

| 序号 | 章节 | 主要内容 |
| --- | --- | --- |
| 1 | 基本信息 | version、current_database、current_user、启动时间、数据目录、配置文件、恢复模式 |
| 2 | 连接信息 | 最大连接、超级用户预留、当前连接、连接状态分布、用户连接分布 |
| 3 | 数据库信息 | 数据库大小、编码、排序规则、连接限制、当前连接 |
| 4 | 表统计 | 活跃行、死元组、死元组率、VACUUM/ANALYZE 时间 |
| 5 | 索引使用分析 | 索引扫描率、未使用索引 |
| 6 | 性能指标 | 缓存命中率、BGWriter/Checkpointer、事务提交与回滚 |
| 7 | 复制状态 | wal_level、archive_mode、复制插槽、主库 WAL 发送或备库 WAL 接收 |
| 8 | 锁信息 | 锁概览、锁等待详情 |
| 9 | 长查询 | 运行超过 30 秒的查询 |
| 10 | 等待事件 | wait_event_type、wait_event、会话状态 |
| 11 | 安全信息 | 角色、超级用户、扩展 |
| 12 | 关键参数 | shared_buffers、work_mem、wal、autovacuum、ssl、track_counts 等 |
| 13 | 系统信息 | 数据库可见地址、端口、目录、backend_pid；OS 指标需绑定主机监控源补齐 |

## 5. 结果结构

每个脚本章节最终都会转换为一条 `Finding`：

```json
{
  "check_category": "6. 性能指标",
  "check_item": "性能指标 巡检",
  "check_status": "warning",
  "metric_name": "buffer_pool_hit_pct",
  "metric_value": "99.64",
  "threshold_value": ">=95%",
  "raw_data": {
    "engine": "MySQL",
    "script_name": "mysql_check.sh",
    "script_version": "3.5",
    "expected_section_total": 20,
    "actual_section_total": 20,
    "read_only": true,
    "html_section_id": "performance",
    "summary": {},
    "items": [],
    "sql": [],
    "warnings": []
  }
}
```

前端展开明细时读取：

```text
raw_data.summary
raw_data.items
raw_data.sql
raw_data.warnings
```

空结果不会再返回空数组，而是返回一条 `_empty=true` 的说明行：

```json
{
  "subsection": "14.1 auto_increment 使用率",
  "采集状态": "无数据",
  "说明": "当前查询未返回明细行，等价于脚本中的空表/无相关对象。",
  "_empty": true
}
```

这样可以避免前端展开后出现“暂无采集明细”的误解。

## 6. 安全边界

巡检只读执行：

```text
read_only = true
status = plan_only
```

数据库变更类动作不在巡检中执行，包括：

```text
KILL
扩容
删除
重建索引
更新统计信息
恢复
主备切换
备份清理
```

巡检只生成证据链、风险判断和 plan_only 建议。

## 7. 系统资源章节说明

MySQL 和 PostgreSQL 脚本中包含本机 shell 采集项，例如：

```text
CPU
内存
磁盘
iostat
vmstat
网络接口
TCP 状态
数据库进程资源
```

平台通过数据库连接远程巡检，不能默认在数据库主机执行 shell 命令。因此这些章节当前处理方式为：

1. 返回数据库连接可见的主机、目录、端口、进程标识等信息。
2. 对 OS 级指标返回“需要主机监控源”的结构化明细。
3. 后续通过 SSH 资产、Zabbix Host、Prometheus Target 绑定补齐持续采集数据。

这避免了数据库账号越权执行系统命令，也符合生产环境最小权限原则。

## 8. 验收记录

2026-06-15 至 2026-06-16 已完成以下验证：

| 验证项 | 结果 |
| --- | --- |
| `python -m py_compile backend/app/services/inspection/script_db_checks.py ...` | 通过 |
| 真实 MySQL 实例 ID=6 脚本化巡检 | 返回 20 个章节 |
| 真实 PostgreSQL 实例 ID=9 脚本化巡检 | 返回 13 个章节，任务 `20260616105136617560`，评分 98 |
| `runner.run_unified_inspection` | 成功写入 20 条巡检结果，生成 20 个模块 |
| `POST /api/v1/instances/9/test` | 连接测试环境 PostgreSQL 成功，返回 PostgreSQL 15.4 |
| `python scripts/check_backend_routes.py` | 通过 |
| `python scripts/check_backend_static.py` | 通过 |
| `node scripts/check_frontend_static.cjs` | 通过 |
| `npm run build` | 通过 |

PostgreSQL 已完成真实实例验证；具体地址与凭据不写入仓库，巡检结果覆盖 13 个章节。

## 9. PostgreSQL 连接故障处理记录

2026-06-16 处理测试环境 PostgreSQL 无法连接问题。

### 现象

页面测试 PostgreSQL 实例连接失败，后端日志显示：

```text
psycopg2-binary driver is not installed
```

端口检测结果：

```text
Test-NetConnection <DB_HOST> -Port <DB_PORT>
TcpTestSucceeded: True
```

说明网络和端口可达，问题不在防火墙或监听端口。

### 根因

当前后端使用 `D:\python\python.exe`，版本为 Python 3.14。`psycopg2-binary==2.9.10` 在该环境下没有可用预编译包，安装时会退回源码编译，并因为缺少 `pg_config` 失败。

### 修复

1. 安装 `psycopg[binary]==3.3.4`。
2. PostgreSQL 连接器支持双驱动：
   - 优先使用 `psycopg2`。
   - 未安装 `psycopg2` 时自动回退到 `psycopg 3`。
3. `backend/app/collectors/utils.py` 补充 `time/timedelta` JSON 安全转换，解决 PostgreSQL `interval` 类型导致巡检入库 500 的问题。
4. `backend/requirements.txt` 更新为 `psycopg[binary]==3.3.4`。

### 验证

```text
D:\python\python.exe -m py_compile backend/app/collectors/utils.py backend/app/collectors/postgresql/postgresql_connection.py
D:\python\python.exe scripts/check_backend_static.py
POST /api/v1/instances/9/test
POST /api/v1/inspection/postgresql/9/run      ← 历史路径，现不存在
```

验证结果：

```text
连接成功
current_user=postgres
database=postgres
PostgreSQL version=15.4
capability=full_inspection
巡检章节=13
巡检评分=98
```

> **补注（2026-09-21）**：本节是**当时的实测记录**（环境、驱动回退与结果），保留原样不改写。
> 但上面 `POST /api/v1/inspection/postgresql/9/run` 是**历史路径**，现已不存在——
> 当前等价入口是 `POST /api/v1/postgresql/{instance_id}/inspect`。
> 照抄旧路径会拿到 404。

## 10. 后续维护规则

脚本升级后按以下步骤同步平台：

1. 对比脚本新增/删除章节。
2. 更新 `script_db_checks.py` 中对应 `_mysql_sections` 或 `_pg_sections`。
3. 保持每个章节输出 `summary/items/sql/warnings`。
4. 空结果使用 `_empty=true` 明细，不返回纯空数组。
5. 高风险建议仍保持 plan_only，不执行变更。
6. 运行后端编译、后端静态检查、前端静态检查和 `npm run build`。
