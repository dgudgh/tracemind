# 登录页面 (Login)

## 概述
系统登录入口，支持两种认证方式：用户名密码登录和 MES Token 认证。

## 功能点

### 用户名密码登录
- 输入用户名和密码
- 调用 `POST /api/v1/auth/login` 接口
- 登录成功后 Token 存储到 `localStorage("mesguard_token")`
- 自动跳转到总览页面

### MES Token 认证
- URL 参数携带 `?token=xxx` 自动触发 MES 认证
- 调用 `POST /api/v1/auth/mes-login` 接口
- 适用于 MES 系统集成免登录场景

### Token 管理
- 存储位置：`localStorage.mesguard_token`
- 所有 API 请求通过 `Authorization: Bearer {token}` 携带
- 401 响应自动跳转到登录页

## 涉及 API
| API | 方法 | 说明 |
|-----|------|------|
| /api/v1/auth/login | POST | 用户名密码登录 |
| /api/v1/auth/mes-login | POST | MES Token 登录 |
| /api/v1/auth/info | GET | 获取认证配置 |
