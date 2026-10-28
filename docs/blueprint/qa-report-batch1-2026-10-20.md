# batch-1 独立验收报告（QA / 严过关）

- 验收对象：`HEAD = cf7db4c`（`3a59416` fix: register the two unapplied migrations、`cf7db4c` feat: unified run outcome and readback）
- 基线：`0a622f3`
- 验收立场：**证伪优先**。实现方声明只作为"待验证命题"，每一条都用独立构造的数据与命令复现，不接受"已有测试通过"作为结论。
- 结论：**有条件通过（PASS，1 个 LOW 源码回归 + 1 个 MEDIUM 设计漏洞 + 3 项 LOW 观察项，均不阻塞本批交付）**
- 缺陷路由：`FIND-QA-02`、`FIND-QA-03` → **工程师修**（源码 Bug，我未改业务代码）；其余为观察项/产品决策。

---

## 0. 结论速览

| # | 验收项 | 判定 |
|---|---|---|
| 1 | 老库升级路径真的能走通（自建老库 + 注册表摘除负向对照） | **PASS** |
| 2 | 集合断言不是摆设（`versions/` 放探针 → 变红 → 删除 → 恢复绿） | **PASS** |
| 3 | outcome 映射边界（含 `cancelled/expired/空串/None/大小写/空格`） | **PASS** |
| 4 | 零结果运行的权限归属（B 用户 403、A 用户 200 + `evidence.state="missing"`、列表不泄漏） | **PASS** |
| 5 | 取消不谎报（running 保持 running + 文案；被拒 commit 终结为 cancelled） | **PASS** |
| 6a | 对抗：取消后执行端持续心跳是否永不收敛 | **CONFIRMED HOLE（FIND-QA-05）** |
| 6b | 对抗：取消后立刻 retry 是否允许 / 是否继承 snapshot | **PASS（行为符合设计）** |
| 6c | 对抗：同 idempotency_key 在 running 时重放 / 跨用户 | **PASS（同 run；跨用户独立 run，有安全收益）** |
| 6d | 对抗：去掉 `cancel_requested==0` 过滤后能否再 heartbeat | **PASS（拒绝）** |
| 7 | 契约基线是否真手术式 | **PASS（+23/−0、+283/−0，文档完全一致）** |
| 8 | 门禁与全量 | **PASS（2872 passed / 708 subtests；check_all --fast 全绿）** |
| 9 | 自建 7 个变异体，看声明是否真有守卫 | **7/7 被现有测试杀死** |

总计数：**PASS 12 项 / FAIL 0 项（阻塞级）/ 缺陷 2 个待修（LOW 级）+ 1 个设计洞（MEDIUM）+ 4 个观察项**。

---

## 1. 老库升级路径（自建，不复用实现方夹具）

**方法**：先把当前 schema 建好并插入一条 `success` 行，然后用**手写 DDL + 数据搬迁**（`copy → drop → rename`）把 `db_inspection_job` 退回本批之前的样子（无 `cancel_requested_at/by`、无 `ix_db_inspection_job_created_at`），另三张表 `DROP COLUMN`，并删掉 journal 里 3 条 revision。这与实现方的 `ALTER TABLE DROP COLUMN` 是不同机制，额外验证了**存量数据不丢**。

```
$ DATABASE_URL="sqlite:////tmp/qa_batch1/upgrade.db" TRACEMIND_AUTO_MIGRATE=1 \
  PYTHONPATH=$PWD/backend .venv-mac-primary/bin/python /tmp/qa_batch1/qa1_migration.py
sqlite version: 3.53.1
PRE-STATE ok: no new columns, no created_at index, legacy row present: [('legacyjob01', 'success')]
APPLIED: ['20261019_01_data_quality_schedule', '20261019_02_capacity_anti_flap', '20261020_01_inspection_job_control']
POST columns include cancel_requested_at/by: True
POST index present: True
data-quality columns: True
capacity policy columns: True
ORM round-trip on the new column: ok (legacy row preserved)
second boot applied nothing: []
NEGATIVE ok: withheld registry raised SchemaUpgradeRequired -> 已记 migrations 后仍缺失或不兼容的 schema 对象：db_inspection_job.cancel_...
```

