# 热升级主干：验收标准与测试案例

> 适用范围：引擎宿主（engine host）的**排队、取消、崩溃恢复、热重启、进程归属清理**。
> 不含：native provider 的双协议握手（见 §6 未完成项 A）、达梦/金仓真机认证。
>
> 权威代码：[supervisor.py](../../backend/app/runtime/engine_host/supervisor.py)、
> [lifecycle.py](../../backend/app/runtime/engine_host/lifecycle.py)、
> [ownership.py](../../backend/app/runtime/engine_host/ownership.py)、
> 测试夹具 [fake_provider.py](../../backend/tests/fixtures/engine_providers/fake_provider.py)。

## 1. 一句话结论

**热升级主干已闭环**：`test_engine_host_dispatch.py` **24/24 通过**（此前 24 项全红）。
引擎相关套件从 **29 failed / 350 passed** 提升到 **12 failed / 367 passed**，
剩余 12 项集中在两块独立竖切（native provider 协议、LabManagement），见 §6。

## 2. 验收标准

验收按"层"给判据。**任一层不满足即不算通过**，不允许用另一层的绿灯抵。

### 2.1 调度与容量

| # | 判据 | 反例（必须被拒） |
| --- | --- | --- |
| S1 | 并发上限 `max_inflight` 生效：同时最多 N 个调用在跑 | 第 N+1 个直接执行 |
| S2 | 排队上限 `max_inflight × max_queue` 生效，超出报 `queue_full` | 无限入队 |
| S3 | 排队超时报 `queued_deadline_exceeded`，且**该调用绝不派发** | 超时后仍执行副作用 |
| S4 | 排队中取消释放槽位，后续调用能补位 | 取消后位置永久占用 |
| S5 | 40 个并发读里每个响应与请求一一对应（不串包） | 交叉响应 |

### 2.2 取消语义

| # | 判据 |
| --- | --- |
| C1 | 排队中取消 → 调用方拿到 `call_cancelled` + `effect_state="none"`，且从未派发 |
| C2 | 运行中取消 → 必须**通知 Provider**（独立控制连接），不能只在本进程打标记 |
| C3 | 取消时副作用已提交 → `effect_state="completed"`，并能用 `verify` 找回结果 |
| C4 | Provider 不合作（`ignore_cancel`）→ 超过 `cancellation_grace` 后杀**整棵进程树** |

**C3 是这一组里最容易被做成假绿的**：只把 future 置成取消、不区分副作用是否已提交，
会让"已扣款却报取消成功"这种事故在测试里看不出来。

### 2.3 崩溃与恢复

| # | 判据 |
| --- | --- |
| R1 | `kill()` 是故障注入：staging/socket/monitor 都要留着，由崩溃策略决定是否重启 |
| R2 | 6 次连续崩溃触发熔断（`QUARANTINED`），`manual_reset()` 能清熔断并重新拉起 |
| R3 | 30 次 warm restart 复用同一不可变 staging，每次换 `launch_instance_id`，且不产生 crash 事件 |
| R4 | 单次调用超时后自动恢复（杀进程树 + 从原 staging 重新拉起），引擎不至于永久不可用 |
| R5 | leader 崩溃后，之前观测到的后代进程必须被回收（`cleanup_report().processes_remaining` 为空） |

### 2.4 身份与安全

| # | 判据 |
| --- | --- |
| I1 | 对端 pid 不等于我们启动的那个 → `peer_identity_mismatch`，不建立会话 |
| I2 | 有非授权进程连到 Provider 的 socket → Provider **自停**（退出码 2），宿主报身份问题 |
| I3 | Provider 自述与身份矛盾（`inflight=-1`、`launch_instance_id` 不符）→ 熔断并**抛错**，不许静默返回 |
| I4 | 私有 workdir 的校验只有一处权威实现（相对路径、符号链接、已有内容都拒） |
| I5 | 伪造 `input_digest` 不能绕过幂等（只在会产生副作用的调用上校验） |
| I6 | 请求 `deadline` 已过期 → 不派发 |

