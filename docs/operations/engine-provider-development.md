# Engine Provider 源码切片与接入边界

## 2026-10-27 后续实现更新

[标准接入教程](engine-extension-quickstart.md)与[Broker/SDK 接线](engine-secret-broker.md)记录本轮已实现的签名独立子进程、标准扩展包构建器、实际仓库验证和动态扩展 Registry 摄入。下文“仅源码切片/尚未接 SDK”的文字是本轮早期边界，已被上述局部实现取代；真实数据库、完整旧能力迁移和发布认证仍未完成。国产库具体功能按用户本日要求延期，只保留扩展位置。

2026-10-27：`backend/app/engine_providers/` 仅是离线开发源码切片，不是 SDK RPC Provider、签名包或已激活产品。四个原生模块提供固定的产品版本标识查询、版本、会话数、锁数及基础指标查询。调用接收 DB-API connect 的关键字参数或测试注入的连接工厂；宿主负责安全解析连接配置和秘密、目标授权、进程隔离、期限及驱动参数白名单。提供者不接收 SQL、脚本或 shell 命令。驱动仅在调用时导入。结果行数、序列化格式和字节数受限，异常仅返回安全分类；视图权限由数据库账户配置。

版本 banner 是有限产品证据，不构成防伪产品身份。Oracle DBID/DB_UNIQUE_NAME/实例名、MySQL server_uuid、PostgreSQL system_identifier、SQL Server database_guid/服务器名、DM DB_MAGIC/实例名与宿主资产逐项匹配；SQL Server 数据库 GUID 可复制/恢复变化，PostgreSQL system_identifier 可由同集群节点共享，这些不能单独证明唯一节点或防止伪造。实例和集群的完整身份、兼容模式、可信产品身份均未完成。未经真实驱动和目标验证，不应把这些能力发布为可执行生产能力。仅有一个查询指标且没有完整旧行为、权限和拓扑回传；不可把它标成 EP-T02b 已完成。DM 和 KingbaseES 各有候选只读 SQL 与各自驱动懒加载；缺驱动返回 `driver_unavailable`，未登记能力返回 `unsupported`。金仓稳定资产身份字段尚无充分依据，连接且识别版本后返回 `identity_unavailable`，不会执行采集，也不会用 PostgreSQL 驱动替代金仓。DM DB_MAGIC 在恢复等操作后可能变化，需重新核对授权资产。真实首包须提供合法驱动、目标与授权后完成产品识别、逐能力核验及持久结果回读。

`app.engine_registry` 作为不可变内存模型保存身份、族、协议和不可用原因。`register(..., verified=True)` 的布尔值是**可信宿主签名验证之后的内部输入**，绝不能从请求/包清单直接传入；当前模块本身并不验签或激活包。`tracemind` 是本开发切片的原生 publisher 策略标识，实际信任根、命名和策略尚待 REL 确认。持久 generation/CAS、撤销/隔离、权限过滤、catalog API 尚未接线。旧值桥只转换四个规范 db_type，保留未知原值，不猜产品。

SDK/Host 接入前须完成：在可信安装和签名策略限定 engine/publisher，扩展现有仅 `test.fake` 的 Descriptor/context/参数校验，以 capability 对应版本化输入和输出 schema 进行分派；进程 RPC、任务绑定、deadline、权限、秘密和持久结果继续由宿主负责；不得把调用者传来的 `verified` 视为证明；将未认证能力标为 unavailable。抽取源码之后逐能力与旧连接、采集、错误和权限行为对照，再由 T02c 迁移现有路由和残余分支。

候选国产 SQL 参考：[达梦动态性能视图](https://eco.dameng.com/document/dm/zh-cn/pm/dm8-admin-manual-appendix2.html)、[KingbaseES 运维手册](https://help.kingbase.com.cn/v9.3.11/PDF/KingbaseES%E6%95%B0%E6%8D%AE%E5%BA%93%E8%BF%90%E7%BB%B4%E6%89%8B%E5%86%8C.pdf) 和 [KingbaseES 参考手册](https://help.kingbase.com.cn/v8/PDF/KingbaseES%E6%95%B0%E6%8D%AE%E5%BA%93%E5%8F%82%E8%80%83%E6%89%8B%E5%86%8C.pdf)。版本、视图权限及兼容模式未在实例复核。

离线开发者最小调用：`from app.engine_providers.conformance import check`，`from app.engine_providers.dm import Provider`，再调用 `check(Provider, "DM Database Server 64", "PostgreSQL")`。该测试仅运行内存 DB-API 夹具，结果固定 `server_certified=false`；真实驱动、版本、ABI 和数据库未验证。新增源码模块可继承 `ReadProvider`，静态定义 `engine_id`、厂商驱动名称、固定查询和身份证据；上线仍需实现正式 SDK、版本化 schema、强身份和供应链验收，不能以此示例当作完整第三方发布模板。

每次 invoke 的 `expected_identity` 是宿主冻结、授权的资产值，必须按引擎字段逐项提供字符串；不能从 HTTP 请求或插件自报取信。身份查询失败、缺失或空值返回 `identity_unavailable`，不匹配返回 `identity_mismatch`，均不执行采集 SQL。金仓身份当前不提供该放行路径。离线执行：`PYTHONPATH=backend .venv-mac-primary/bin/python -m app.engine_providers.conformance`。返回仅是 source-fixture，server_certified 一律 false。

身份字段资料：[PostgreSQL pg_control_system](https://www.postgresql.org/docs/11/functions-info.html)、[SQL Server database_guid 生命周期与权限](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-database-recovery-status-transact-sql?view=sql-server-ver17)、[达梦 OGUID 为数据守护配置值](https://eco.dameng.com/document/dm/zh-cn/pm/data-watch-overview.html)，不能将 OGUID 推断为通用唯一实例标识。

达梦 DB_MAGIC 来源为 [V$RLOG 视图](https://eco.dameng.com/document/dm/zh-cn/pm/dm8-admin-manual-appendix2.html)；[官方集群 FAQ](https://eco.dameng.com/document/dm/zh-cn/faq/faq-dm-cluster.html) 指出主备 DB_MAGIC 不一致。此值不是密码学身份，也未在真实数据库验证。
