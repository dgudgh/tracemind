# 巡检报告模块 (Reports)

## 概述
提供数据库健康巡检功能，通过数据库连接执行 SQL 查询收集 27 项巡检数据，结果自动保存到数据库，支持历史报告查看和 Word 导出。

## 数据库表：db_check_report
| 字段 | 类型 | 说明 |
|------|------|------|
| id | int | 主键 |
| instance_id | int | 数据库实例 ID |
| instance_name | string | 实例名称 |
| total | int | 检查项总数 |
| success | int | 通过数 |
| failed | int | 失败数 |
| result_json | text | 完整结果 JSON |
| create_time | datetime | 创建时间 |

## 巡检检查项（27 项）

### 基础信息
| key | 标题 | 说明 |
|-----|------|------|
| instance_info | 实例信息 | DBID、数据库名、角色、创建时间、日志模式、版本 |
| instance_host | 主机信息 | 主机名、实例名、状态、启动时间 |
| db_version | 数据库版本 | Banner 信息 |
| db_components | 已安装组件 | comp_id、comp_name、version、status |

### 参数配置
| key | 标题 | 说明 |
|-----|------|------|
| db_params | 核心参数 | SGA、PGA、processes、sessions 等核心参数 |
| spfile_check | SPFILE 检查 | 检查是否使用 SPFILE |
| memory_params | 内存参数 | 内存分配参数（GB 单位） |

### 存储管理
| key | 标题 | 说明 |
|-----|------|------|
| control_files | 控制文件 | 控制文件路径和状态 |
| redo_logs | Redo Log | 日志组、序列号、大小 |
| redo_switch | Redo 切换频率 | 近 7 天每日切换次数 |
| archive_rate | 归档日志产生量 | 近 7 天每日归档量（MB） |
| tablespaces | 表空间使用率 | 每个表空间的总大小、已用、空闲、使用率 |
| datafiles | 数据文件 | 数据文件路径、大小、自动扩展 |
| temp_files | 临时文件 | 临时文件路径和大小 |
| recyclebin | 回收站 | 回收站对象数量和大小 |

### 安全与用户
| key | 标题 | 说明 |
|-----|------|------|
| users | 数据库用户 | 非系统用户列表、账号状态 |
| profiles | Profile 配置 | 资源限制配置 |
| resource_limit | 资源限制 | 当前资源使用情况 |

### 性能诊断
| key | 标题 | 说明 |
|-----|------|------|
| sga_info | SGA 信息 | SGA 各组件大小 |
| pga_info | PGA 信息 | PGA 统计信息 |
| sessions | 会话统计 | 按状态和类型分组统计 |
| lock_info | 当前锁等待 | 等待超过 10 秒的锁信息 |
| top_sql | TOP SQL | 按执行时间排名的 Top 20 SQL |

### 对象与任务
| key | 标题 | 说明 |
|-----|------|------|
| invalid_objects | 失效对象 | 非系统用户的失效对象 |
| db_links | 数据库链接 | DB Link 配置 |
| jobs | 定时任务 | 失败或中断的 DBMS_JOB |
| dg_config | Data Guard 配置 | 归档目的地配置 |

## API 列表

### GET /api/v1/reports
获取历史巡检报告列表（最近 50 条）。

**响应 (200)**
```json
{
  "items": [
    {
      "id": 1,
      "instance_id": 1,
      "instance_name": "ORCL_PROD",
      "total": 27,
      "success": 25,
      "failed": 2,
      "create_time": "2025-05-12 15:00:00"
    }
  ]
}
```

---

### POST /api/v1/reports/dbcheck
执行巡检并自动保存报告。

**请求体 (JSON)**
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| instance_id | int | 是 | 数据库实例 ID |

**响应 (200)**
```json
{
  "ok": true,
  "instance_name": "ORCL_PROD",
  "report_id": 1,
  "total": 27,
  "success": 25,
  "failed": 2,
  "items": [
    {
      "key": "instance_info",
      "title": "实例信息",
      "columns": ["DBID", "NAME", "DATABASE_ROLE"],
      "rows": [["1234567890", "ORCL", "PRIMARY"]],
      "row_count": 1,
      "status": "ok"
    },
    {
      "key": "invalid_objects",
      "title": "失效对象",
      "columns": [],
      "rows": [],
      "row_count": 0,
      "status": "error",
      "error": "ORA-00942: table or view does not exist"
    }
  ],
  "errors": [],
  "generated_at": "2025-05-12T15:00:00"
}
```

---

### GET /api/v1/reports/{report_id}
查看历史报告详情。

**路径参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| report_id | int | 报告 ID |

**响应** 同 `POST /dbcheck` 的响应格式

---

### DELETE /api/v1/reports/{report_id}
删除历史报告。

---

### GET /api/v1/reports/{report_id}/export-word
导出 Word 报告。

**路径参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| report_id | int | 报告 ID |

**响应** 返回 `.docx` 文件流
- Content-Type: `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
- Content-Disposition: `attachment; filename*=UTF-8''dbcheck_ORCL_PROD_20250512_150000.docx`

**Word 报告内容：**
- 标题页（Oracle 数据库巡检报告）
- 实例信息、巡检时间、统计汇总表
- 每项检查的标题（✓/✗ 状态标识）和数据表格