判定 **PASS**：
- 三条 revision 都真的被应用，列/索引都回来了，存量行还在，且新列可经 ORM 读写；
- 第二次启动 `[]`（不是每次开机重跑 DDL）；
- **负向对照**：从 `runner.MIGRATIONS` 里摘掉 `20261020_01` 后同一个库 `SchemaUpgradeRequired` —— 说明"能升级"不是恒真命题。

> 附注（不影响判定）：`20261020_01_inspection_job_control.downgrade()` 是空实现，`downgrade_revision()` 会把 journal 行删掉但列留着，之后再 `upgrade()` 走 `if name in existing: continue` —— 幂等，不会炸；代价是"降级"名不副实（回滚只回滚 journal）。这属于既有约定（三个 20261019 迁移同样如此），本批一致。

## 2. 集合断言不是摆设（变异）

在 `backend/app/migrations/versions/` 放一个合法最小 revision `v29999999_99_probe.py`：

```
$ ... pytest backend/tests/test_migration_registration.py -q
E       AssertionError: revision modules exist but are never applied: ['v29999999_99_probe'].
1 failed, 7 passed
```

删除后：

```
8 passed in 1.38s
```

判定 **PASS**。同时确认真实缺陷方向也守住了：`test_every_revision_module_on_disk_is_registered` 用**集合差**（不是计数），增/删两侧各有断言（`..._no_module_missing_from_disk`）。探针文件与其 `.pyc` 已删除，工作区无残留（见 §10）。

## 3. outcome 映射边界

直接调 `app.services.run_status`（无 DB、无 HTTP、无夹具）：`/tmp/qa_batch1/qa3_run_status.py`，共 **86 条断言全过**。要点：

```
== (a) 未识别状态永不回落 success
status='sucess' / 'SUCCESSFUL' / 'ok' / 'done' / '<script>' / '0' / 'null' /
'undefined' / 'partially' / 'cancel' / 'timeout' / 'timedout' / 'interrupt'  ->  全部 unknown 且 requires_attention=True

== (b) 四个 declare-only 值
declare('success'/'partial'/'failed'/'cancelled'/'unknown'/'expired'/'nonsense') -> 全部 ValueError
normalize('plan_only'|'blocked'|'unsupported'|'needs_input') -> unknown（不会把自己当结果）
declare(x, '   ') -> ValueError

== (c) 无法判定即 unknown
timed_out -> outcome=unknown kind=timeout
interrupted -> outcome=unknown kind=lease_lost

== 实现方没提的状态
'cancelled'->cancelled  'expired'->expired  'failed'->failed  'partial'->partial  'success'->success
''->unknown   '   '->unknown   '\t\n'->unknown   None->unknown   0->unknown   5->unknown   object()->unknown
'cancelled '->cancelled   'CANCELLED'->cancelled   '  Expired  '->expired   'SUCCESS'->success   'timed_out '->unknown
```

补充语义（打印的真实回落表，无静默）：

| status | outcome | outcome_kind | terminal | percent / basis |
|---|---|---|---|---|
| `''` / `None` | `unknown` | `unrecognized_status` | False | None / `missing_job_status` |
| `running` | `None` | `None` | False | None / `unknown_stage_weight`（未编造百分比） |
| `queued` | `None` | `None` | False | 10 / `stage_weights_v1`（事件<2 时给 None） |
| `expired` | `expired` | `expired_in_queue` | True | None / `terminal_no_percentage` |
| `cancelled` | `cancelled` | `cancelled_before_run` | True | None |

判定 **PASS**。唯一需要记录的宽松点：`strip()+lower()` 会把 `" SUCCESS "` 也认成 success —— 这是**向规范方向**收铱（不会把未知变成功），可接受；但它意味着库里带空白的脏值不会暴露成 unknown，属于有意的宽容，建议知会产品。

## 4. 零结果运行的权限归属

自建：用户 A(id=7) 授权 asset 11（native 101），用户 B(id=8) 授权 asset 12（native 202）；A 有一个 **0 结果行**的 `failed` job；HTTP 断言走**真实 `AuthMiddleware` + 真实 `inspection` router**（不是假端点）。

