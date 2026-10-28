# 用户管理模块 (Users)

## 概述
管理系统用户账号，包括 CRUD、密码管理、账号锁定/解锁、模块权限分配、密码策略配置。仅管理员可访问。

## 数据库表

### sys_user
| 字段 | 类型 | 说明 |
|------|------|------|
| id | int | 主键 |
| username | string | 登录用户名（唯一） |
| password_hash | string | SHA256 密码哈希 |
| display_name | string | 显示名称 |
| role | string | 角色：`admin` 或 `user` |
| status | string | 状态：`active`、`disabled`、`locked` |
| password_changed_at | datetime | 密码最后修改时间 |
| locked_until | datetime | 锁定截止时间 |
| login_fail_count | int | 连续登录失败次数 |
| last_login_at | datetime | 最后登录时间 |
| create_time | datetime | 创建时间 |

### sys_user_module
| 字段 | 类型 | 说明 |
|------|------|------|
| id | int | 主键 |
| user_id | int | 用户 ID（外键） |
| module_key | string | 模块标识 |

## API 列表

### GET /api/v1/users
获取所有用户列表。

**响应 (200)**
```json
{
  "items": [
    {
      "id": 1,
      "username": "admin",
      "display_name": "管理员",
      "role": "admin",
      "status": "active",
      "password_changed_at": "2025-01-01 00:00:00",
      "locked_until": null,
      "login_fail_count": 0,
      "last_login_at": "2025-05-12 10:00:00",
      "create_time": "2025-01-01 00:00:00"
    }
  ]
}
```

---

### POST /api/v1/users
创建新用户。

**请求体 (JSON)**
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| username | string | 是 | - | 登录用户名 |
| password | string | 是 | - | 登录密码（受密码策略约束） |
| display_name | string | 否 | "" | 显示名称 |
| role | string | 否 | "user" | 角色：`admin` 或 `user` |

**密码复杂度要求**（可配置）：
- 至少 8 位
- 包含至少一个大写字母
- 包含至少一个小写字母
- 包含至少一个数字

---

### PUT /api/v1/users/{user_id}
更新用户信息。

**路径参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| user_id | int | 用户 ID |

**请求体 (JSON)**
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| display_name | string | 否 | 显示名称 |
| role | string | 否 | 角色 |
| status | string | 否 | 状态：`active`、`disabled`、`locked` |
| locked_until | string | 否 | 锁定截止时间（ISO 格式，传 null 清除） |

---

### PUT /api/v1/users/{user_id}/reset-password
重置用户密码。

**请求体 (JSON)**
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| new_password | string | 是 | 新密码（受密码策略约束） |

重置后自动：清除登录失败计数、解除锁定、激活账号。

---

### DELETE /api/v1/users/{user_id}
删除用户。`admin` 账号不可删除。同时删除该用户的所有模块权限。

---

### POST /api/v1/users/{user_id}/unlock
解锁用户。清除锁定状态、登录失败计数，恢复为 active。

---

### GET /api/v1/users/{user_id}/permissions
获取用户的模块权限列表。

**响应 (200)**
```json
{
  "user_id": 2,
  "role": "user",
  "module_keys": ["dashboard", "inspection", "slowSql"]
}
```

---

### PUT /api/v1/users/{user_id}/permissions
设置用户的模块权限（覆盖式更新）。

**请求体 (JSON)**
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| module_keys | string[] | 是 | 授权的模块标识列表 |

**请求示例**
```json
{"module_keys": ["dashboard", "inspection", "slowSql", "capacity"]}
```

---

### GET /api/v1/users/password-policy
获取密码策略配置。

**响应 (200)**
```json
{
  "password_expire_days": 90,
  "max_login_fail": 5,
  "lock_duration_minutes": 30,
  "enforce_complexity": true
}
```

---

### PUT /api/v1/users/password-policy
更新密码策略配置。

**请求体 (JSON)**
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| password_expire_days | int | 是 | 90 | 密码过期天数（0=不过期） |
| max_login_fail | int | 是 | 5 | 最大登录失败次数 |
| lock_duration_minutes | int | 是 | 30 | 自动锁定时长（分钟） |
| enforce_complexity | bool | 是 | true | 是否强制密码复杂度 |
