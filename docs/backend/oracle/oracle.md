# Oracle 运维模块 (Oracle)

## 概述
Oracle 数据库运维中心，涵盖基础信息、启停管理、参数管理、安全检查、Redo Log、锁会话、表空间监控、表和索引、备份恢复、归档/FRA、Oracle 用户管理、巡检、慢 SQL 实时监控等功能。

所有接口前缀：`/api/v1/oracle`

## 基础信息

### GET /{instance_id}/basic-info
获取数据库基础信息（版本、字符集、实例名等）。

### GET /{instance_id}/db-status
获取数据库运行状态。

### GET /{instance_id}/boot-prerequisites
获取启动前置条件检查。

### GET /{instance_id}/boot-script
生成启动脚本。

### GET /{instance_id}/startup-script
生成 STARTUP 命令脚本。

### GET /{instance_id}/shutdown-script
生成 SHUTDOWN 命令脚本。

---

## 参数管理

### GET /{instance_id}/parameters
获取所有初始化参数。

**Query 参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| keyword | string | 按参数名搜索 |

### GET /{instance_id}/parameters/sga
获取 SGA 相关参数。

### GET /{instance_id}/parameters/pga
获取 PGA 相关参数。

### GET /{instance_id}/parameters/process
获取进程相关参数。

### GET /{instance_id}/parameters/session
获取会话相关参数。

### GET /{instance_id}/parameters/datafile
获取数据文件相关参数。

### GET /{instance_id}/alter-system-script
生成 ALTER SYSTEM 修改参数的脚本。

**Query 参数**
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | string | 是 | 参数名 |
| value | string | 是 | 参数值 |
| scope | string | 否 | `both`、`spfile`、`memory` |

---

## 表空间监控

### GET /{instance_id}/tablespaces
获取表空间列表。

### GET /{instance_id}/tablespace-overview
获取表空间概览。

### GET /{instance_id}/tablespace/{tablespace_name}/datafiles
获取指定表空间的数据文件。

### POST /{instance_id}/tablespaces/create-script
生成创建表空间脚本。

**请求体**
| 参数 | 类型 | 说明 |
|------|------|------|
| tablespace_name | string | 表空间名 |
| datafile_path | string | 数据文件路径 |
| size_mb | int | 初始大小（MB） |
| autoextend | bool | 是否自动扩展 |
| maxsize_mb | int | 最大大小 |

### POST /{instance_id}/tablespaces/create-temp-script
生成创建临时表空间脚本。

### DELETE /{instance_id}/tablespaces/{tablespace_name}/drop-script
生成删除表空间脚本。

### POST /{instance_id}/tablespace-expand-script
生成表空间扩容脚本。

**请求体**
| 参数 | 类型 | 说明 |
|------|------|------|
| tablespace_name | string | 表空间名 |
| add_size_mb | int | 增加大小（MB） |

### GET /{instance_id}/omf-status
获取 Oracle Managed Files 状态。

### POST /{instance_id}/tablespaces/deploy-autoextend
通过 SSH 部署自动扩展脚本。

---

## 表和索引

### GET /{instance_id}/table-fragmentation
获取表碎片分析。

**Query 参数**
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| owner | string | - | Schema 名称 |
| min_size_mb | float | 10 | 最小表大小（MB） |
| limit | int | 100 | 返回条数上限 |

### GET /{instance_id}/table-shrink-script
生成表碎片整理（SHRINK SPACE）脚本。

**Query 参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| owner | string | Schema |
| table_name | string | 表名 |

### GET /{instance_id}/index-fragmentation
获取索引碎片分析。

**Query 参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| owner | string | Schema 名称 |

### GET /{instance_id}/index-rebuild-script
生成索引重建脚本。

**Query 参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| owner | string | Schema |
| index_name | string | 索引名 |

### GET /{instance_id}/unused-indexes
获取未使用索引分析（90 天内未出现在执行计划中）。

**Query 参数**
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| owner | string | - | Schema 名称 |
| days | int | 90 | 分析天数（7-365） |

**响应**
```json
{
  "indexes": [
    {
      "owner": "SCOTT",
      "table_name": "EMP",
      "index_name": "IDX_EMP_NAME",
      "index_type": "NORMAL",
      "uniqueness": "NONUNIQUE",
      "status": "VALID",
      "visibility": "VISIBLE",
      "num_rows": 1000,
      "leaf_blocks": 5,
      "distinct_keys": 1000,
      "clustering_factor": 500,
      "last_analyzed": "2025-05-01"
    }
  ],
  "summary": {
    "total_unused": 3,
    "total_index_mb": 500.5,
    "unused_size_mb": 25.3,
    "days": 90,
    "owner": "SCOTT"
  }
}
```