```
[#4] owner detail: status=failed outcome=failed evidence=missing report=None
[#4] foreign detail=403 list_total=0 unknown_id=403
```

- A 读详情 **200**，`results.total_count==0`、`evidence.state=="missing"`、`outcome=failed/outcome_kind=collector_error`、`report is None`（不伪造报告）；
- B 读 A 的 run → **403**（且 `detail != "巡检运行不存在"`，不伪装成 404）；
- B 的列表 `total=0`、items 不含 A 的 job（不泄漏）；
- B 读一个不存在的 id → **403**（fail-closed，见观察项 O-1）。

判定 **PASS**。

## 5. 取消不谎报

```
[#5] running cancel -> status=running notice=取消已受理，采集可能仍在进行；结果尚未发布，结束后会如实标记为已取消
[#5] refused commit -> status=cancelled kind=cancelled_during_run rows=0
```

- running 取消：`status` 仍是 `running`，`cancel_requested=1`、`at/by` 有值、`cancel_attribution="recorded"`、`outcome is None`，文案含"采集可能仍在进行"；
- 随后 `commit_results` 因取消被拒：job 终结为 **`cancelled`**（**不是** `interrupted`/`timed_out`），`outcome_kind=cancelled_during_run`、`requires_attention=True`，且**一行结果都没落库**（`db_check_result` 计数 0）；
- queued 取消：立即 `cancelled` + `finished_at`，`outcome_kind=cancelled_before_run`、`requires_attention=False`、`status ∈ RETRYABLE`。

判定 **PASS**。

## 6. 对抗性场景（实现方未覆盖，逐条结论）

### 6a. 取消后执行端一直活着并继续心跳 → **永不收敛（MEDIUM，FIND-QA-05）**

```
[ADVERSARIAL] after 610s (timeout_seconds=60) and a cancel request, the job is still running; nothing reaped it.
[ADVERSARIAL] retry while running/cancel-requested -> JobError: 该任务不允许重试或已达到三次尝试上限; rows 2 -> 2
[ADVERSARIAL] after the heartbeat stops, the reaper calls it 'interrupted'
```

40 个心跳周期（每 15s）后，`timeout_seconds=60` 早已越过、取消请求也早就在，job 仍然是 `running`。原因链（全部可指到行）：

1. `jobs.reap_interrupted()`（`app/services/inspection/jobs.py:354`）只回收**租约过期**的 running，执行端心跳在续租它就永远不满足条件；
2. `timeout_seconds` 只在 `commit_results()`（`jobs.py:616`）里检查，**执行端不提交就永远不检查**；
3. `retry()`（`jobs.py:248`）要求父 job 已终态，`running` 不在 `RETRYABLE` → 操作员不能另起一次；
4. 再次 `cancel()` 只会重写 `cancel_requested_at`，不改变状态。

**结论：这是一个真实的"永不收敛"洞**，只靠执行端配合才能终止。触发条件：执行进程活着但忽略 `cancel_requested`（旧版本 agent、卡在长查询、bug）。运维视角：列表里一条永远"取消中"的运行，既不能 retry 也不能强制终结。建议（不在本批改）：给 running 增加**总超时兜底 + 取消请求宽限期**（如 `cancel_requested_at + N 分钟` 后由 reaper 终结为 `cancelled`，并保留"目标端情况未知"的 kind），或允许对 running 强制终结并明确标 `unknown`。

### 6b. 取消后立刻 retry → **允许，且继承已取消的 snapshot（符合设计）**

```
[ADVERSARIAL] retry of a cancelled run allowed: child=ab0b982f... attempt=2 status=success
```

`cancelled ∈ RETRYABLE`；child 用 `deepcopy(parent.policy_snapshot_json)`，`attempt=parent+1`，`parent_job_id` 指向父，重复 retry 返回同一个 child（`parent_job_id` 唯一约束）。判定 **PASS**：继承的是"操作员当初评审过的那份计划"，取消这件事本身不改变计划内容，语义正确。

### 6c. 同 idempotency_key：running 时重放 / 跨用户

