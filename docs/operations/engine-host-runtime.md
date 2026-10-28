# Fixture Engine Host 的开发与验收

本说明适用于2026-10-26连续批次01：当前macOS上的 `test.fake` SDK、独立Provider进程、认证UDS、调度、持久事务与故障恢复。它不表示原生四引擎、DM/金仓、安装器、平台蓝绿或生产环境已经交付。生产根E07与正式计划实例保持独立。

## 包与版本身份

使用 `app.runtime.engine_host.packages.build_fixture_package` 构建V1/V2。两版应用源码和实际返回不同，包含同一公共SDK的独立签名副本；所有应用/SDK/SBOM字节进入inventory。`digest`为规范manifest的SHA-256，`payload_digest`只覆盖self-built实际文件，`runtime_digest`绑定OS/ABI、解释器、SDK和固定入口。三个摘要不可相互替代。

`save_package`只保存公开测试公钥、签名、manifest与不可变payload路径，不保存私钥。恢复用 `load_package(expected_digest=...)` 与持久任务/路由身份再核对；不能将有效签名但版本不匹配的包替换进去。

SDK支持S=1.0及S-1=0.9，S-2拒绝。未知mandatory字段拒绝；`optional_fields`原样保留。核心持有标准result decoder/schema，移除Provider源文件后标准结果仍可读。

## 运行与协议

`FixtureSupervisor`必须显式指定新的绝对私有workdir，创建OWNERSHIP。Host通过固定SDK入口加载已验签且复制后复验的payload，业务请求、manifest入口ID均不能指定任意命令/模块。payload只读，不将同UID或管理员代码执行描述为安全沙箱。

当前OS使用UDS，两端核真实UID/GID/PID。Provider launch secret仅经继承匿名fd传递，4字节长度+32字节原始secret，无argv/env/落盘副本。fresh双nonce、方向、双方peer、host/launch、版本与包/runtime身份、deadline进入transcript；共享SDK用cryptography HKDF-SHA256及HMAC-SHA256，各方向key/序列独立。业务帧和握手都用有界严格JSON，拒重复key、非有限数、错误类型/深度/UTF-8/长度，响应落账前比较完整context。

一条认证连接支持并发多路复用；实际连接数不超过配置上限。默认frame解码上限1MiB、累计调用输出64MiB、Host队列256、inflight8、IPC连接上限32，manifest/部署策略可收紧。大型结果按有界分块重组并核size/hash，不把部分结果当完整结果。

```python
future = supervisor.submit("invoke_read", {"echo": "example"}, timeout=10)
future.request_id
future.cancel()                       # 尚在排队时不会派发
supervisor.cancel(future.request_id)  # running时合作取消，必要时安全终止
envelope = future.result()            # 标准result envelope，业务在values
values = supervisor.call("invoke_read", {"echo": "example"})
```

排队/执行deadline都生效。已提交效果后取消仍标completed/待对账，不能改成none；断线、强制终止而效果无法确认时不盲重试execute。`prepare/execute/verify`只用于本批独占SQLite合成效果表，不接真实数据库。

Supervisor实际消费CrashPolicy：退避1/2/4/8/16/30秒封顶、±20%jitter、10分钟第6次崩溃开启15分钟circuit；manual_reset是显式恢复。自动恢复只用原受信package/generation，旧reader不能污染新launch。warm测试复用同一不可变staging但启动新PID/launch。

## 持久切流与回退

隔离FixtureStore使用独立SQLite、事务lease/fence/generation CAS，准备任务时固定版本/SDK/schema/capability/授权/输入/operation/attempt/deadline。相同请求号仅回放完全相同语义；参数或权限变化必须冲突。终态回执整体不可变，verify/reconcile的单调转换原子更新task与result。

更新必须完整经过验签、staging、真实launch、handshake/selftest、capacity admission、candidate_ready后才允许CAS。回退同样先启动/验证安全旧包，再以新的单调generation切流；不回拨数字，不执行共享数据down migration。

queued/running/retry/reconcile四类旧任务留在原精确版本。running须在CAS时真实inflight；旧effect先写入但无Host终态回执，再经verify恢复。旧代引用/inflight归零后才drain；未知效果保留引用并阻断回收。active、last-good、吊销、任务和artifact引用均进入回收判定。

