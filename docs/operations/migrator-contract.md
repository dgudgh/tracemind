# 平台 Schema Migrator Contract（S2-1）

日期：2026-10-26。状态：已实现合同与只读启动门禁；未部署到生产、未执行客户迁移。

## 角色边界

- **migrator**：唯一允许在 managed-hot 模式执行 DDL 的角色；由 `scripts/migrate_schema.py apply` 或平台启动器中受控阶段调用，复用数据库级 `schema_lock`。
- **API/Worker**：设置 `TRACEMIND_SCHEMA_ROLE=verify` 时只校验结构，不创建表、不执行 revision、不修复索引。
- 默认 `TRACEMIND_SCHEMA_ROLE=all` 保留现有单进程启动行为；切到 managed-hot 前必须显式改为 `verify`。

## 合同

只读接口：`app.migrations.contract.migration_status(bind)` 返回：

```json
{
  "contract_version": 1,
  "revision": "contract-1",
  "checksum": "<all revisions 的规范化 SHA-256>",
  "applied": ["..."],
  "pending": ["..."],
  "bootstrap_pending": false,
  "ready": true
}
```

CLI：

```bash
python scripts/migrate_schema.py contract
```

`ready=false` 返回退出码 2；不会执行迁移。

## 启动门禁

`TRACEMIND_SCHEMA_ROLE=verify` 时，`main.py` 调用
`app.core.database.verify_schema_ready()`：

1. 取得 schema lock；
2. 检查所有 revision 与 bootstrap 已应用；
3. 调用 `validate_schema()` 校验注册表结构；
4. 任一不满足即抛 `SchemaUpgradeRequired`，启动失败，进程不接流量。

该路径不执行 `initialize_schema()`，因此不会产生“API 进程偷偷 DDL”。

## 已知边界

- MySQL 非事务 DDL 不能承诺崩溃后“恰好执行一次”；本实现只保证迁移执行者互斥和结构校验，未实现完整 reconcile journal。
- 迁移失败后的自动 `reconcile_required`、T09 readiness 回执消费和平台蓝绿切流属于后续 S2/S3 工作，尚未完成。
- 本合同尚未在真实 MySQL managed-hot 双进程上验收；当前只有 SQLite 单元测试和现有迁移回归。
