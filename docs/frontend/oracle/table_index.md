# 表和索引页面 (TableIndex)

## 概述
分析 Oracle 数据库的表碎片、索引碎片和未使用索引，生成整理/重建/删除脚本。

## 功能点

### 实例和 Schema 选择
- 下拉选择 Oracle 实例
- 自动加载 Schema 列表（从 dba_tables 提取）
- 支持最小大小过滤

### Tab 1：表碎片
- 统计卡片：碎片表数量、总分配、总浪费、浪费率
- 警告提示（浪费 > 1000MB）
- 明细表格：Owner、表名、分配/已用/浪费 MB、浪费率进度条、行数、最后分析
- 浪费率排序
- 操作：点击"整理"生成 SHRINK SPACE 脚本

**涉及 API：**
| API | 方法 | 说明 |
|-----|------|------|
| /api/v1/oracle/{id}/table-fragmentation | GET | 表碎片分析 |
| /api/v1/oracle/{id}/table-shrink-script | GET | 生成整理脚本 |

### Tab 2：索引碎片
- 统计卡片：索引数量、分析表数、实际大小、浪费大小
- 明细表格：表名、索引名、应有大小、实际大小、浪费、膨胀率
- 操作：点击"重建"生成 REBUILD ONLINE 脚本

**涉及 API：**
| API | 方法 | 说明 |
|-----|------|------|
| /api/v1/oracle/{id}/index-fragmentation | GET | 索引碎片分析 |
| /api/v1/oracle/{id}/index-rebuild-script | GET | 生成重建脚本 |

### Tab 3：未使用索引
- 统计卡片：未使用索引数、占用空间、Schema 总索引大小、分析天数
- 警告/成功提示
- 明细表格：表名、索引名、类型、唯一性、状态、可见性、行数、叶块数、不同键数、聚合因子、最后分析
- 按叶块数降序排列
- 操作：点击"生成脚本"生成安全删除脚本（先 INVISIBLE 观察，再 DROP）

**涉及 API：**
| API | 方法 | 说明 |
|-----|------|------|
| /api/v1/oracle/{id}/unused-indexes | GET | 未使用索引分析 |
| /api/v1/oracle/{id}/drop-index-script | GET | 生成删除脚本 |

### 脚本弹窗
- 展示生成的 SQL 脚本
- 注意事项/警告列表
- 复制到剪贴板按钮

### Tab 4：失效索引（T-041）
- 查询 dba_indexes.status = 'UNUSABLE' 的普通失效索引
- 查询 dba_ind_partitions + dba_ind_subpartitions 的分区/子分区失效索引
- 查询 domidx_status / funcidx_status 异常的 Domain / Function-based 索引
- 支持 owner、table_name 过滤
- 操作：点击"生成重建 SQL"按钮生成 ALTER INDEX ... REBUILD ONLINE 脚本（plan_only）

**涉及 API：**
| API | 方法 | 说明 |
|-----|------|------|
| /api/v1/oracle/{id}/indexes/unusable | GET | 普通失效索引 |
| /api/v1/oracle/{id}/indexes/partitions/unusable | GET | 分区/子分区失效索引 |
| /api/v1/oracle/{id}/indexes/abnormal | GET | 异常 Domain/Function-based 索引 |
| /api/v1/oracle/{id}/indexes/rebuild-plan | POST | 生成重建索引 SQL |

### Tab 5：索引创建向导（T-041）
- 输入 owner、table_name 后加载字段列表
- 支持选择多个字段生成组合索引
- 支持 index_type：NORMAL / UNIQUE / BITMAP
- 支持 tablespace_name、online、parallel、compress
- 创建前自动检查：表存在性、字段存在性、重复索引、统计信息、表空间空间
- 生成 CREATE INDEX SQL（plan_only）

**涉及 API：**
| API | 方法 | 说明 |
|-----|------|------|
| /api/v1/oracle/{id}/tables/{owner}/{table}/columns | GET | 表字段列表 |
| /api/v1/oracle/{id}/indexes/check-duplicate | POST | 重复索引检查 |
| /api/v1/oracle/{id}/indexes/create-plan | POST | 生成创建索引 SQL |

### Tab 6：统计信息管理（T-042，前端待接入）
- 后端 11 个 API 已就绪，前端 Tab 暂未添加
- 功能：查看表/列/索引统计信息、过期统计信息过滤
- 生成 DBMS_STATS.GATHER_TABLE_STATS / GATHER_SCHEMA_STATS SQL
- 生成锁定/解锁/删除统计信息 SQL
- 所有操作 plan_only，不自动执行

**涉及 API：**
| API | 方法 | 说明 |
|-----|------|------|
| /api/v1/oracle/{id}/stats/tables | GET | 表统计信息 |
| /api/v1/oracle/{id}/stats/columns | GET | 列统计信息 |
| /api/v1/oracle/{id}/stats/indexes | GET | 索引统计信息 |
| /api/v1/oracle/{id}/stats/history | GET | 统计信息历史 |
| /api/v1/oracle/{id}/stats/operations | GET | DBMS_STATS 操作历史 |
| /api/v1/oracle/{id}/stats/gather-table-plan | POST | 表统计收集 SQL |
| /api/v1/oracle/{id}/stats/gather-schema-plan | POST | Schema 统计收集 SQL |
| /api/v1/oracle/{id}/stats/lock-table-plan | POST | 锁定统计 SQL |
| /api/v1/oracle/{id}/stats/unlock-table-plan | POST | 解锁统计 SQL |
| /api/v1/oracle/{id}/stats/delete-table-plan | POST | 删除统计 SQL |
