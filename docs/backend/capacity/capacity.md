# 容量预测模块 (Capacity)

## 概述
监控数据库表空间/磁盘组容量变化趋势，支持容量策略配置、自动采集、告警管理和趋势分析。

## API 列表

### GET /api/v1/capacity/scheduler/status
获取容量定时采集调度器状态。

**响应 (200)**
```json
{"running": true, "interval_seconds": 3600, "last_run": "2025-05-12 14:00:00"}
```

---

### GET /api/v1/capacity/policies
获取所有实例的容量策略状态。

---

### GET /api/v1/capacity/policies/{instance_id}
获取指定实例的容量策略。

**路径参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| instance_id | int | 实例 ID |

---

### PUT /api/v1/capacity/policies/{instance_id}
更新容量策略。

**请求体 (JSON)**
| 参数 | 类型 | 说明 |
|------|------|------|
| warning_threshold_pct | float | 告警阈值（%） |
| critical_threshold_pct | float | 严重告警阈值（%） |
| auto_collect_enabled | bool | 是否启用自动采集 |
| collect_interval_minutes | int | 采集间隔（分钟） |

---

### DELETE /api/v1/capacity/policies/{instance_id}
删除容量策略。

---

### POST /api/v1/capacity/{instance_id}/collect
手动触发一次容量数据采集。

---

### GET /api/v1/capacity/{instance_id}/summary
获取指定实例的容量汇总信息。

---

### GET /api/v1/capacity/{instance_id}/analysis
获取容量趋势分析。

**Query 参数**
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| days | int | 30 | 分析天数（1-365） |

---

### GET /api/v1/capacity/{instance_id}/snapshots
获取容量快照列表。

**Query 参数**
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| days | int | 30 | 查询天数（1-365） |

---

### GET /api/v1/capacity/alerts
获取容量告警列表。

**Query 参数**
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| instance_id | int | - | 按实例过滤 |
| limit | int | 50 | 返回条数（1-200） |
| status | string | "open" | 告警状态：`open`、`closed`、`all` |

---

### PUT /api/v1/capacity/alerts/{alert_id}/close
关闭指定告警。

---

### PUT /api/v1/capacity/alerts/close-all
批量关闭告警。

**Query 参数**
| 参数 | 类型 | 说明 |
|------|------|------|
| instance_id | int | 按实例关闭（不传则关闭所有） |

---

### GET /api/v1/capacity/alerts/stats
获取告警统计信息。

---

### GET /api/v1/capacity/trend
获取容量趋势数据。

**Query 参数**
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| instance_id | int | - | 实例 ID（必传） |
| days | int | 30 | 天数（1-365） |
