# AI 诊断模块 (AI)

## 概述
提供 AI 智能诊断功能，根据输入的症状描述和数据库指标，给出诊断建议。

## API 列表

### POST /api/v1/ai/diagnose
执行 AI 诊断。

**请求体 (JSON)**
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| symptoms | string | 是 | 症状描述 |
| instance_id | int | 否 | 关联的实例 ID |
| metrics | object | 否 | 相关数据库指标 |

**响应 (200)**
```json
{
  "diagnosis": "诊断结果描述",
  "suggestions": ["建议1", "建议2"],
  "severity": "high",
  "category": "performance"
}
```
