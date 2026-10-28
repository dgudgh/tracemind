# 数据库引擎扩展开发入口

验证日期：2026-10-27。原生身份是 `oracle/mysql/postgresql/sqlserver`；平台保留 `dm/kingbasees` 扩展位置。按用户当日决定，国产库具体功能延后，不以源码候选或测试驱动宣称已支持。

## 接口和执行边界

扩展使用固定 `python.sdk.provider.v1` 启动入口；包内固定文件 `engine_provider.py`，不是任意启动命令。该文件导出 `ENGINE_ID`、`DRIVER`、`CAPABILITIES`、`PROVIDER_VERSION` 和 `Provider.invoke(operation, config, arguments, expected_identity=...)`。固定参数来自验签后的 `provider_profile`，SDK 会与实际模块逐项比对。平台无需为新的 `vendor.engine` 增加执行分支。已有引擎 publisher 不能被另一个厂商替换；四原生身份仅接受 `tracemind`。

当前 SDK 接入的是固定只读 operation，非空业务参数、自带连接配置、SQL、密码和写操作均拒绝。新增有参或写能力须先扩展受审参数/权限/效果合同及 conformance，不通过修改一份清单绕过。宿主仍管理登录权限、资产范围、任务审批/版本、deadline/fence、凭据租约及结果持久化。

数据库连接配置和宿主冻结的 `expected_identity` 只能通过 Host Secret Broker 在内存交付。Provider 使用标准 SDK 的 BrokerClient，关闭游标/连接后释放租约；不得在参数、日志、返回值或持久状态中保存凭据。数据库产品/实例证据不匹配或缺失就停止，不能改用协议相似产品的驱动。

## 构建与安装

自研模板调用 `app.runtime.engine_host.provider_packages.build_provider_package`；第三方调用 `build_extension_package`，传明确 engine_id、固定 adapter 源码、driver、operations 和调用者持有的内存 Ed25519 测试 key。`backend/tests/test_engine_provider_rpc.py` 提供完整可执行扩展示例、实际独立进程、Broker 和结果回读；示例 driver 是合成字节，不能进入客户包。构建器不会从文件、环境或网络读取生产私钥。

构建结果具有 manifest、detached signature、签名 inventory、分类、SBOM、ABI/runtime 摘要；`build_archive` 生成 `.tmengine`。使用当前信任域的 `EngineRepository` 安装到受控导入区；`Catalog.register_installed` 会重新验证当前信任和实际磁盘字节。安装只表示文件合格，需 Host 握手、自检、驱动/资产能力验证后才可声明可用。

测试环境可以显式使用 `LabRepository` 和实验室管理入口；正式启动必须有独立生产信任、E07、精确容量准入和合法驱动。测试/生产签名用途相互隔离。`fixture_only` 的包显示为不可执行测试条目，不作为正常客户能力。生产渠道缺少外部准入时拒绝启动，不能回落实验室模式。

## 离线符合性与负例

在仓库根运行：

```sh
.venv-mac-primary/bin/python -m pytest -q backend/tests/test_engine_sdk_contract.py backend/tests/test_engine_provider_profiles.py backend/tests/test_engine_provider_rpc.py backend/tests/test_engine_registry_installed.py
```

这条固定执行器覆盖 SDK S/S-1/S-2、严格参数/结果、源码与签名 driver/operation 对照、厂商扩展身份、磁盘字节、吊销后的拒绝、Broker 作用域/重放、缺驱动及实际进程清理。测试临时 key 不保存。通过代表当前本机离线合同/子进程验证，不代表数据库版本、许可证、六引擎、生产根、预生产或现场验收。

发布前还必须逐能力给出真实输入/输出/错误/权限/副作用对照、第三方驱动来源/hash/SBOM/许可、精确 OS/CPU/ABI 与容量报告。历史结果使用核心 decoder 读取，不能依赖卸载后的插件代码。旧任务摘要不足时重新 prepare/授权，禁止填上当前版本伪造历史。

相关合同：[凭据租约](engine-secret-broker.md)、[信任生命周期](engine-trust-lifecycle.md)、[离线安装](engine-pack-offline-installation.md)、[本机管理](engine-pack-management.md)。