owned PID/start time/UID/PGID及已验证成员lineage持久化。清理按精确成员身份发信号；leader消失且成员无法证明时blocked，不能凭裸PGID杀新复用组。真实IPC根包含私有短路径 `/tmp/tm-ipc-*`，不能只扫描workdir。临时runtime snapshot与保留的签名原包区分记录，明确retained_audit理由。

恢复只有在进程/socket均已回收，且再次核对OWNERSHIP、UID、精确launch artifact与摘要路径后，才删除运行副本并记cleaned；文件实际仍存在时不能报清理成功。尚未分配PID的已验证stage可以保留为retained_audit，必须写明理由。公开报告对结构化字段及journal列内嵌JSON同时脱敏，保留ownership token的SHA-256供交叉检查。

## 实际执行入口

项目根目录使用现有 `.venv-mac-primary/bin/python`。所有workdir必须新建、绝对、私有；以下 `/ABS/...` 为本次实际目录占位，不可指向用户既有目录。

先测单代calibration并冻结profile，再消费同一产物formal：

```bash
PYTHONPATH=backend ./.venv-mac-primary/bin/python scripts/measure_engine_capacity.py --fixture-profile local-fake --kind single --phase calibration --samples 30 --workdir /ABS/NEW/CAL --report /ABS/REPORT/single-calibration.json
PYTHONPATH=backend ./.venv-mac-primary/bin/python scripts/measure_engine_capacity.py --fixture-profile local-fake --kind single --phase formal --samples 30 --profile /ABS/REPORT/local-fake-single.profile.json --workdir /ABS/NEW/FORMAL --report /ABS/REPORT/single-candidate.json
```

双代使用精确single profile+candidate，calibration/formal各跑20轮；formal额外传双代profile：

```bash
PYTHONPATH=backend ./.venv-mac-primary/bin/python scripts/measure_engine_capacity.py --fixture-profile local-fake --kind dual --phase calibration --single-profile /ABS/REPORT/local-fake-single.profile.json --single-candidate /ABS/REPORT/single-candidate.json --workdir /ABS/NEW/DUAL-CAL --report /ABS/REPORT/dual-calibration.json
PYTHONPATH=backend ./.venv-mac-primary/bin/python scripts/measure_engine_capacity.py --fixture-profile local-fake --kind dual --phase formal --profile /ABS/REPORT/local-fake-dual.profile.json --single-profile /ABS/REPORT/local-fake-single.profile.json --single-candidate /ABS/REPORT/single-candidate.json --workdir /ABS/NEW/DUAL-FORMAL --report /ABS/REPORT/dual-candidate.json
```

统一runner正式fixture准入消费单代实际profile/candidate，执行20轮与八kill点：

```bash
PYTHONPATH=backend ./.venv-mac-primary/bin/python scripts/check_fake_provider_runtime.py --fixture-profile local-fake --single-profile /ABS/REPORT/local-fake-single.profile.json --single-candidate /ABS/REPORT/single-candidate.json --cycles 20 --kill-case all --workdir /ABS/NEW/RUNTIME --report /ABS/REPORT/runtime.json
```

`--fixture-calibration`仅用于独立开发/故障夹具，报告会记录显式harness预算，不能冒充正式容量准入。缺profile/候选、错误产物/OS/ABI/load、摘要漂移、预算超限或必需采样不足时，启动或CAS前非0退出。

## 负载与故障证据

- 单代：30cold、同stage的30warm、至少200次invoke_read并发1/4/8、独立超限拒绝场景、20轮生命周期；保留原始样本、统计法、PID/资源时序和清理。
- 双代：两版真实进程共存、20轮CAS/排空/回退、在途旧任务、RSS/进程/FD/连接/队列峰值与排空。预算明确只证明本机fake，不认证真实驱动或V2独立厂商环境。
- 八Host SIGKILL：stage_complete、provider_started_before_handshake、selftest_before_cas、cas_after_commit、dual_generation_inflight、effect_after_commit_before_receipt、drain_midway、rollback_after_cas。外层controller真实SIGKILL，全新Host/SQLite连接仅凭持久身份/journal恢复。
- 错包、吊销/不兼容、stale fence、重复请求、晚回执、PID/PGID重用及未知效果均有拒绝叶。结果不能仅依据事件总数或手填passed。

候选报告保持 `fixture_only=true` 和 `qa_ops_acceptance=pending`。calibration不是正式通过，failed run保留原始记录；重新校准必须重新冻结并重跑formal。正式计划test/output instances、生产信任、真实引擎、平台A/B、预生产/现场不由本工具签发。