```
[ADVERSARIAL] same Idempotency-Key, different requested_by -> separate run 5c1d844e...; rows 2 -> 3
```

- key 摘要 = `digest([requested_by, trigger_type, idempotency_key])`（`jobs.py:240`），**含请求者盐**；
- 同用户重放（含执行中）→ 返回**同一个 run**，`submit()` 见 `status != "queued"` 直接 return，不会二次执行、不会二次入库；
- 跨用户同 key → **各自独立的 run**。这是安全收益（B 无法借 key 拿到 A 的 run），代价是"同一 key 跨身份不幂等"。属可接受设计，但**必须写进接口文档**，否则客户端换 token 重试会得到两条运行；
- 旧 run 已 `cancelled/expired` 时重放 → `JobError(code="not_resumable")` → 409，不把取消的运行当作结果返回。

### 6d. 去掉 `cancel_requested==0` 过滤后，被拒 commit 的 job 还能 heartbeat 吗

**不能。** 被拒 commit 后 `finish_cancelled` 把 `status=cancelled`、`owner_token=NULL`、`lease_until=NULL`；`heartbeat()` 的过滤条件是 `status=="running" AND owner_token==token AND lease_until>=now`，全部不满足 → 返回 `False`，租约没有被续期。另测：错 token → False；租约已过期 → False。判定 **PASS**。

## 7. 契约基线是否真手术式

```
contracts/baselines/database-schema-v1.json: +23 -0
contracts/baselines/openapi-v1.json:        +283 -0
新增 path 行数: 4
新增 Idempotency-Key 行: 2（既有 run 操作的 header 参数）
```

`+` 行里只有 4 个 path：`/api/v1/inspection/jobs`、`/{job_id}`、`/{job_id}/cancel`、`/{job_id}/retry`；schema 只有 `db_inspection_job` 的 2 列 + 1 索引。**零删除行**意味着没有任何既有 operation 被改写。

再用**实时 spec 与基线整体比对**（`scripts/check_backend_contract_baseline.py`）：

```
{"added_operations": [], "removed_operations": [], "changed_operations": [],
 "added_schemas": [], "removed_schemas": [], "changed_schemas": [], "document_changed": false}
database: {"added_tables": [], "removed_tables": [], "changed_tables": [], "document_changed": false}
```

即"当前代码生成的契约 == 已评审基线"，没有夹带他人漂移。判定 **PASS**。唯一措辞瑕疵：commit message 只说"四个 operation + 2 列 1 索引"，漏说了 `POST /inspection/{db_type}/{instance_id}/run` 新增的 `Idempotency-Key` header（实际改了，也进了基线）。

## 8. 门禁与全量

```
$ DATABASE_URL="sqlite:///./tmp_qa.db" PYTHONPATH=$PWD/backend .venv-mac-primary/bin/python \
  -m pytest backend/tests/ -q
2872 passed, 50 warnings, 708 subtests passed in 327.22s (0:05:27)

$ .venv-mac-primary/bin/python scripts/check_all.py --fast
=== backend contract baseline === {"ok": true, ... "document_changed": false}
=== git diff whitespace ===
All validation checks passed.       (exit=0)
```

新增用例数复核：5 个全新测试文件共 collect **83**，加上 3 个既有文件的增补 ≈ **84**，与"基线 2788 → 2872（+84）"自洽。临时库 `tmp_qa.db` 已删除。

## 9. 变异测试：声明的守卫是真是假（7/7 全被杀死）

每个变异体都从**逐字节备份**还原并用 sha256 校验一致（工作区确认干净），因此可以放心说这些不是"装饰性测试"：

| 变异体 | 结果 |
|---|---|
| M1 `_STATUS_OUTCOME["timed_out"] = "failed"` | `test_unverifiable_endings_are_unknown_not_failed` 红 |
| M2 `commit_results` 去掉 `cancel_requested == 0` 过滤 | `test_refused_commit_closes_the_job_as_cancelled...` 红 |
| M3 `_finalize_refused_commit` 不再 `finish_cancelled`（退回旧行为） | 同上红 |
| M4 `request_scope` 改用 `DBCheckResult` 解析 job 归属 | `test_own_zero_result_run_is_readable...`、`test_cancel_and_retry_are_control_not_read` 红 |
| M5 零结果 `evidence.state` 由 `missing` 改成 `recorded` | 2 个 readback 用例红 |
| M6 `_load_job_for_request` 去掉 403 | `test_handler_filters_the_list_by_scope` 红 |
| M7 `_requested_by()` 恒返回 `None` | 4 红（含 `test_manual_run_records_who_asked_for_it`） |

