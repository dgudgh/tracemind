# 认证与会话安全

## 能力范围

平台支持本地账号、LDAP/AD、OIDC、数据库会话、细粒度动作角色、资产数据范围和 TOTP MFA。访问令牌使用持久化 RSA 私钥签发 RS256 JWT，服务端同时保存可撤销会话；进程重启后会话可以恢复，登出、密码变更、角色或组织变更会撤销相关会话。

MES 一次性票据和统一退出属于后续集成范围，本模块不依赖 MES 即可独立运行。

兼容的 `mes-login` 共享令牌只代表一个未绑定平台账号的外部身份，不授予业务或管理员权限；仅可查询当前身份和退出。业务调用必须使用已授权的平台本地或企业账号。

## 登录流程

### `POST /api/v1/auth/login`

请求：

```json
{
  "username": "admin",
  "password": "********"
}
```

未启用 MFA 时返回：

```json
{
  "token": "signed-jwt",
  "username": "admin",
  "expires_in": 28800
}
```

启用 MFA 时只返回短时挑战，不签发正式会话：

```json
{
  "mfa_required": true,
  "challenge_token": "one-time-challenge",
  "expires_in": 300,
  "username": "admin"
}
```

随后调用 `POST /api/v1/auth/mfa/verify`，提交 `challenge_token` 和动态验证码或一次性恢复码。验证成功后才返回正式 JWT。挑战绑定密码验证当时的密码版本和加盐凭据摘要，避免 MySQL 秒精度时间戳碰撞；改密前取得的挑战不能在改密后继续换取会话，必须重新输入新密码。摘要仅保存在服务端挑战中，不返回给客户端。常规登录及 MFA 会话签发持有用户锁直到会话提交，避免并发改密时签发错误版本的令牌。

## 密码修改与重置

管理页使用 `PUT /api/v1/users/{user_id}/reset-password`。请求必须同时包含 `current_password` 和 `new_password`，旧版只有新密码的请求返回 422。

- 修改自己的密码：验证当前登录账号的原密码。
- 重置他人的密码：要求管理员或显式授予 `platform.security` 权限，并验证**操作者**的当前密码，不要求目标用户提供密码。
- 操作者由服务端登录会话确定，重新读取账号状态与密码版本。此入口仅支持本地账号登录，不绕过企业身份认证。
- 当前密码错误计入操作者的密码验证失败次数，达到策略阈值后锁定操作者并撤销其会话。账号不能自行调用“解锁”清除失败次数；未锁定账号也不能通过该接口清零。
- 新密码仍受现有复杂度策略约束，且不能与目标账号原密码相同。密码、审计和目标账号的会话撤销在同一事务提交，失败不报告成功。
- 修改自己密码后返回 `reauthenticate: true`，前端清除登录状态并返回登录页；重置他人密码不会退出操作者。停用账号不会因重置密码被自动启用，MFA 配置保持不变。
- 审计记录操作者、目标账号、成功或失败及原因，不记录新旧密码。此接口仍保留管理页的既有权限边界，不新增普通用户自助改密入口。

前端弹窗区分“当前密码”和“操作管理员当前密码”，关闭后清空密码字段，提交期间禁止重复提交。忘记密码需要其他授权管理员处理或经授权的运维恢复，不能通过省略当前密码绕过本接口。

## MFA 管理

| API | 方法 | 说明 |
|---|---|---|
| `/api/v1/auth/mfa/status` | GET | 查询当前账号 MFA 状态 |
| `/api/v1/auth/mfa/enroll` | POST | 生成绑定密钥、URI 和一次性恢复码 |
| `/api/v1/auth/mfa/confirm` | POST | 首次验证码确认并启用 |
| `/api/v1/auth/mfa/disable` | POST | 使用验证码或恢复码停用 |
| `/api/v1/auth/mfa/verify` | POST | 登录阶段完成二次验证 |

绑定密钥和恢复码只在注册响应中显示一次，前端不会长期保存明文。

## 会话管理

