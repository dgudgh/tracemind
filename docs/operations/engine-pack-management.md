# 本机引擎包管理（实验通道）

本入口只接受 `.tmengine` 测试信任根及 `test.fake` 的运行验证。服务是独立本机进程，不经过平台登录/API。`--mode production` 固定拒绝；真实管理员准入、生产信任根、四原生 Provider 和业务热切流仍需单独交接。

## 配置与启动

使用独立私有目录、测试公钥策略和显式 OS UID 方法白名单。仓库目录必须归服务进程 UID 所有且模式 `0700`；策略文件必须同 UID 所有且模式 `0600`。白名单格式：

```json
{"schema":"tmengine.lab-management-policy/v1","allowed_uids":[501],"allowed_methods":["list","inspect","verify","install","initial-activate","status","recover","initial-rollback","upgrade","rollback","disable","uninstall","gc"]}
```

`allowed_uids` 必须填实际实验管理员 UID，示例 `501` 不能直接照搬。普通平台账号/业务权限不会映射为本机管理员。当前 socket 私有目录 `0700`、socket `0600`，实验客户端须与服务属于同一 OS UID；白名单仍按每次 OS peer 验证且可撤销。跨 UID 的生产管理员接入需要专门组权限、服务账号和角色验收。测试信任文件采用 `tmengine.lab-trust/v1`，只放公钥；不得使用真实生产密钥材料。

```sh
.venv-mac-primary/bin/python scripts/engine_pack_manager.py serve \
  --mode lab --repository /absolute/private/repository \
  --trust-policy /absolute/private/lab-public-trust.json \
  --admin-policy /absolute/private/admin-policy.json
```

服务输出一行 JSON，包含 `socket`、`server_uid`、`server_pid`。将这两个前者传给客户端。socket 位于 `/tmp` 的私有且绑定精确仓库路径的目录中，以避开 macOS Unix socket 路径长度限制。客户端核对 socket owner/mode 与服务端 OS peer UID/PID；服务端核对客户端 OS peer UID/PID，再按显式方法白名单授权。请求使用每连接随机 nonce、严格 JSON、单次调用和 64 KiB 帧上限；方法及参数字段固定。服务串行处理管理命令，仓库和 Host SQLite 另有持久事务。

```sh
.venv-mac-primary/bin/python scripts/engine_pack_manager.py call \
  --socket /absolute/path/from/server-output --server-uid 501 list
```

## 导入、验装与首次激活

包必须先由离线受控流程放入 `<repository>/incoming/`，客户端只传 `.tmengine` 叶名和预期 `sha256:` 归档摘要。`verify` 读取并验签；`install` 按精确摘要和幂等 `request-id` 写不可变包目录。安装不是激活。

```sh
... call --socket "$SOCKET" --server-uid "$SERVER_UID" verify \
  --import-name package.tmengine --archive-sha256 sha256:...
... call --socket "$SOCKET" --server-uid "$SERVER_UID" install \
  --import-name package.tmengine --archive-sha256 sha256:... --request-id install-001
... call --socket "$SOCKET" --server-uid "$SERVER_UID" initial-activate \
  --digest sha256:... --request-id activate-001
... call --socket "$SOCKET" --server-uid "$SERVER_UID" status
```

`initial-activate` 仅在该 engine 尚无路由时执行。它重新核对已安装精确包，在真实隔离子进程和签名 UDS 上完成 Host/Provider 握手、selftest 和 heartbeat 就绪后，才通过 Host Store 的原子初始路由写入 generation 1。它当前只接受 Host 的 `test.fake` 固定入口；其他签名测试包可安装但不能冒充已经运行。服务崩溃后 `status` 会显示持久路由和 `live_engines` 差异；先执行 `recover`，由 Host 所有权记录精确核对 PID 启动时间、UID、进程组、owner token、包摘要及运行时快照，再清理和按已安装摘要重启。所有者身份不匹配会阻断恢复。`initial-rollback` 只清理由失败初激留下的未接流状态，任何已写入路由或任务后的回退都会拒绝；Provider 热切流由 T13b/CORE 合同处理。

## 实验室生命周期扩展

`upgrade`/`rollback` 需要启动服务时用 `--lab-capacity-budget` 传入私有、包摘要和顺序精确匹配的显式测试预算；正式容量证据还需同时提供 `--lab-capacity-profile` 和 `--lab-capacity-candidate`。实验室预算是投影测试，不能当作 QA/OPS 正式容量签字。每个方向的旧/新包顺序不同，回退要重新配置对应预算并重启服务，先调用 `recover`。

```sh
... call --socket "$SOCKET" --server-uid "$SERVER_UID" upgrade \
  --digest sha256:... --expected-generation 1 --request-id upgrade-001
... call --socket "$SOCKET" --server-uid "$SERVER_UID" rollback \
  --digest sha256:... --expected-generation 2 --request-id rollback-001
... call --socket "$SOCKET" --server-uid "$SERVER_UID" disable \
  --digest sha256:... --request-id disable-001
... call --socket "$SOCKET" --server-uid "$SERVER_UID" uninstall \
  --digest sha256:... --request-id uninstall-001
... call --socket "$SOCKET" --server-uid "$SERVER_UID" gc
```

