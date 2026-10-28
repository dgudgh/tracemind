# 用户管理页面 (UserManagement)

## 概述
管理系统用户账号、密码策略和模块权限分配。仅管理员可见。包含两个 Tab：用户管理和模块权限。

## Tab 1：用户管理

### 用户列表
- 表格展示所有用户（用户名、显示名、角色、状态、登录失败次数、密码修改时间、锁定至、上次登录时间）
- 调用 `GET /api/v1/users`

### 新增用户
- 弹窗表单：用户名、密码、显示名、角色
- 密码复杂度校验
- 调用 `POST /api/v1/users`

### 停用/启用用户
- 切换用户状态 active ↔ disabled
- 调用 `PUT /api/v1/users/{id}`

### 解锁用户
- 清除锁定状态
- 调用 `POST /api/v1/users/{id}/unlock`

### 重置密码
- 弹窗输入新密码（含确认密码校验）
- 调用 `PUT /api/v1/users/{id}/reset-password`

### 删除用户
- 确认弹窗后删除（admin 不可删除）
- 调用 `DELETE /api/v1/users/{id}`

### 密码策略配置
- 弹窗表单：密码过期天数、最大登录失败次数、锁定时长、强制复杂度开关
- 调用 `GET/PUT /api/v1/users/password-policy`

---

## Tab 2：模块权限

### 权限矩阵
- 每个非 admin 用户一个卡片
- 表格展示所有可分配模块（排除 userManagement 和 moduleConfig）
- 每行：模块名 + 分类标签 + 功能描述
- Checkbox 实时保存到后端
- 每个用户有"全选"和"清空"快捷操作
- 调用 `GET /api/v1/users/{id}/permissions` 加载
- 调用 `PUT /api/v1/users/{id}/permissions` 保存

### 模块描述
| 模块 | 描述 |
|------|------|
| dashboard | 数据库实例运行状态总览 |
| instances | 管理数据库连接实例 |
| inspection | 一键执行数据库健康巡检 |
| slowSql | 捕获和分析慢查询 SQL |
| capacity | 监控表空间容量，预测增长趋势 |
| schedule | 配置定时巡检策略 |
| alertCenter | 集中查看容量告警 |
| reports | 查看和导出巡检报告 |
| ai | AI 智能诊断数据库问题 |
| basicInfo | 查看 Oracle 数据库基础信息 |
| dbControl | 启动和停止数据库实例 |
| parameters | 查看和修改初始化参数 |
| redoLog | 监控 Redo Log 切换频率 |
| lockSession | 查看和清理阻塞会话 |
| userMgmt | 管理 Oracle 数据库用户 |
| backupRestore | 管理 RMAN 备份恢复 |
| archiveFRA | 监控归档日志和 FRA 空间 |
| tablespaceMonitor | 实时监控表空间使用率 |
| tableIndex | 表碎片、索引碎片、未使用索引分析 |
| operations | 日常运维操作 |

## 涉及 API
| API | 方法 | 说明 |
|-----|------|------|
| /api/v1/users | GET | 获取用户列表 |
| /api/v1/users | POST | 创建用户 |
| /api/v1/users/{id} | PUT | 更新用户 |
| /api/v1/users/{id} | DELETE | 删除用户 |
| /api/v1/users/{id}/reset-password | PUT | 重置密码 |
| /api/v1/users/{id}/unlock | POST | 解锁用户 |
| /api/v1/users/{id}/permissions | GET | 获取用户权限 |
| /api/v1/users/{id}/permissions | PUT | 设置用户权限 |
| /api/v1/users/password-policy | GET | 获取密码策略 |
| /api/v1/users/password-policy | PUT | 更新密码策略 |
| /api/v1/modules | GET | 获取模块列表 |
