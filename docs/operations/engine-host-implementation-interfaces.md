# 本批 Codex 接手实现的模块接口

2026-10-26。用户已授权 Codex 实现清单、将简单工作交 dsh。原B00–B07/22组与隔离范围保持；这份短合同仅用于共享工作区并行实现，不扩展产品范围。

## 文件写入责任

- Codex主代理：engine_sdk、engine_host/ipc.py/supervisor.py/staging.py/ownership.py/packages.py、SDK固定launcher和fake Provider、相应协议/调度测试。
- 持久化实现代理：engine_host/store.py/update.py/orchestrator.py、`scripts/fixture_host_worker.py`、`scripts/check_fake_provider_runtime.py`、任务绑定/kill恢复测试。
- 容量实现代理：engine_host/metrics.py、scripts/measure_engine_capacity.py、容量测试。
- 包合同实现代理：engine_packs/trust.py/contract.py、相应测试/fixture。
- dsh简单工作仅限文档、CB01映射与交付证据索引；不得修改以上核心代码或正式计划状态。DSH Desktop界面目前被工具权限拒绝，交接单先保存在项目。

## 保持兼容并新增的运行时接口

`FixtureSupervisor(workdir=absolute_new_private_path, *, max_queue=256, max_inflight=8, max_connections=32, max_output_bytes=64*1024*1024, crash_policy=None, restart=True, cancellation_grace=...)`。

- `start(source, manifest, signature, trusted_keys, expected_namespace=..., expected_channel=..., generation=1, store_path=None)`：验证和复制后复验、建立完整身份/握手/自检，返回LaunchRecord。保持旧入参兼容；新参数可选。
- `call(method, params=None, timeout=60, context=None)`：同步包装；内部真实有界排队/并发。
- `submit(method, params=None, timeout=60, context=None) -> concurrent.futures.Future`：Future增加request_id；queued Future.cancel()不派发。`cancel(request_id)`对running调用发送合作取消；不合作则终止owned组并标未知。
- `status(request_id=None)`、`heartbeat()`、`drain(timeout=...)`、`stop()`、`kill(signal=SIGKILL)`、`restart()`、`manual_reset()`。
- `record`含pid/start_time/pgid、host_pid、launch_instance_id、socket_path、staging_dir、manifest_sha256/package_digest/payload_digest/runtime_digest/provider_version/sdk_version/generation。
- `identity`字典为上述版本身份，`events`为无secret的生命周期/调度记录，`owned_resources()`及`cleanup_report()`提供完整owned集合。
- `SupervisorError.code`保持；错误含category/retryable/effect_state/safe_message/diagnostic_ref。不把超时/丢连接当未执行。
- 稳定SDK错误/结果schema、严格JSON、HKDF/HMAC双向绑定与响应关联由主代理交付，Provider不私有import平台业务模块。

## 包构建接口（主代理负责）

`engine_host.packages.FixturePackage`保留version/digest/source/manifest/signature/trusted_keys，新增payload_digest/runtime_digest，可从manifest实际字节派生，禁止传入错误digest。

- `build_fixture_package(root, version='1.0.0')`：构建真实不同V1/V2 payload，包含固定入口和独立SDK runtime；临时内存Ed25519签名。返回FixturePackage。
- `save_package(package,path)`、`load_package(path, expected_digest=None)`：只保存公开信任材料/签名/路径；恢复须比较持久expected_digest，不能包内自授权或覆写task身份。
- `describe_context()`给当前OS/CPU/Python/Host/SDK/launcher digest，用于精确容量profile。签名产物在calibration/formal间复用，不重签。
- `FixturePackage.verify()`返回规范identity；构造/使用时均核实际manifest digest与payload，不能把V2当V1。

## 持久化与故障接口（实现代理负责）

任务在调用前prepare并固定engine/generation/package/runtime/SDK/schema/capability/operation/attempt/deadline/fence/input摘要，结果与原任务分开保存。提供状态/引用、update journal/lease/fence、launch ownership、安全回放/恢复、合成效果verify。

八Host kill点由独立controller启动worker，worker按阶段写持久journal并通过marker阻塞，controller真实SIGKILL；全新Host/连接从持久状态恢复。不得保留旧Host内存作为结果来源。使用Supervisor record保存owned PID/start_time/pgid/socket，恢复前只能清理匹配身份对象。

报告和runner稳定入口保持原命令参数，支持独立kill case/全部cases；每轮任务使用唯一ID，不通过硬编码事件数量判全通过。完整scope需消费容量预算；可注入测试限额，不可假造正式QA/OPS。

## 容量接口（实现代理负责）

复用上面的Supervisor与包构建工具；30cold+30warm、200 invoke_read并发1/4/8与超限、20生命周期轮、20双代切换。保存逐样本、冻结profile及同产物manifest，formal启动前精确核对OS/CPU/Python/ABI/host/sdk/launcher/package/payload/runtime/load/context/阈值/样本。

容量脚本可提供`--kind single|dual`。profile与候选报告仍是fixture_only/QA_OPS pending，禁止改正式test/output instances。raw/report含真实identity、采样源、单位、全部阈值和错误。
