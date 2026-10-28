# 系统配置模块 (System)

## 概述
提供系统全局配置信息，包括项目名称、版本号、数据库引擎版本等。

## API 列表

### GET /api/v1/system/config
获取系统配置信息（无需认证）。

**响应 (200)**
```json
{
  "project_name": "TraceMind AI",
  "version": "1.0.0",
  "db_engine_edition": "oracle",
  "edition_label": "Oracle 版",
  "api_prefix": "/api/v1"
}
```

**字段说明**
| 字段 | 类型 | 说明 |
|------|------|------|
| project_name | string | 项目名称 |
| version | string | 系统版本号 |
| db_engine_edition | string | 数据库引擎版本：`oracle` 或 `mysql` |
| edition_label | string | 版本显示标签 |
| api_prefix | string | API 前缀路径 |