### GET /{instance_id}/drop-index-script
生成删除未使用索引脚本。

**Query 参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| owner | string | Schema |
| index_name | string | 索引名 |

---

## 索引管理（T-041）

### GET /{instance_id}/indexes/unusable
查询普通失效索引（dba_indexes.status = 'UNUSABLE'）。

**Query 参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| owner | string | 可选，Schema 过滤 |
| table_name | string | 可选，表名过滤 |

### GET /{instance_id}/indexes/partitions/unusable
查询分区/子分区失效索引（dba_ind_partitions + dba_ind_subpartitions）。

**Query 参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| owner | string | 可选，Schema 过滤 |
| table_name | string | 可选，表名过滤 |

### GET /{instance_id}/indexes/abnormal
查询 Domain / Function-based 异常索引。

### POST /{instance_id}/indexes/rebuild-plan
生成重建索引 SQL（plan_only，不自动执行）。

**请求体**
| 字段 | 类型 | 说明 |
|------|------|------|
| owner | string | 索引所属 Schema |
| index_name | string | 索引名 |
| partition_name | string | 可选，分区名 |
| subpartition_name | string | 可选，子分区名 |
| tablespace_name | string | 可选，重建目标表空间 |
| online | bool | 默认 true |
| parallel | int | 并行度，默认 1 |

### POST /{instance_id}/indexes/create-plan
生成创建索引 SQL（plan_only，含表/列/重复检查）。

**请求体**
| 字段 | 类型 | 说明 |
|------|------|------|
| owner | string | 表所属 Schema |
| table_name | string | 表名 |
| index_name | string | 索引名 |
| columns | string[] | 索引列列表 |
| index_type | string | NORMAL / UNIQUE / BITMAP |
| tablespace_name | string | 可选，索引表空间 |
| online | bool | 默认 true |
| parallel | int | 并行度，默认 1 |
| compress | bool | 是否压缩，默认 false |

### POST /{instance_id}/indexes/check-duplicate
检查重复索引。

### GET /{instance_id}/tables/{owner}/{table_name}/columns
查询表字段列表（供索引创建向导使用）。

---

## 统计信息管理（T-042）

### GET /{instance_id}/stats/tables
查询表统计信息。

**Query 参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| owner | string | 可选，Schema 过滤 |
| table_name | string | 可选，表名过滤 |
| stale_only | bool | 仅显示过期统计信息 |

### GET /{instance_id}/stats/table-detail
查询表统计信息详情。

**Query 参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| owner | string | Schema |
| table_name | string | 表名 |

### GET /{instance_id}/stats/columns
查询列统计信息。

### GET /{instance_id}/stats/indexes
查询索引统计信息。

### GET /{instance_id}/stats/history
查询统计信息历史（dba_tab_stats_history）。

### GET /{instance_id}/stats/operations
查询 DBMS_STATS 操作历史（dba_optstat_operations）。

### POST /{instance_id}/stats/gather-table-plan
生成表统计信息收集 SQL（plan_only）。

**请求体**
| 字段 | 类型 | 说明 |
|------|------|------|
| owner | string | Schema |
| table_name | string | 表名 |
| partition_name | string | 可选，分区名 |
| degree | int | 并行度，默认 4 |
| cascade | bool | 同时收集索引统计，默认 true |

### POST /{instance_id}/stats/gather-schema-plan
生成 Schema 级统计信息收集 SQL（plan_only）。

**请求体**
| 字段 | 类型 | 说明 |
|------|------|------|
| owner | string | Schema |
| options | string | GATHER STALE / GATHER / GATHER EMPTY |
| degree | int | 并行度，默认 4 |

### POST /{instance_id}/stats/lock-table-plan
生成锁定统计信息 SQL（plan_only）。

### POST /{instance_id}/stats/unlock-table-plan
生成解锁统计信息 SQL（plan_only）。

### POST /{instance_id}/stats/delete-table-plan
生成删除统计信息 SQL（plan_only，高危操作）。

---

## Redo Log

### GET /{instance_id}/redo-log
获取 Redo Log 信息。

### GET /{instance_id}/redo-log/switch-frequency
获取 Redo Log 切换频率。

**Query 参数**
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| days | int | 7 | 查询天数 |

### GET /{instance_id}/redo-log/add-group-script
生成添加 Redo Log 组脚本。