**M7 直接回答实现方自报偏差 ①**：手动入口记录 `requested_by` 这件事**有专门用例覆盖**（`test_manual_run_records_who_asked_for_it` + 3 条连带），"23 红未复现"只是当时的观察口径问题，不是覆盖缺口。

---

## 10. 缺陷与观察项

### FIND-QA-01（MEDIUM，**既有问题，非本批引入**）HEAD 无法从干净检出运行

```
$ git archive HEAD | tar -x -C /tmp/qa_clean && cd /tmp/qa_clean
$ PYTHONPATH=.../backend python -c "import app.migrations.runner"
ModuleNotFoundError: No module named 'app.migrations.locking'
$ python -c "import app.main"
ModuleNotFoundError: No module named 'app.services.ai_diagnosis_contract'
```

仓库存量：**183 个 `.py` 未被 git 跟踪**（`backend/app/services` 21、`migrations` 15、`models` 10、`schemas` 9 …）。`backend/app/migrations/` 只跟踪了 `runner.py` 和 3 个新 revision，`initialization.py`/`policy.py`/`schema_contract.py`/`locking.py`/`runtime_schema.py` 及 8 个旧 revision 全部缺失。

影响：**"2872 passed" 只在本工作区可复现，`git clone` + `checkout HEAD` 起不来**；CI、干净部署、他人复现全部失效。本批自己的新文件（`run_status.py`、`run_readback.py`、`request_scope.py`、4 个测试文件）都已入库，问题在外围。基线 `0a622f3` 更糟（`models/inspection_job.py` 也没入库，`jobs.py` 直接 import 不到），`3a59416` 顺手修好了其中一处。
建议：单独一个"入库补齐"提交（不要混进业务提交），否则本批的可交付性无法传递。

### FIND-QA-02（LOW，**源码回归，需工程师修**）历史结果的 `instance_name` 退化为数字 id

- 位置：`backend/app/services/inspection/result_writer.py:189`（`_task_summary(task_id, items, None)`）与 `:283`（`"instance_name": target["name"]`）
- 事实：`list_tasks` 不再查 `DBInstance`，改由 `report_target(rows)` 从 `raw_data["inspection_target_snapshot"]` 取名；本批之前写入的结果行**没有** snapshot → 回落成 `str(instance_id)`。
- 复现（自建 1 行无 snapshot 的 `db_check_result` + 同名 `DBInstance`）：

```
[REGRESSION] legacy row instance_name='101' snapshot_status='historical_context_missing'
期望 'mysql-prod-101'，实际 '101'
```

- 影响面：`GET /api/v1/inspection/tasks`、`/inspection/results` 相关列表、报告目录里**队列接入之前**的历史条目（`run_readback.legacy_summary` 的 `instance_name` 同源）。升级后老用户会看到"巡检报告 101"。
- 期望行为：snapshot 缺失时**回落查 `DBInstance.instance_name`**（一次批量查询即可，和旧逻辑等价），再把 snapshot 作为首选来源。0 行成本、无 N+1（本来就是按 instance_ids 批量取）。
- 说明：这条是"新读模型替换旧聚合"的副作用，不是实现方声明范围内的东西，但属于用户可见回归。

### FIND-QA-03（LOW，**边界源码问题，建议工程师修**）零结果 `success` 仍会给出 100 分/"normal"

```
success + 0 rows  -> score: 100  risk: normal   results_published: False
```