热切流使用 Host `FixtureUpdater` 的 prepared→verified→staged→candidate_started→handshake_complete→selftest_complete→candidate_ready→CAS 事务和每引擎 lease/fence。就绪前崩溃保持旧代次；CAS 后崩溃由 `recover` 清理精确 owner 后恢复当前摘要。旧任务引用未释放时旧 Host 继续保留；`gc` 仅选择已停用或标记退役的包，并在引用、在途/待对账任务、活动路由、last-known-good 和未完成更新事务都允许时清理。`disable` 调用 Host Store 的任务准入门禁，阻止新任务绑定；同 request 的已绑定任务可幂等回读，不会被停止。卸载前先 quarantine 阻止并发新绑定、复查引用，删除受控安装目录后提交仓库记录，SIGKILL 中断可由 `recover` 续完。历史标准结果仍由核心 `read_result` 独立校验回读。

每个允许操作和拒绝写入 `management.sqlite` 的链式审计，包括 peer UID/PID、方法、操作 ID、摘要、代次、结果和原因。服务不能写审计时不能返回成功。`LabRepository` 另保存安装 journal 与审计。服务被正常终止会停止持有的 Provider；SIGKILL 后使用 `recover` 复核并清理。

## 已知边界与交接

- 安装器已在内部固定 FD 快照，并按调用者预期 archive SHA 验证。管理服务前后再比对收据。若同 UID 可改写导入区，仍须依靠仓库内部 SHA 门禁拒绝替换内容；正式权限模型应使用独立服务账号隔离导入区。
- 管理协议只服务本机实验。正式模式需要操作系统管理员身份/角色的独立验收、生产信任根 E07、各 OS 的 IPC/身份验证、经认证的真实 Provider 和驱动；本地 fake 进程不能替代这些签字。
- `EngineRepository.guard_installed` 已提供从精确安装摘要到 Host 就绪/CAS 的持续信任和仓库锁；当前管理服务在初激、升级、回退及恢复中使用该上下文。正式域还需要仓库入口 profile 白名单、生产 OS 管理角色准入和非 fixture Supervisor 工厂。仅具备生产公钥元数据不能让 `FixtureSupervisor` 启动原生引擎。
- 本服务对 Host Store 写路由；尚未接入平台 Router/业务入口。管理切流实验只能证明独立的 fake Provider 进程/签名 UDS 与 Host 事务，不能声称平台实流量热更。正式业务控制应消费 Host Store 的 `active`/`route`、`launches` 和管理审计；业务热更新不能调用 `initial-activate` 绕过资格门禁。

### 生产启动期接口（未交接）

管理类已有 `mode="production"` 的可信注入入口：固定 `EngineRepository(mode="managed", namespace="tmengine.production")`、非 fixture `TrustStore`、认证过的 Host 工厂、正式容量准入回调、外部公开交接验证器、`service_uid` 与 `admin_gid`。缺任何一个或信任 ledger 缺 timestamp/含 `fixture_only=true` 均拒启动。外部验证器返回结构固定为 [production-admission-schema.json](/Users/zhaolongjie/codex/mesdbtools/reports/engine-extension-completion-20261027/management/production-admission-schema.json) 的 `tmengine.production-management-admission/v1`，含 `accepted=true`、`namespace`、`fixture_only=false`、`handoff_digest`、`e07_status=production-trust-accepted`、`admin_policy_digest`、`trust_root_version`、`service_uid`、`admin_gid`；针对包的 verify/install/initial-activate/upgrade/rollback/recover 还要返回精确 `package_digest` 与 `production_release_ready=true`。这些字段是**可信验证器的结果**，单独写一份同形 JSON 不构成 E07 证据。服务启动和新增/切换命令重新核对公开信任状态及策略摘要；已启动服务的 `status`、`disable`、`uninstall`、`gc` 仍受 OS peer 和管理员 allowlist 限制，但允许在外部准入撤销后执行事故止损与清理。

生产 Host 工厂须在可信启动代码里闭包绑定 `ProviderSupervisor(workdir, broker=<已启动 SecretBroker>, asset_resolver=<可信资产/授权回调>, restart=False)`。Broker 所需 `resolve_secret`、`authorize` 与 asset/fence 来源由部署环境提供，不能通过管理 IPC 的 `args` 注入。管理器在已安装摘要的信任 guard 内执行 Provider handshake/selftest/就绪/CAS；具体原生驱动、凭据作用域和真实连接仍需独立验收。

生产 transport 在固定服务 UID/管理员 GID 下使用私有 socket 目录 `0710`、socket `0660`、组可读 endpoint descriptor `0640`；OS `chown` 失败拒启动。客户端要求显式目标仓库根，校验目录/socket/descriptor 的 owner、group、mode，以及内核 peer UID/PID 和 descriptor 中进程启动时间。已用当前本机 GID 的合成 Probe 服务验证该软件路径；没有真实跨 UID 管理员账号、E07 签署或原生 Provider 接管证据。CLI 的 production 启动入口仍会拒绝，因为可信 Host 工厂、vault/asset/fence 来源与公开交接验证器尚未由部署启动器提供。