### 2.5 输出与契约

| # | 判据 |
| --- | --- |
| O1 | `output_bytes` 超过 `max_output_bytes` → `output_limit_exceeded`（派发前判定） |
| O2 | 单帧超过 1 MB → `frame_too_large` |
| O3 | `optional_fields` 未知字段原样回传；`context` 由宿主填满 20 个字段后透传 |
| O4 | 结果身份（`provider_version`）不能被猜测：与 `context.provider_version` 不一致即拒收 |

## 3. 测试案例矩阵

全部位于 [test_engine_host_dispatch.py](../../backend/tests/test_engine_host_dispatch.py)，
下表 `ID` 与文件内用例名一一对应。

| ID | 用例 | 覆盖判据 | 状态 |
| --- | --- | --- | --- |
| TC-01 | `test_eight_inflight_multiplex_without_crossed_responses` | S1 S5 O3 | ✅ |
| TC-02 | `test_queue_is_bounded_cancel_frees_slot_and_deadline_never_dispatches` | S2 S3 S4 | ✅ |
| TC-03 | `test_running_cooperative_cancel_and_read_deadline` | C2 R4 | ✅ |
| TC-04 | `test_uncooperative_call_terminates_owned_process_tree` | C4 | ✅ |
| TC-05 | `test_cancel_after_commit_reports_completed_effect_and_verify` | C3 | ✅ |
| TC-06 | `test_real_crashes_consume_policy_open_circuit_and_manual_reset` | R1 R2 | ✅ |
| TC-07 | `test_thirty_warm_restarts_reuse_immutable_stage_without_stale_reader_failure` | R3 | ✅ |
| TC-08 | `test_leader_crash_reclaims_previously_observed_owned_child` | R5 | ✅ |
| TC-09 | `test_wrong_peer_pid_fails_closed_and_cleans_launch` | I1 | ✅ |
| TC-10 | `test_invalid_heartbeat_quarantines_live_worker` | I3 | ✅ |
| TC-11 | `test_forged_input_digest_cannot_disable_effect_idempotency` | I5 | ✅ |
| TC-12 | `test_stale_deadline_is_rejected_before_dispatch` | I6 | ✅ |
| TC-13 | `test_output_limit_streamed_result_and_unknown_optional_roundtrip` | O1 O2 O3 | ✅ |
| TC-14 | `test_result_identity_and_mandatory_bits_are_not_guessed` | O4 | ✅ |
| TC-15 | `test_s_minus_one_real_provider_and_core_decoder_after_removal` | O4 | ✅ |
| TC-16 | `test_sdk_dispatch_rejects_bad_params_before_provider`（4 参数化） | O1 | ✅ |
| TC-17 | `test_signed_incompatible_package_is_rejected_before_spawn`（4 参数化） | 启动前校验 | ✅ |
| TC-18 | `test_stage_tamper_between_verify_and_launch_is_rejected` | 验签到启动之间不许改字节 | ✅ |
| TC-19 | `test_ownership_mismatch_never_kills_or_unlinks_a_live_instance` | 身份不符不误杀 | ✅ |
| TC-20 | `test_same_uid_wrong_real_pid_cannot_claim_provider_endpoint` | I2 | ✅ |
| TC-21 | `test_explicit_private_workdir_rejects_existing_content_and_links` | I4 | ✅ |

安全侧另由 [test_engine_host_security.py](../../backend/tests/test_engine_host_security.py)
（12 项，含帧解析与密钥不外泄）覆盖，本表不重复。

## 4. 复现命令

```bash
cd /Users/zhaolongjie/codex/mesdbtools
# 主干（必须 24/24）
PYTHONPATH=$PWD/backend .venv-mac-primary/bin/python -m pytest -q \
  backend/tests/test_engine_host_dispatch.py -p no:randomly
# 宿主安全（必须 12/12）
PYTHONPATH=$PWD/backend .venv-mac-primary/bin/python -m pytest -q \
  backend/tests/test_engine_host_security.py -p no:randomly
# 引擎全量（当前 12 failed / 367 passed，剩余项见 §6）
PYTHONPATH=$PWD/backend .venv-mac-primary/bin/python -m pytest -q backend/tests/ \
  -k "engine_host or engine_pack or engine_provider or engine_management or engine_sdk \
      or engine_registry or engine_catalog or engine_trust or engine_secret or engine_task \
      or engine_cluster or platform_router or platform_update" \
  --tb=no -p no:randomly
```