### GET /{instance_id}/redo-log/add-member-script
生成添加 Redo Log 成员脚本。

### GET /{instance_id}/redo-log/switch-script
生成手动切换 Redo Log 脚本。

### GET /{instance_id}/redo-log/drop-group-script
生成删除 Redo Log 组脚本。

---

## 锁会话

### GET /{instance_id}/locked-objects
获取当前被锁的对象。

### GET /{instance_id}/blocking-tree
获取阻塞会话树。

### GET /{instance_id}/kill-session-script
生成 KILL SESSION 脚本。

**Query 参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| sid | int | Session ID |
| serial | int | Serial# |

---

## 安全检查

### GET /{instance_id}/password-policy
获取密码策略配置。

### GET /{instance_id}/audit-config
获取审计配置。

### GET /{instance_id}/nls-parameters
获取 NLS 参数。

### GET /{instance_id}/dblinks
获取 Database Links。

### GET /{instance_id}/supplemental-log
获取补充日志配置。

### POST /{instance_id}/ransomware-check
执行勒索病毒安全检查。

---

## 备份恢复

### GET /{instance_id}/backup/exp-script
生成 exp 导出脚本。

### GET /{instance_id}/backup/imp-script
生成 imp 导入脚本。

### GET /{instance_id}/backup/expdp-script
生成 expdp 导出脚本。

### GET /{instance_id}/backup/impdp-script
生成 impdp 导入脚本。

### GET /{instance_id}/backup/rman-backup-script
生成 RMAN 备份脚本。

### GET /{instance_id}/backup/rman-restore-script
生成 RMAN 恢复脚本。

---

## 归档/FRA

### GET /{instance_id}/archive-config
获取归档配置。

### GET /{instance_id}/archive-enable-script
生成开启归档模式脚本。

### GET /{instance_id}/archive-disable-script
生成关闭归档模式脚本。

### GET /{instance_id}/archive-path-script
生成修改归档路径脚本。

### GET /{instance_id}/archive-space-detail
获取归档空间详情。

### GET /{instance_id}/archive-emergency-script
生成归档空间紧急清理脚本。

### POST /{instance_id}/archive/deploy-cleanup
通过 SSH 部署归档清理脚本。

### GET /{instance_id}/fra/status
获取 FRA 状态。

### GET /{instance_id}/fra
获取 FRA 使用详情。

---

## Oracle 用户管理

### GET /{instance_id}/users
获取数据库用户列表。

### GET /{instance_id}/roles
获取角色列表。

### GET /{instance_id}/users/{username}/privileges
获取用户权限详情。

### POST /{instance_id}/users/create-script
生成创建用户脚本。

### POST /{instance_id}/users/{username}/drop-script
生成删除用户脚本。

### POST /{instance_id}/users/{username}/lock-script
生成锁定用户脚本。

### POST /{instance_id}/users/{username}/password-script
生成修改用户密码脚本。

### POST /{instance_id}/grant-script
生成授权脚本。

### POST /{instance_id}/revoke-script
生成撤销权限脚本。

---

## ASM / Data Guard / RMAN

### GET /{instance_id}/asm
获取 ASM 磁盘组信息。

### GET /{instance_id}/dg-status
获取 Data Guard 状态。

### GET /{instance_id}/rman-status
获取 RMAN 备份状态。

---

## 巡检/慢 SQL/其他

### POST /{instance_id}/inspect
执行数据库健康巡检。

### GET /{instance_id}/top-sql
获取 Top SQL。

### GET /{instance_id}/locks
获取锁信息。

### GET /{instance_id}/slow-sql-realtime
获取实时慢 SQL。

**Query 参数**
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| category | string | "top_avg" | 分类：`active`、`top_elapsed`、`top_avg`、`top_buffer`、`top_disk`、`waiting` |

### GET /{instance_id}/sql-detail
获取 SQL 详细执行计划。

### GET /{instance_id}/capacity/summary
获取容量汇总。

### GET /{instance_id}/capacity/growth
获取容量增长趋势。

---

## SSH 相关（Oracle 模块内）

### POST /{instance_id}/ssh-test
测试 SSH 连接。

### POST /{instance_id}/ssh-diagnose
诊断 SSH 连接问题。

### GET /{instance_id}/ssh-system-info
通过 SSH 获取系统信息。

### POST /{instance_id}/ssh-execute
通过 SSH 执行远程命令。

**请求体**
| 参数 | 类型 | 说明 |
|------|------|------|
| command | string | Shell 命令 |
| timeout | int | 超时秒数 |
| confirm | bool | 必须为 true |