- 位置：`run_readback.py:206`（`score_for_counts(counts, task_status=outcome["outcome"])`）+ `result_writer.py:454`（只看 `task_status == "success"`）。
- 现象：同一个响应里 `results_published=False`、`evidence.state="missing"`，却 `score=100 / risk_level="normal"` —— 正是本批要消灭的"编造健康度"。
- 可达性：正常写入路径下 `execution_summary([])` 会给 `failed`，所以需要 `status=success` 且 0 行（历史行、外部写入、或未来某条路径）。**低概率但语义自相矛盾**。
- 期望：`score` 的门槛加一条 `result_count > 0`（或 `results_published`），否则 `None`。

### FIND-QA-04（LOW，一致性）同一个事实两个字段名

列表项用 `job_status`（`run_readback.py:220`），详情 `run` 块用 `status`（`jobs.job_view`，`jobs.py:157`）。本模块的立身之本是"一个问题一个答案"，两个读模型对**同一列**给了两个键名，前端必然要写两套取值。建议详情也补 `job_status`（保留 `status` 兼容），或统一。

### FIND-QA-05（MEDIUM，设计洞）取消后执行端存活即永不收敛

见 §6a，`jobs.py:354 / 616 / 248`。不阻塞本批（这是"取消只能请求、不能强制"的必然结果，且实现方在文案上诚实），但需要产品或架构拍板补一条兜底路径；否则"取消中"会成为新的永久态。

### O-1（观察，不修）受限账号读不存在的 run 返回 403 而非 404

中间件层 `request_scope` 对 `Missing inspection job` 一律 deny → 403；handler 层单独看会给 404。**fail-closed 方向正确、无存在性预言机**，但两层的语义不一致，文档里说清楚即可。

### O-2（观察，产品未拍板）`expired` 对外可见

`expired` 进入 `run_status.OUTCOMES`、`TERMINAL`、`RETRYABLE`，并由新注册的 `run_due_inspection_job_expiry` 真实产生（`master_scheduler.due_job_functions` 已注册两个 reaper，`inspection_scheduler.py` 两个 consumer）。技术上自洽（未知即 unknown、过期即可重试），但**产品未确认是否对外叫"已过期"**；同时前端未动、没有任何 UI 消费 `/inspection/jobs`，所以这个新词目前只出现在 API 里。

### O-3（观察）进行中运行的 `risk_level="warning"`

`run_readback.py:233` → `risk_level_for(counts, None)` 对 `queued/running` 也返回 `"warning"`（因为 score 为 None）。列表会把"正在跑"渲染成"需关注风险等级"。建议进行中给 `None`/`unknown`。

### O-4（观察）取消请求在"执行端失联"时的最终态不体现取消

`reap_interrupted` 不看 `cancel_requested`：一条已请求取消的 running 在租约过期后被记为 `interrupted`（`outcome=unknown`），取消事实只留在 `cancel_requested*` 字段里。§6a 的探针实测到 `→ 'interrupted'`。语义上"我们不知道目标端做了什么"是诚实的，但操作员会问"我明明取消了"。可选改进：`interrupted` 时若 `cancel_requested=1`，把 `failure_reason` 里带上取消背景。

---

## 11. 对实现方自报三个偏差的判断

| 偏差 | 我的独立判断 |
|---|---|
| "手动入口不传 `requested_by` → 23 红未复现，实际由其它用例覆盖" | **接受**。变异体 M7 证明有 4 条用例守护（含专门的 `test_manual_run_records_who_asked_for_it`）。"23 红"只是当时的观察数字，不构成覆盖缺口。 |
| `report_builder.build_report_payload` 没加 `include_job` 形参（认为没人用） | **接受**。全仓 `grep include_job` 命中 0，`build_run_read` 调用它只用 3 个位置参数；不引入未使用形参是正确取舍。 |
| `expired` 现在对外可见终态，产品未拍板 | **技术可接受，流程需补**。映射与回收都自洽且被测；但这是**对外词汇表变更**，见 O-2：需要产品明确"已过期"这个词，且前端接线前不要在 UI 层猜。 |

---

## 12. 未覆盖 / 残余风险