**判定口径**：主干与安全两组必须 **0 failed**；全量允许 §6 列出的已知项失败，
但**不允许出现新的失败模块**——新增一个失败模块等于回归。

## 5. 本轮实测（29 → 12）

| 时点 | failed | passed | 说明 |
| --- | --- | --- | --- |
| 开工前 | 29 | 350 | dispatch 24 全红 |
| 队列+取消+Provider 扩展后 | 15 | 364 | dispatch 24/24 转绿 |
| 入口约定与 workdir/peer 修正后 | **12** | **367** | security 12/12 |

失败模块分布：`test_engine_provider_rpc` 4、`test_engine_management_service` 3、
`test_engine_cluster_process` 3、`test_engine_pack_repository` 1、`test_engine_catalog_view` 1。

## 6. 未完成项（按根因归类，不是 12 个独立缺陷）

### A. native provider 的双协议握手（8 项，最大一块）

涉及：`test_engine_provider_rpc` 4、`test_engine_cluster_process` 3、`test_engine_catalog_view` 1。

**根因**：`ProviderSupervisor` 继承了 fixture 的 `_spawn`/`_connect`，但 native provider 走的是
**完全不同的启动协议**——`provider.main(launch_fd, root)` 从 fd 读
`token + config`（含 `identity/capabilities/connection_id/handshake_deadline/secret_broker`），
握手用 `transcript` 绑定双方 peer 身份；而 fixture 是 stdin 传 token、plain hello + HMAC。

**已做**：入口约定改为类属性（`ENTRYPOINT_ID` / `ENTRYPOINT_FILE`），`ProviderSupervisor`
覆盖为 `python.sdk.provider.v1` / `engine_provider.py`——这两处从"永远 entrypoint_missing"
推进到"能起进程但握手协议不匹配"。

**下一步**：在父类加 `_native_spawn()`（管道传 launch 配置 + `pass_fds`）与
`_native_connect()`（transcript 绑定 + peer 校验），由 `ProviderSupervisor` 覆盖。
**不要用 fixture 的握手冒充**——那会让 8 项"变绿"而实际跑不通真实驱动。

### B. LabManagement 侧（3 项）

`test_engine_management_service` 3 项。属引擎包管理流程（install/activate/upgrade 的准入与状态机），
与宿主调度正交，未在本次范围内。

### C. 安装验收 kill 用例（1 项）

`test_engine_pack_repository.py::test_whole_installation_runner` 报 `kill_cases: 0`：
整机安装验收脚本里"杀进程"用例没有执行。需要先确认是脚本编排问题还是依赖缺失，
不能直接改断言。

## 7. 改了哪些代码（本轮）

| 文件 | 改动 |
| --- | --- |
| `supervisor.py` | 真队列（worker 池 + pending）、`_JobFuture` 取消语义、`_drop_pending`、`_notify_provider_cancel`、`_context_for` 填满 20 字段、`_recover_after_timeout`、`_observe_children`/`_reap_observed_children`、`kill()` 语义修正、workdir 走 `ensure_private_workdir`、`owned_resources()` 返回可清理记录、入口约定类属性 |
| `provider_supervisor.py` | `_stage` 与父类同签名、覆盖入口约定 |
| `fake_provider.py` | context/optional_fields 回显、`output_bytes`、`spawn_child`、`cancel` 通道、可中断 sleep、execute 返回 value、`reply_delay`、heartbeat 自述字段、非授权对端自停（退出码 2） |

**刻意没做的事**：没有为了让测试变绿而放宽断言；没有把 native 握手降级成 fixture 协议。
