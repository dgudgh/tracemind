# 模块配置模块 (Module Config)

## 概述
管理系统的功能模块开关。每个模块有一个全局 enabled 状态，控制是否在菜单中显示。还支持按用户过滤模块权限。

## 数据库表：module_config
| 字段 | 类型 | 说明 |
|------|------|------|
| id | int | 主键 |
| module_key | string | 模块标识（唯一） |
| module_name | string | 模块显示名称 |
| category | string | 分类：`common`（通用）或 `oracle`（Oracle 专属） |
| enabled | int | 是否启用：1=启用，0=禁用 |
| sort_order | int | 排序序号 |
| create_time | datetime | 创建时间 |
| update_time | datetime | 更新时间 |

## API 列表

### GET /api/v1/modules
获取模块列表。自动补全缺失的默认模块。

**权限逻辑：**
- admin 用户：返回所有模块
- 非 admin 用户：过滤掉 `userManagement` 和 `moduleConfig`
- 数据库用户（非 admin）：只返回该用户被授权的模块

**请求头**
| 参数 | 说明 |
|------|------|
| Authorization | Bearer Token（用于判断用户角色） |

**响应 (200)**
```json
{
  "items": [
    {
      "id": 1,
      "module_key": "dashboard",
      "module_name": "总览",
      "category": "common",
      "enabled": 1,
      "sort_order": 1
    }
  ]
}
```

---

### PUT /api/v1/modules/{module_key}
更新单个模块的启用状态。

**路径参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| module_key | string | 模块标识 |

**请求体 (JSON)**
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| enabled | int | 否 | 1=启用，0=禁用 |

**响应 (200)**
```json
{"ok": true, "module_key": "dashboard", "enabled": 1}
```

---

### PUT /api/v1/modules
批量更新模块启用状态。

**请求体 (JSON)**
```json
{
  "modules": [
    {"module_key": "dashboard", "enabled": 1},
    {"module_key": "ai", "enabled": 0}
  ]
}
```

**响应 (200)**
```json
{"ok": true, "updated": 2}
```

---

### POST /api/v1/modules/reset
重置所有模块配置为默认值。删除现有配置后重新初始化。

**响应 (200)**
```json
{"ok": true, "message": "已重置为默认配置"}
```
