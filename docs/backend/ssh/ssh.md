# SSH 远程运维模块 (SSH)

## 概述
通过 SSH 连接数据库服务器，执行远程命令、部署脚本、获取系统信息。所有操作需要实例配置 SSH 连接信息。

## API 列表

### POST /api/v1/ssh/{instance_id}/test
测试 SSH 连接。

**路径参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| instance_id | int | 数据库实例 ID |

**响应 (200)**
```json
{"ok": true, "message": "SSH 连接成功", "system_info": {"os": "Linux", "kernel": "..."}}
```

---

### POST /api/v1/ssh/{instance_id}/diagnose
诊断 SSH 连接问题。

**响应 (200)**
```json
{"ok": true, "checks": [{"name": "网络连通", "status": "pass"}, {"name": "SSH端口", "status": "pass"}, {"name": "认证", "status": "fail", "detail": "密码错误"}]}
```

---

### GET /api/v1/ssh/{instance_id}/system-info
通过 SSH 获取服务器系统信息。

**响应 (200)**
```json
{
  "hostname": "db-server-01",
  "os": "Oracle Linux 7.9",
  "kernel": "5.4.17",
  "cpu_count": 16,
  "memory_total_gb": 64,
  "memory_used_gb": 45,
  "disk_usage": [{"mount": "/", "total_gb": 500, "used_gb": 300, "usage_pct": 60}]
}
```

---

### POST /api/v1/ssh/{instance_id}/execute
执行远程命令。

**请求体 (JSON)**
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| command | string | 是 | Shell 命令 |
| timeout | int | 否 | 超时秒数 |
| confirm | bool | 是 | 必须为 true（安全确认） |

**响应 (200)**
```json
{
  "ok": true,
  "stdout": "命令输出...",
  "stderr": "",
  "exit_code": 0
}
```

---

### POST /api/v1/ssh/{instance_id}/deploy-script
部署脚本到远程服务器并可选执行。

**请求体 (JSON)**
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| script_content | string | 是 | 脚本内容 |
| remote_path | string | 是 | 远程保存路径 |
| run | bool | 否 | 是否立即执行 |
| timeout | int | 否 | 执行超时秒数 |
