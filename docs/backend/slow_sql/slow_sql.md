# 慢 SQL 分析模块 (Slow SQL)

## 概述
记录和分析慢查询 SQL，支持按实例、数据库类型、风险等级筛选，以及 AI 智能分析。

## API 列表

### GET /api/v1/slow-sql
分页获取慢 SQL 列表。

**Query 参数**
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| page | int | 否 | 1 | 页码（≥1） |
| page_size | int | 否 | 20 | 每页条数（1-100） |
| instance_id | int | 否 | - | 按实例 ID 过滤 |
| db_type | string | 否 | - | 按数据库类型过滤：`oracle` 或 `mysql` |
| risk_level | string | 否 | - | 按风险等级过滤 |

**响应 (200)**
```json
{
  "items": [
    {
      "id": 1,
      "instance_id": 1,
      "db_type": "oracle",
      "sql_text": "SELECT * FROM ...",
      "execution_time_ms": 5000,
      "risk_level": "high",
      "analysis": "...",
      "create_time": "2025-05-12 10:00:00"
    }
  ],
  "total": 100,
  "page": 1,
  "page_size": 20
}
```

---

### POST /api/v1/slow-sql/{slow_sql_id}/analyze
对慢 SQL 执行 AI 智能分析。

**路径参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| slow_sql_id | int | 慢 SQL 记录 ID |

**响应 (200)** 返回包含分析结果的慢 SQL 对象
