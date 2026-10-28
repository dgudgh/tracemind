# 巡检历史结果对账

## 范围与安全边界

适用于平台配置库的旧巡检表 `db_check_result_new` 与规范结果表 `db_check_result`。
不是被巡检业务数据库的修复，也不是在线合并工具。迁移保留旧表，不删除旧证据，
不覆盖同 ID 冲突，不修改已有迁移版本号或伪造 journal。当前实现只在复制的 SQLite
验证；MySQL/PostgreSQL 的备份、停写、实际锁、失败恢复及升级重启仍需现场验收。

两个表都有数据不再一律视为错误：逐行比较 14 个迁移字段，允许内容一致的既有行和
仅存在于新表的后续结果。旧表缺行、同 ID 内容不同、不可无损表示的数据会阻断启动，
给出专用错误 `legacy_inspection_results_inconsistent`，而不是误报网络连接故障。
每次普通启动都会重新核对，即使原迁移版本已经记账；不会自动补写旧表的新记录。

这不是运行期间的持续同步。旧程序或外部写入者必须停用；数据库级 schema 锁只协调
遵守同一协议的维护进程，不能阻止不合作的旧版本直接写表。对账需要完整扫描保留证据，
耗时随历史规模增长，不属于毫秒级请求路径。每批最多 25 行，数据库端先检查单行文本
表示的总字节数，超过 1 MiB 时拒绝拉取和自动复制，需要单独审查，不裁剪原始证据。

## 先做只读检查

使用拟发布版本的运行环境，在发行包根目录执行。通过受保护环境提供 `DATABASE_URL`，
或用 `--database-url-env` 指定环境变量名；不要把含凭据的 URL 写入命令历史或文档。
当前入口为源码/具备该版本 Python 运行环境的维护 CLI；尚未验证独立二进制或容器发行包
携带此命令，不能据本文宣称两种制品已经可以完成客户升级。

```bash
python scripts/migrate_schema.py inspection-results-check
```

这个子命令不初始化表、不申请迁移写锁、不做 DDL/DML。输出 JSON 包含表名、数量、
缺失/冲突的有限 ID 样本、额外列、比较字段与 `snapshot_hash`，不输出 SQL/结论原文。
只读检查不等于一致性快照或业务停写，写入持续发生时须在维护窗口重新检查。

| 状态 | 处理 |
| --- | --- |
| `absent` / `consistent` | 旧表不存在或证据一致；不表示其他结构、业务或发布验收通过 |
| `missing` | 新表缺少旧表 ID；备份和停写后可使用下述补行入口 |
| `conflict` / `invalid_rows` / `invalid_schema` | 停止升级，保留两表和备份，由运维确认差异；不提供自动覆盖选项 |
| `evidence_too_large` | 有行超过读取上限，停止自动处理，另行审查原始证据 |
| `snapshot_changed` | 检查后内容发生变化，原 hash 作废，核实所有写入者已停再检查 |
| `unsupported_target_columns` | 目标存在自动补行未支持的额外列，需专门评估，不能丢列绕过 |
| `unsupported_storage` / `unsupported_target_triggers` | 事务存储或触发器不在支持范围，停止自动补行，不能临时关触发器或换引擎绕过 |
| `trigger_visibility_unverified` | 未正向确认迁移账号具有目标表的 TRIGGER 可见权限，不能将目录无行当作无触发器 |
| `copy_side_effect` / `invalid_target_rows` | 写后检查发现副作用或已有目标无效，禁止成功记账，停止后续发布 |

退出码：`0` 为该命令通过，`3` 为巡检证据不一致，`2` 为缺少确认参数或普通结构需要升级，
`1` 为其他已脱敏错误。自动化部署必须按退出码停止，而不是仅检查有 JSON 输出。

## 维护窗口内补缺失行

1. 确认目标库身份，制作并验证平台库完整备份与恢复方法，保留原安装包/镜像和配置。
2. 停止所有平台实例、worker、调度器、旧版本及外部写入进程。不能仅关闭浏览器。
3. 再执行只读检查，确认只有缺失行，没有冲突/无效数据，记录新 hash 和数量。
4. 将新 hash 传入维护命令，必须同时确认备份及停写；SQLite 也不豁免。

```bash
python scripts/migrate_schema.py inspection-results-reconcile \
  --backup-verified --service-quiesced \
  --expected-snapshot-hash <fresh-check-hash>
python scripts/migrate_schema.py inspection-results-check
python scripts/migrate_schema.py check
```

两个确认参数只是操作者的事实声明，工具不会自动制作备份或停止进程。
`check` 在指定开发 SQLite 模式下沿用原有初始化语义；需要零写入检查时只使用
`inspection-results-check`。待迁移结构应遵守[升级检查单](code-fix-upgrade-checklist.md)，
经授权再执行原 `apply`，补行命令不会代替结构迁移。

补行重新读取并校验 hash，持 schema 锁，只插入原先缺失的 ID。写前预测完整目标摘要，
写后核对完整目标与原源表全部列，检测触发器删除/改写无关结果、甚至同时改写源/目标的
副作用；不以“总行数相等”替代内容验证。源表始终保留；未知目标额外列不自动处理。
相同结果再次对账为零补行；旧 hash 过期必须重新检查，不能自动重试或自动覆盖。

## 失败与回退

MySQL/MariaDB 补行只接受两张 InnoDB 表，PostgreSQL 只接受普通本地表；两者的目标表
存在用户触发器即拒绝补行，避免事务回滚无法撤销跨非事务表或外部系统的副作用。
权限不足以核验目录时也不得继续。对应分支有隔离测试，真实引擎行为仍待验收。
MySQL 的触发器目录会按 TRIGGER 权限过滤，参见[官方目录说明](https://dev.mysql.com/doc/mysql-infoschema-excerpt/8.0/en/information-schema-triggers-table.html)。
工具正向核对当前账号直接的全局/当前库/目标表 TRIGGER 权限；只靠角色或通配库授权等
未被正向核实的情况拒绝自动补行。应使用已有授权的维护连接，不自动给应用账号加权限，
不把无法查看目录当作不存在触发器。
仅有全局授权时，还必须明确读到 `partial_revokes=OFF`；为 ON 或该能力未核实时不凭全局
记录放行，因为[部分撤销](https://dev.mysql.com/doc/refman/8.0/en/partial-revokes.html)可能排除当前库。
工具不修改该全局变量，不自动补权限；使用已有的可正向核实授权或停止维护。

失败即停止，不重启到不一致状态，不删除旧表、不删 journal、不尝试通过覆盖冲突恢复。
SQLite 的本地回归覆盖事务回滚；MySQL DDL 可隐式提交，非事务表、跨表/外部触发器副作用
以及非合作写入者不在本地回滚保证内。维护前必须确认事务存储、触发器及备份恢复方案。
如果失败，不自动恢复可能不兼容的旧程序；先按备份和实际 journal/结构核对，再决定修复
或恢复。换安装包本身不是数据回退。

完成后验证普通启动、就绪接口、原巡检列表/详情/报告数量和内容，确认旧写入者不再运行。
未经执行的现场步骤不得登记完成；本地测试不替代客户升级、真实恢复或重启验收。
