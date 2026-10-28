# 总览页面 (Dashboard)

## 概述
系统首页，展示数据库实例运行状态总览、容量告警摘要。

## 功能点

### 实例状态概览
- 加载所有已配置的数据库实例
- 显示每个实例的连接状态、数据库类型
- 调用 `GET /api/v1/instances`

### 容量告警摘要
- 显示最近的容量告警
- 调用 `GET /api/v1/capacity/alerts?limit=20`

### 模块菜单
- 根据模块配置和用户权限动态生成菜单
- 调用 `GET /api/v1/modules` 获取已启用的模块列表
- 非管理员只看到被授权的模块

## 涉及 API
| API | 方法 | 说明 |
|-----|------|------|
| /api/v1/instances | GET | 获取实例列表 |
| /api/v1/modules | GET | 获取模块配置 |
| /api/v1/system/config | GET | 获取系统配置 |
| /api/v1/capacity/alerts | GET | 获取容量告警 |
