# 实例管理模块 (Instances)

## 概述
管理数据库连接实例的增删改查和连接测试。每个实例包含数据库类型、主机、端口、用户名、密码等连接信息。

## API 列表

### GET /api/v1/instances
分页获取数据库实例列表。

**Query 参数**
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| page | int | 否 | 1 | 页码（≥1） |
| page_size | int | 否 | 10 | 每页条数（1-100） |
| db_type | string | 否 | 系统配置 | 过滤数据库类型：`oracle` 或 `mysql` |
| keyword | string | 否 | - | 按实例名或主机名搜索 |

**响应 (200)**
```json
{
  "items": [
    {
      "id": 1,
      "instance_name": "ORCL_PROD",
      "db_type": "oracle",
      "host": "192.168.1.100",
      "port": 1521,
      "username": "system",
      "service_name": "orcl",
      "sid": null,
      "oracle_connect_type": "service_name",
      "ssh_host": "192.168.1.100",
      "ssh_port": 22,
      "ssh_username": "oracle",
      "status": "active",
      "create_time": "2025-01-01 00:00:00",
      "update_time": "2025-01-01 00:00:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 10
}
```

---

### GET /api/v1/instances/{instance_id}
获取单个实例详情。

**路径参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| instance_id | int | 实例 ID |

**成功响应 (200)** 返回实例对象  
**失败响应 (404)** `{"detail": "Instance not found"}`

---

### POST /api/v1/instances
创建数据库实例。

**请求体 (JSON)**
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| instance_name | string | 是 | 实例名称 |
| db_type | string | 是 | 数据库类型：`oracle` 或 `mysql` |
| host | string | 是 | 主机地址 |
| port | int | 是 | 端口号 |
| username | string | 是 | 数据库用户名 |
| password | string | 是 | 数据库密码（加密存储） |
| service_name | string | 否 | Oracle Service Name |
| sid | string | 否 | Oracle SID |
| oracle_connect_type | string | 否 | 连接方式：`service_name` 或 `sid` |
| ssh_host | string | 否 | SSH 主机地址 |
| ssh_port | int | 否 | SSH 端口 |
| ssh_username | string | 否 | SSH 用户名 |
| ssh_password | string | 否 | SSH 密码 |

**成功响应 (201)** 返回创建的实例对象

---

### PUT /api/v1/instances/{instance_id}
更新数据库实例。

**路径参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| instance_id | int | 实例 ID |

**请求体 (JSON)** 同创建，所有字段可选。

**成功响应 (200)** 返回更新后的实例对象

---

### DELETE /api/v1/instances/{instance_id}
删除数据库实例。

**路径参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| instance_id | int | 实例 ID |

**成功响应 (204)** 无内容

---

### POST /api/v1/instances/{instance_id}/test
测试数据库连接。

**路径参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| instance_id | int | 实例 ID |

**响应 (200)**
```json
{
  "ok": true,
  "message": "连接成功",
  "detail": { "version": "Oracle Database 12c" }
}
```
```json
{
  "ok": false,
  "message": "连接失败",
  "detail": { "error": "ORA-12154: TNS:could not resolve..." }
}
```