| API | 方法 | 说明 |
|---|---|---|
| `/api/v1/auth/current-user` | GET | 当前登录身份与 MFA 状态 |
| `/api/v1/auth/sessions` | GET | 当前账号最近会话 |
| `/api/v1/auth/sessions/{id}` | DELETE | 撤销指定会话 |
| `/api/v1/auth/logout` | POST | 注销当前令牌 |
| `/api/v1/auth/jwks` | GET | 发布当前 RS256 公钥 |

服务端在每次请求时校验 JWT 签名、过期时间、数据库中的会话撤销状态、空闲超时、账号状态和密码版本。本地缓存不能代替持久化会话校验；数据库会话保存失败时不签发正式令牌。

## 授权模型

动作角色与资产范围同时生效：

1. 动作角色决定用户能否查看、维护、执行、审批或导出。
2. 资产范围限制租户、组织、区域、机房、可用区、环境、主机、集群和实例。
3. 范围访问级别分为 `read`、`operate` 和 `admin`。
4. 校验同时检查 URL、查询参数、请求体以及任务、备份、归档等对象所关联的实例，任何一个目标越界都会拒绝请求。受限账号访问尚未实现范围过滤的全局列表或无法归属的对象时返回 403。
5. 未分配动作角色、角色停用、接口未登记权限时默认拒绝；新旧非管理员账号都需要管理员显式分配角色。账号未配置资产范围时仍表示全范围，配置范围后同时限制访问级别。账号自身的会话和 MFA 管理不依赖业务角色。
6. 数据治理权限拆分为 `data_governance.read/manage/execute/approve/legal_hold`。数据库运维角色可创建和执行归档，但默认不能审批或解除归档保留；审批角色默认不能执行归档。备份审批使用 `automation.approve`。

升级后应由本地管理员检查角色分配。没有角色的旧账号将收到 403，不能通过保留旧令牌恢复原来的默认放行行为。新增业务路由必须登记动作权限，并通过 `test_authorization_boundary.py` 的全路由覆盖检查。

内置角色只在首次创建时填充默认权限，查询角色列表不会恢复已撤销的授权。已有角色需要管理员审核并补充本次新增权限。修改实例连接地址、账号、密码或证书等信息还需要 `instance.credential`；查看诊断和导出诊断证据分别需要 `diagnosis.read` 与 `diagnosis.export`。

### 恢复 Agent 身份

`/api/v1/recovery/agent/*` 仅供恢复 Agent 使用，不向普通查看者、执行者或未绑定的管理员开放。客户端使用平台本地服务账号的 Bearer 会话，账号必须显式获得 `recovery.agent`，并通过服务端环境变量 `RECOVERY_AGENT_USER_BINDINGS` 绑定唯一 Agent ID。例如 `{"rescue-01": 7}` 表示将 Agent `rescue-01` 绑定到用户 ID 7，不是密码或令牌。不能用通配符权限代替此显式授权。

注册和心跳中的 Agent ID 必须匹配绑定；领取任务、上传日志、上报进度和完成任务均校验任务归属。绑定未配置时拒绝调用，不回退到匿名模式。

## 关键配置

```dotenv
AUTH_TOKEN_EXPIRE_SECONDS=28800
AUTH_SESSION_IDLE_SECONDS=7200
AUTH_MFA_CHALLENGE_SECONDS=300
MFA_ISSUER=TraceMind AI
MFA_REQUIRED_FOR_ADMIN=false
JWT_PRIVATE_KEY_PATH=
SECURITY_AUDIT_RETENTION_DAYS=365
```

`JWT_PRIVATE_KEY_PATH` 为空时使用平台数据目录中的 `.auth_signing_key.pem`，首次创建权限为 `0600`。生产环境应把私钥目录纳入受控备份，不得提交到 Git。

## 审计

登录、MFA、角色、范围、租户、组织和会话操作写入安全审计。审计详情会脱敏密码、Token、Secret、Cookie 和私钥字段，并使用 SHA-256 前后哈希形成可校验链；`GET /api/v1/security/audit/verify` 可验证链完整性。
