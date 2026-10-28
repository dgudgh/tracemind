# 安全策略

## 报告漏洞

**请不要通过公开 issue 报告安全问题。**

请使用 GitHub 的 [私密漏洞报告](https://docs.github.com/zh/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
（仓库 **Security** 标签页 → *Report a vulnerability*）。

报告中请尽量包含：

- 受影响的版本或提交哈希
- 复现步骤（能稳定复现的最小步骤）
- 影响范围：能否越权、能否读到其他实例的数据、是否需要已登录账号
- 若已知修复思路，一并说明

我们会在 5 个工作日内确认收到，并在修复发布后致谢（除非你希望匿名）。

## 支持范围

只对**最新发布版本**和 `main` 分支的最新提交提供安全修复。

## 部署侧提醒

本平台会连接生产数据库，请务必注意：

1. **使用最小权限账号接入被管实例**。采集账号只需要读权限；写操作请单独建账号并走审批。
2. **不要用超级用户运行服务进程**。
3. **元数据库与凭据通过环境变量注入**，不要提交到仓库。仓库的 `.gitignore` 已排除 `.env`、
   `*.db`、`license.json`，但仍请在提交前自查。
4. **反向代理必须启用 HTTPS**。平台默认不终止 TLS，会话令牌在明文 HTTP 下会被截获。
5. 首次部署后立即修改引导创建的管理员密码，并确认 `/api/v1/setup` 已关闭。