1. **前端未接线**：`/inspection/jobs`（GET/GET one/cancel/retry）目前无任何前端调用（`grep -r "inspection/jobs" frontend/src` 为空）。本批修的是"后端唯一真相"，用户可见的"未知不再显示成功"要等 UI 改造；在那之前旧页面仍在按 `status` 猜。
2. **MySQL/PostgreSQL 上的 DDL 未实测**：只验证了 sqlite（`ALTER TABLE ... ADD COLUMN ... NULL` / `CREATE INDEX` 在三方言都合法，但 `v20261020_01` 的 `TIMESTAMP` 分支与 `ix_..._created_at` 在 MySQL 的重复索引检测只有静态代码审查）。建议发布前在真 MySQL 上跑一次升级演练。
3. **`heartbeat`/`lease_guard` 的线程行为**只用同步调用模拟，未在真实多线程 + 多进程竞争下压测（`claim` 的 `UPDATE ... WHERE` 乐观锁路径依赖数据库行锁语义）。
4. **`expired` 与 `cancelled` 的报表侧**：`report.py` 目录会把非 success 的 `score` 置 None、`export_url` 置 None，但**导出端**（`/reports/inspections/{instance_id}/{task_id}/export-word`）对失败运行的行为未测。
5. 未做性能基线：`list_jobs` 的 `outcomes=["unknown"]` 用了 `status NOT IN (...)`（无法走索引），大表下需关注；`run_sources` 一次取 100 条 job 后逐个 `run_summary`（内部有批量 count，但 `_template_changed_since_freeze` 与 `reports_block` 是**每行一次查询**，N+1 风险与旧实现同形）。

---

## 13. 路由判定

- **FIND-QA-02 / FIND-QA-03 → 工程师（Alex）修**（源码 Bug，两处都很小；我未改业务代码，遵守"发现源码 Bug 不改业务代码"约定）。
- **FIND-QA-05 + O-2 → 架构师/产品拍板**（设计缺口与对外词汇表），不阻塞本批。
- **FIND-QA-01 → 团队流程**（补齐入库），建议单独立提交。
- 测试本身的问题只有一处（我自己的断言把 `list_jobs` 的 `("plan_only")` 空结果写错、以及"租约过期"用了过去时间戳），已自行修正，不涉及产品代码。
- **本批结论：通过（PASS，带 2 个 LOW 待修项 + 1 个 MEDIUM 设计洞待决策）。** 声明中的核心可证伪命题（老库能升级、集合断言有效、未知不回落成功、零结果运行可见且归属正确、取消不谎报、契约手术式、全量 2872）**全部独立复现为真**。

---

## 附录：复现方式

临时脚本不在仓库内（`/tmp/qa_batch1/`，可重跑）：

- `qa1_migration.py` —— §1 老库升级 + 负向对照（手写旧 DDL 搬迁）
- `qa3_run_status.py` —— §3 86 条 run_status 断言
- `test_qa456.py` —— §4/§5/§6，16 条用例（真实 `AuthMiddleware` + 真实 router）
- `mutate.sh` —— §9 六个变异体，逐字节备份 + sha256 校验还原

关键命令：

```bash
# 老库升级（独立临时库，跑完删除）
DATABASE_URL="sqlite:////tmp/qa_batch1/upgrade.db" TRACEMIND_AUTO_MIGRATE=1 \
  PYTHONPATH=$PWD/backend .venv-mac-primary/bin/python /tmp/qa_batch1/qa1_migration.py

# 权限/取消/对抗
DATABASE_URL="sqlite:////tmp/qa_batch1/http.db" PYTHONPATH=$PWD/backend \
  .venv-mac-primary/bin/python -m pytest /tmp/qa_batch1/test_qa456.py -q -s

# 变异
bash /tmp/qa_batch1/mutate.sh

# 契约基线（实时 spec 对基线）
PYTHONPATH=$PWD/backend .venv-mac-primary/bin/python scripts/check_backend_contract_baseline.py

# 干净检出可用性
git archive HEAD | tar -x -C /tmp/qa_clean && cd /tmp/qa_clean && \
  PYTHONPATH=/tmp/qa_clean/backend <venv>/bin/python -c "import app.main"
```

本次验收后工作区状态：探针 revision 已删除、所有变异文件 sha256 与 HEAD 一致、`tmp_qa.db` 与临时库已清理、未执行任何 `git add`、未触碰 `.workbuddy/`。
