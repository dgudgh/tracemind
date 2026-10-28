# C05 Inspection Plan Adapter

Date: 2026-10-20. Scope: C05/U05 adapter only, not C01-C16 acceptance.

## Authorized Blocker Repair Design

The follow-up explicitly extends this task's ownership to `runner.py`, `jobs.py`,
and new focused tests. No plan service, worker queue, API, release flag, or domain
status vocabulary is changed here.

- Keep `prepare_findings`' two-value return interface, returning a list-compatible
  `PreparedFindings` that carries trusted in-memory staged slow-SQL write commands.
  A scoped ContextVar makes existing native collectors stage automatically during
  preparation; their direct legacy helper calls retain existing default behavior.
- At guarded publication, materialize a copy using the same slow-SQL writers and
  update the copied persistence finding with actual count/status/record IDs.
  Original staged findings never acquire IDs from a transaction that might roll back.
- Both `jobs.commit_results` and legacy `run_inspection` consume the same staged
  interface. Slow SQL and check-result rows commit together; report failure rolls
  both back. Add Oracle's missing real SQLite outer BEGIN before its savepoint.
- Optional `execution_guard(db)` keywords preserve old callers. Guard normal
  publication and terminal error/cancel commits, including after internal rollback.
  BaseException ownership loss unwinds without being converted into domain failure.
- End the collector read transaction before fresh target/actor/plan validation.
  Keep filesystem validation and collector I/O outside parent publication locks.

The previous handoff had 187 passing isolated tests and two open source blockers.
The authorized follow-up implements both in runner/jobs; current evidence below
supersedes that blocker status without claiming field or C01-C16 acceptance.

## Small Design

- `InspectionPlanParams` is strict and forbids extra keys. Public creation input is
  `policy_id > 0`, `timeout_seconds = 900` (30..3600). The service, not the caller,
  produces `policy_revision`, `runtime_digest`, and `target_fingerprint`.
- `prepare_params(db, instance, params)` reads the platform DB and local runtime
  only. It never connects to a target, seeds templates, or chooses a default.
  It rejects missing/disabled/inapplicable/unexecutable templates and missing scripts.
- `validate_params` requires all three frozen hashes and compares fresh state.
  An edit means refusal, not automatic revision replacement. The parent must bind
  its approval to the entire returned parameter snapshot and plan revision.
- `execute(ctx)` requires an authenticated actor, a target, and a nonempty
  `execution_id` in `planid:seq:revision` form. The queue's existing unique key
  implements deduplication. A replay reads the existing run, never recollects.
- Reuse `jobs.enqueue/claim/lease_guard`, `runner.prepare_findings(frozen=True)`,
  `jobs.commit_results`, and the shared `run_readback` summary. No collector or result
  writer is introduced. Guards and fresh authorization run before collection and
  before publication, not only after the adapter returns.
- A guard is an optional callback `guard(db)` (also accepts a no-argument wrapper):
  raise or return `False` to refuse; `None` means accepted. Calls occur before enqueue, immediately before claim,
  before collection, and immediately before `commit_results`. It must check/renew
  the parent's execution ownership. Ordinary terminal error writes also need a
  guard after rollback; guard refusals themselves unwind without publishing a
  child success/failure verdict.
- Output: `schema=inspection.plan_result/v1`, `job_id`, `instance_id`, bounded
  authoritative run summary, report IDs, and the queue's actual status/outcome.
  No full run envelope, findings, SQL, Markdown, progress, URI, or arbitrary path
  is copied into a plan. Failure, partial, running, and unknown are not success.

## Integration Ownership

The parent owns `CapabilityContext.execution_id/execution_guard`, declarations,
registry, `plan_service`, approvals, and API integration. This module is a write
adapter, not an ordinary `registry.execute_read` handler. The only existing source
files edited by the authorized follow-up are runner/jobs. Registration must reject collection via execute_read,
freeze at create time, and validate each step immediately before execution.
The parent intends to wire static server capability `inspection.run.execute` v2,
behind an explicit release flag that defaults off. That registration/flag, request
idempotency-before-prepare ordering, approvals and revisions are parent-owned and
are not claimed as completed by this adapter.

## Freeze Boundaries

`jobs.freeze_policy` supplies the real content/update-time revision. Queue revision
also includes the target display snapshot and is intentionally a different hash.
Runtime binding covers the actual local script bytes plus SQL-producing runtime
modules; shell references alone do not pin the Python SQL implementation. Both
on-disk source and loaded bytecode/SQL constant maps are hashed. The latter uses
stable code fields, not `marshal.dumps(code)` (interning can change its bytes on
first execution). Full SQL remains only in the existing template/job evidence,
never in capability-plan parameters. Digests are deployment/Python-runtime
specific, not portable release signatures.

Oracle `oracle_profile=auto` is supported without any pre-approval target probe:
the frozen conditional contract includes the existing version/CDB dispatch code
and every candidate script in the controlled catalog (`10g`, `noncdb`, `cdb`),
each pinned by its actual content hash. A change to even an unselected candidate
invalidates the frozen runtime. Execution retains the existing profile detection
and unknown/mismatch refusal rules. The existing saved finding/report metadata
records the actual resolved `oracle_profile`, script name, and script hash; the
bounded plan result links to that authoritative job instead of copying SQL.
Explicit profiles pin their selected script. Host plans are outside this
four-database adapter.

Before/after runtime digest checks detect ordinary concurrent edits, but do not
provide filesystem locking or proof against edit-and-restore (ABA) during a read.
Immutable deployed source/scripts remain a release prerequisite. No claim of
four-engine field validation, full dependency attestation, or arbitrary hot-reload
safety follows from these hashes.

Queue deduplication is inherited unchanged: its unique key hashes
`[requested_by, capability_plan, execution_id]`. The parent must keep `actor_id`
bound to the original approved requesting principal through retries/takeover;
changing that principal requires a new plan revision. The adapter stores the
complete frozen-parameter digest in the real `plan_bound` progress event and
rejects reuse with a different runtime hash, even if template/timeout are unchanged.

The parent guard must provide a publication fence, ideally checking/locking its
execution row on this same DB session until `commit_results` commits. A check in
a separate session does not prove atomicity against takeover in the final
check-to-commit interval. These adapter tests prove refusal for a failed guard
before publication and a lost child-job owner, not cross-process parent/child
linearizability. Parent ownership integration and its concurrent acceptance remain
required; a guard only after return is insufficient.

Call `validate_params` before taking parent publication locks: it reads platform
metadata and hashes local files. The adapter also validates before guard locks,
commits the pre-collection ownership check to release its lock before collector
I/O, then revalidates before acquiring the final publication fence. The last guard
and `jobs.commit_results` share one session/transaction with no adapter commit or
filesystem validation between them. Callers must not invoke the whole adapter
while already holding a parent row lock across its validation/collection I/O.

After collector I/O the adapter ends the old read snapshot with rollback, refreshes
the actual job, then rereads template/target/authorization/runtime before the final
guard. This is necessary on MySQL REPEATABLE READ, not merely an identity-map
refresh. Default template slow-SQL writes now remain staged across that rollback.

## Implemented Staged Publication

`prepare_findings` still returns `(target, findings)`. Its findings list is now a
`PreparedFindings` subclass with `staged_writes`: captured engine/instance ID,
copied source metrics/source keys, and the associated persistence finding. There
are no ORM writes or allocated slow-SQL IDs during native collection through
prepare. The provisional finding has `status=pending`, `count=0`, `record_ids=[]`;
it is internal, not a published health or persistence success.

`runner.materialize_staged_writes(db, findings, instance_id=...)` copies findings,
calls the same `_save_mysql_slow_sql`/`_save_oracle_slow_sql` writers inside the
caller transaction, and attaches their actual status/count/IDs/errors while
preserving existing template metadata. It does not commit. Original staged
findings remain unchanged, so retry after a result-insert rollback cannot retain
ghost IDs. Oracle ranking deduplication, successful empty results, partial/error
semantics and numeric validation remain in the existing writers.

Both `jobs.commit_results` and legacy `runner.run_inspection` use this API. In the
job path the conditional child-token/lease/cancel check and target validation run
before materialization; auxiliary rows, check results, and the terminal job state
then commit atomically. A result insert/commit failure rolls back both data sets.
`run_inspection` also rolls back on failure, but gains no invented plan approval
or ownership protocol. Direct private collector/helper calls retain their prior
default persistence behavior for compatibility; application job execution goes
through prepare and is staged automatically using a scoped/reset ContextVar.

Oracle's slow-SQL writer now has the same SQLite real outer-BEGIN protection as
MySQL before opening a savepoint. Existing transactions are not restarted, no
helper commits the caller transaction, and a SELECT-only caller can roll back all
new rows. The adapter's unexpected-pending-writes check remains defensive for
future collectors; it no longer blocks the native default slow-SQL path.

## Implemented Guard Wiring

`submit`, `retry`, `execute`, `commit_results`, `_finalize_refused_commit`,
`finish_error`, and `finish_cancelled` accept keyword-only
`execution_guard=None`. Existing positional/default interfaces remain valid.
`submit`/`retry` forward the guard and check before claim. `execute` checks before
collection, releases the pre-I/O lock, then rolls back the collector read snapshot
and refreshes the job/target/actor before calling guarded `commit_results`.

`commit_results` checks the guard in the same session before its write lock. If it
rolls back for cancellation or timeout, the subsequent `finish_cancelled` or
`finish_error` checks that guard again before terminal DML/commit. Generic result
publication errors roll back; `execute` reacquires the guard before recording a
domain failure. The adapter forwards its normalized guard to both publication and
failure APIs, including those internal branches. No new collector or result
writer, no independent slow-SQL commit, and no global before-commit hook is used.

False refuses with `JobError.code=execution_guard_rejected` in jobs, or the
adapter's `inspection_execution_guard_rejected` wrapper code. Refusals are not
converted into authoritative domain failures. `ExecutionLeaseLost(BaseException)`
unwinds; publication cleanup rolls back local data without swallowing that signal.
Parent row locking and worker reconciliation remain parent-owned. Domain status
and outcome stay `success`, not `succeeded`; no old domain vocabulary was changed.

## Stable Error Codes

Adapter refusals use `InspectionPlanError.code` (a `jobs.JobError` subclass):

- Input: `inspection_params_invalid`, `inspection_frozen_fields_forbidden`,
  `inspection_params_not_frozen`, `inspection_execution_id_required`.
- Configuration: `inspection_engine_unsupported`,
  `inspection_policy_not_applicable`, `inspection_template_not_executable`,
  `inspection_runtime_unavailable`.
- Drift: `inspection_policy_changed`, `inspection_runtime_changed`, `target_changed`.
- Control: `inspection_execution_guard_rejected`, `inspection_job_ownership_lost`,
  `inspection_idempotency_conflict`, `inspection_enqueue_refused`,
  `inspection_execution_failed`, `inspection_collector_pending_writes`.
- Existing queue codes are retained, including `actor_inactive`,
  `permission_revoked`, `scope_revoked`, `instance_disabled`, `template_missing`,
  `snapshot_invalid`, `timeout`, and cancellation codes.

Initial validation refusals raise before target I/O; ordinary errors after claim finish the
real job without publishing findings. A child ownership/lease refusal leaves the
actual current job state intact. Consumers must inspect `run.terminal`,
`status`, and `outcome`, not treat any returned dictionary as success. `report`
only references an actually persisted ReportArtifact. `available=false` means no
archive reference, not that a rule report cannot be reconstructed from the job's
stored evidence. `available=true` means an artifact identity exists; its actual
status/archive_state remain explicit and do not promise downloadability or health.

## Final Result Contract

```json
{
  "schema": "inspection.plan_result/v1",
  "job_id": "actual-job-id",
  "instance_id": 1,
  "status": "success",
  "outcome": "success",
  "error_code": null,
  "run": {"job_id": "actual-job-id", "terminal": true, "result_count": 1},
  "report": {"available": false, "state": "not_synced", "artifacts": []}
}
```

`run` is a whitelist from `run_readback.run_summary` (the same source as the
run-detail block, without progress or failure text): `job_id`, `instance_id`,
`db_type`, `status`, `stage`, `attempt`, `trigger_type`, `policy_revision`,
`timeout_seconds`, `created_at`, `started_at`, `finished_at`, `terminal`, `outcome`,
`outcome_kind`, `requires_attention`, `results_published`, `cancel_requested`,
`can_retry`, and `result_count`. String metadata is capped at 128 characters.
Top-level `error_code` is capped at 128 characters. Job IDs are existing generated
30-character queue IDs, never fabricated report IDs or paths.

When synced, `report.artifacts` contains at most one actual
`{id: int, status: str, archive_state: str}`; the two state strings are capped at
30 characters. It is fetched with a projected metadata-only query, never loading
artifact Markdown/HTML/JSON or export/archive URIs. `report.state` is exactly
`synced` or `not_synced`. Full evidence/report retrieval uses the existing
authorized job/report APIs separately. The adapter tests retain full authoritative
readback checks by job ID without embedding that readback in a plan.

## Evidence And Remaining Work

Final follow-up isolated run on 2026-10-20: **604 passed, 74 subtests passed in
68.55s**, using the command below. This includes **69 adapter tests** and
**44 staged-publication tests** in `test_inspection_staged_publication.py`.
Earlier 187/17 handoff and intermediate runs are historical, not summed into this
result. Whitespace diff checks passed. The two authorized existing source files
are modified; adapter, two test files and this document are untracked. No commit
or push was made.

The tests use disposable SQLite metadata and real enqueue/claim/commit_results,
result normalization, rule report generation, and run_readback. Explicit fixtures
replace engine SQL/health sources and the Oracle profile connection/detection
probe. The staged-publication tests use actual native Oracle/MySQL collector
orchestration, preparation, slow-SQL writers, ORM inserts, queue publication and
report readback, not a replacement persistence implementation. Socket connections
are forbidden, and the lease renewal factory points only to disposable SQLite.
Default-template construction uses the actual built-in declarations; execution
seeding is disabled in these fixtures. Adapter prepare/validate assert SELECT-only
behavior, no seeding, and no target probe.

New data/ownership evidence covers:

- Populated native default templates and direct job execute, submit/replay,
  capability adapter, and legacy run_inspection all publish real slow-SQL IDs.
- Three Oracle rankings deduplicate to two actual rows; saved finding counts/IDs
  match the table. Empty success and writer failure/partial reports remain honest.
- Result insertion failure and BaseException during insert/final commit roll
  back both slow-SQL/check-result tables; original staged findings retain no IDs.
- SELECT-only Oracle and MySQL savepoints roll back completely. Existing SQLite
  transactions and caller changes are preserved by both private writers.
- Cancellation/timeout recheck the same-session guard after internal rollback.
  A lost parent lease leaves no new data and does not manufacture terminal failure.
- Failure publication after a result-write rollback reacquires the guard; retry
  forwards it, keeps the failed parent, and publishes IDs only for the child.
- Mapper insert events verify the publication fence covers auxiliary/result
  writes until root commit. Actor revocation during collection prevents both.
- Existing adapter evidence still covers four-engine isolation, script/template
  drift, Oracle auto dispatch, lost child ownership, duplicates, and bounded output.

Pending: four-engine real SQL/driver/account/profile agreement, Oracle profile
field behavior, **real MySQL locking/authorization races**, actual
queue-worker/approval/API integration, immutable release enforcement, restart/HA and load/168-hour
tests. Existing `resolve_execution_policy` still calls default seeding at actual
execution time; this work does not alter that legacy behavior, and does not claim
the entire existing engine is side-effect-free on the platform DB.

The worker's `ExecutionLeaseLost(BaseException)` deliberately unwinds through this
adapter without `finish_error` or result publication. Its reconciliation belongs
to the outer worker. Ordinary callback exceptions/False have the stable adapter
refusal code. Callable signatures are bound before invoking the guard; an internal
TypeError is never retried with a different calling convention.

## Verification Command

```sh
env DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false \
  PYTHONDONTWRITEBYTECODE=1 .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider \
  backend/tests/test_inspection*.py \
  backend/tests/test_custom_inspection_metrics.py \
  backend/tests/test_oracle_inspection_capability.py
```
No target DB, model, Agent, deployment, commit, or push is authorized by this task.

This file is also the pending knowledge-base summary: the designated Obsidian
vault is outside the writable roots. No vault write is claimed. Related historical
note: `10-Projects/mesdbtools/功能卡片与可选AI编排`; source-of-truth is current code
and this round's tests, not that note's previous completion statements.

## Confirmed Cancellation Handoff (2026-10-20)

This follow-up changes only the authorized cancellation branches in
`plan_service.py`, adds `test_inspection_plan_cancellation.py`, and updates this
handoff. It does not edit the queue, approval/publication guard, adapter, collectors,
job writers, API, schema or release flag. The main agent's nested-transaction reset
fix remains intact. The queue author has been explicitly notified of the matching
evidence-reading contract below.

`inspection.plan_result/v1` keeps the existing domain vocabulary. A returned
`cancelled` envelope can settle a started plan only after
`validate_effect_reference` checks its real, revision-bound inspection job:

- Matching requester, instance, execution idempotency key and frozen policy, plus
  the existing terminal status/outcome/finished-at checks.
- `status=stage=cancelled`, `cancel_requested=1`, a recorded request timestamp,
  and cleared `owner_token` and `lease_until`.
- The last terminal progress event is explicitly `cancelled_before_run` or
  `cancelled_during_run` and has stage `cancelled`. The envelope's `error_code`
  must match that event. JSON event timestamps are not compared byte-for-byte
  with SQL DateTime values: MySQL DATETIME may discard fractional seconds.
- No `completed` publication event and no `DBCheckResult` rows for this job.
  The absence check is per job, not per instance: earlier steps' real results
  must remain available.

No message parsing or inferred legacy cancellation substitutes for this proof.
`cancelled_force_closed`, lost ownership, missing request/acknowledgement, or
contradictory persisted publication cannot become a confirmed plan cancellation.
Invalid cancellation references raise stable `result_unknown`; the execution
loop records an unknown step and the durable queue retains reconciliation rather
than automatically replaying it. An earlier successful step can still make the
aggregate plan partial, but an unknown step continues to block reconciliation.

For a newly confirmed cancellation, only the current step is `cancelled`; the
remaining steps are `skipped` without invoking their handlers. With no earlier
product, a started plan settles `cancelled` only when its first step has a real
inspection cancellation reference and all later steps are skipped. With an earlier
success, the plan settles `partial`, retaining both the successful and cancelled
job references. `settle` rechecks both products and cancellation proof. It does
not claim earlier business writes were rolled back. The old never-started
`cancel_plan` behavior remains valid without inventing an inspection job.

Queue integration ownership remains with the queue author: `_evidence` must
require an `inspection_job` reference and call `validate_effect_reference` for
every cancelled step of a started plan, including the missing-reference case.
Started cancelled-plan archive acceptance must also validate actual plan/step
terminal evidence, not use a bare `outcome == cancelled` fallback. Unknown or
force-closed evidence continues to occupy its target. Batch `cancel_requested`
only means delivery cancellation was requested; it never proves a job stopped.

The focused tests use the existing real SQLite queue/result/readback fixtures.
Native populated MySQL/Oracle collection fixtures exercise real cancellation and
slow-SQL publication; four-engine fixtures additionally exercise three-step
plans, stopping at the first or second step and preserving rule report payloads.
Collectors and Oracle probes are explicit fixtures; socket connections are denied.
Direct job cancellation, owner acknowledgement, both reapers, typed references,
settlement and subsequent target claim are the production implementations.
Malformed-proof tests deliberately mutate isolated metadata, never a live target.

Final frozen focused evidence: **56 passed in 68.60s**, exit 0, on the source/test
and queue snapshots below. This includes same-target release after confirmed
cancellation, continued exclusion after unknown cancellation, missing/wrong step
references, missing aggregate references, settlement refusal, native slow-SQL IDs
preserved after a later cancellation, and MySQL DATETIME second-precision
compatibility (simulated on isolated SQLite, not a live MySQL claim).

- `plan_service.py` SHA256:
  `32bbd6f787e346971dfaf78c47419398b2831f027a429dd8adfee9857bc2a2a3`.
- `test_inspection_plan_cancellation.py` SHA256:
  `0222ee4923d87988b5f68d93215e6204414fbeb64958e5abbb7cbbf78990f56a`.
- Independently authored `plan_queue.py` SHA256:
  `089350846bf0ecee053c0006b4e73864334fdf46324982c115d47b95552bdfc7`.
- Log: `/tmp/c05-cancellation-frozen-20261020.log`.
- JUnit: `/tmp/c05-cancellation-frozen-20261020.xml`.

Earlier runs remain historical: initial focused 44 passed in 34.39s; corrected
expanded focused 50 passed in 77.49s; native preservation additions 2 passed in
5.61s. The first nine-file regression below naturally finished **343 passed,
6 failed in 266.87s**. All six failures were the old test module's invalid fixture
transition (setting a settled plan to running without clearing its outcome), which
hit the existing model check constraint before the intended settlement check.
The process was allowed to finish, not restarted due to observation timeout.
That fixture was corrected without relaxing the schema or business checks. Do not
label that historical nine-file run all green, or sum these overlapping counts.

The queue author separately tightened `_evidence` to require real cancelled-step
proof even when a reference is missing, plus exact aggregate references and
first-cancelled/rest-skipped semantics for started cancelled plans. The final
56-case run above uses that implementation. The author is additionally running
the independent queue/workflow/cancellation/schema suite against the frozen
source; its outcome is not claimed here until reported.

Final focused command:

```sh
env DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests \
  .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider --tb=short \
  backend/tests/test_inspection_plan_cancellation.py \
  --basetemp=/tmp/c05-cancellation-frozen-20261020 \
  --junitxml=/tmp/c05-cancellation-frozen-20261020.xml
```

Historical nine-file regression command (with the six fixture failures above):

```sh
env DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests \
  .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider --tb=short \
  backend/tests/test_inspection_plan_cancellation.py \
  backend/tests/test_capability_plan.py backend/tests/test_plan_validation.py \
  backend/tests/test_plan_governance.py backend/tests/test_plan_queue.py \
  backend/tests/test_inspection_plan_workflow.py \
  backend/tests/test_inspection_plan_publication.py \
  backend/tests/test_inspection_plan_adapter.py \
  backend/tests/test_inspection_staged_publication.py
```

No field acceptance is claimed: actual MySQL repeatable-read/permission races,
real engine cancellation/driver behavior and four-engine field evidence remain
outstanding. This fix does not address the separately reviewed lock-held runtime
file validation or C08 report-index synchronization. Report payload existence and
`report.state=not_synced` remain distinct facts. The actual queue currently uses
`ExecutionLeaseLost(RuntimeError)` plus the same-session publication fence;
the earlier BaseException statement above describes the isolated unwind contract,
not the current queue exception's inheritance. No network, deployment, commit or
push was performed. The vault remains outside this task's authorized write set;
this section is also its pending archival summary.

### Uncertain Run Reference Follow-Up

The preceding `32bbd6f7`/56-test freeze is historical. The queue author reported
the matching four-file suite naturally completed **155 passed in 143.30s** on
that snapshot. A subsequent user-requested review found an association gap:
requiring cancellation acknowledgement merely because `job.status=cancelled`
made a real force-close lose its plan-to-job reference. A new assertion against
the actual MySQL fixture/reaper reproduced `step.result_ref is None` (1 failed,
48 deselected, 1.78s); the equivalent Oracle path is covered by the final matrix.

The follow-up keeps the domain vocabulary unchanged. At this source revision,
`reap_overdue` writes status `cancelled` with `cancelled_force_closed`, and the
domain read model still derives outcome `cancelled`; `reap_interrupted` writes
`interrupted` and derives `unknown`. Neither represents owner-confirmed stopping.
The plan maps a `cancelled_force_closed` cancellation to an **unknown step** while
retaining the raw, truthful domain envelope and authoritative job ID. It does not
rewrite the domain payload to a different outcome.

Strict acknowledgement/zero-publication proof now applies only when the plan or
step actually declares **confirmed cancellation**. An unknown step's reference
still passes all existing real-job/requester/instance/execution-id/frozen-policy
checks, including terminal completion and actual status/outcome agreement; its
`error_code` must additionally match the job's actual terminal failure code.
Thus a genuine domain unknown can remain readable without being required to
prove cancellation acknowledgement. An unconfirmed still-running child cannot
be substituted for terminal evidence. A malformed binding or false code is still
`result_unknown`, not a trusted reference.

Queue `_evidence` remains unchanged in this follow-up. An unknown step never
archives, releases the target or automatically replays, even when the aggregate
plan is partial because an earlier step genuinely succeeded. New four-engine
three-step tests retain both successful and force-closed job references, retain
the first job's real results/rule report, skip the last step, and still require
`reconciliation_required`. Existing real-reaper tests now assert retained job IDs,
correct raw outcome/failure code, rejection of a forged failure code, continued
same-target exclusion and no replay. The focused before/after association check
improved from the failure above to **8 passed, 48 deselected in 5.84s**.

Final follow-up source/test freeze:

- `plan_service.py`:
  `9f974a9651be5dfa065758e1e214bda0e2c4c32f9152f3d8063e38f4cf0eb315`.
- `test_inspection_plan_cancellation.py` (60 cases):
  `218b277eb5d85e52faea3235bb74033f4b3e3f5aef6223c2f3445e8ef876a4a1`.
- Independently maintained queue remains:
  `089350846bf0ecee053c0006b4e73864334fdf46324982c115d47b95552bdfc7`.

The final four-file run naturally completed **159 passed in 128.21s**, exit 0:
queue 67, workflow 8, cancellation 60, control schema 24. This is the authoritative
follow-up result for the final hashes above, not an extrapolation from the earlier
56/155-case freeze. No process was restarted because an observation wait yielded.
Log: `/tmp/c05-cancellation-linked-regression-20261020.log`;
JUnit: `/tmp/c05-cancellation-linked-regression-20261020.xml`.

```sh
env DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests \
  .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider --tb=short \
  backend/tests/test_plan_queue.py backend/tests/test_inspection_plan_workflow.py \
  backend/tests/test_inspection_plan_cancellation.py \
  backend/tests/test_capability_control_schema.py \
  --basetemp=/tmp/c05-cancellation-linked-regression-20261020 \
  --junitxml=/tmp/c05-cancellation-linked-regression-20261020.xml
```

The authorized write set remains the same three files. Publication guard,
governance, domain status normalization, adapter and queue were not edited by this
follow-up. Field/driver/MySQL locking validation remains outstanding; no network,
real target, model, Agent, deployment, commit or push was used.

## C08 Atomic Report Index Publication

This follow-up's write set is `inspection/jobs.py`, `inspection/plan_adapter.py`,
`report_center.py`, the inspection-source mapping extraction in `api/v1/report.py`,
new `inspection/report_artifacts.py`, new
`test_inspection_report_atomic_publication.py`, and this document. It does not edit
plan service/queue, assistant APIs, models, migrations, source collectors, report
builders or authorization policy. C07 and receipt/frontend integration stay with
their respective owners. The plan inspection release flag remains unchanged.

### Contract

- `sync_source_artifacts(..., commit=True)` retains its old default. `commit=False`
  flushes changed source-index rows to obtain real IDs, but never commits or opens
  another Session. Renderers, signing, retention, archive/legal-hold behavior,
  instance/tenant/organization metadata and source-key idempotency are unchanged.
- `jobs.commit_results(..., sync_report_artifact=False)` retains the old ordinary
  job default and guard callback protocol. The plan adapter explicitly passes
  `True`; other callers may opt in at this same existing publication boundary.
  This is not a claim that every legacy inspection entry now eagerly indexes.
- The opted-in transaction uses the original slow-SQL and result writers, flushes
  results and the real job's terminal state, then indexes exactly one current job.
  A refresh reads back actual SQL timestamp precision before building the source
  summary. The shared `source_item` is extracted verbatim from the original unified
  catalogue mapping; it is also used by the API. It does not generate another
  inspection report or copy findings/Markdown into the plan.
- A completed job with published results and actual `success`, `partial` or
  `failed` outcome gets a signed source index with the same outcome and a real
  `source:inspection:{job_id}` binding. The job's terminal event gets
  `report_artifact_id`, flushed in the same transaction. The final execution guard
  runs before the sole root commit; the durable worker's before-commit fence also
  remains in force. New or updated artifact, job verdict/reference, slow SQL and
  results therefore publish together, not via an adapter-tail flush that the plan
  service's subsequent rollback could erase.
- Report render/sign/index failures roll back the entire publication and raise
  the stable producer code `inspection_report_index_failed`. The existing caller
  may then write a guarded failed job with no new results or artifact. It cannot
  turn the failure into business success. A guard refusal/lost owner does not get
  permission to publish even this secondary failure. Direct `commit_results`
  callers receive the exception and retain their previously committed run intent.
- Cancelled, interrupted, timed-out or otherwise unknown jobs without a normal
  published report do not get an artifact from this path. Their real cancellation
  or uncertainty evidence and target-holding behavior remain unchanged. The old
  catalogue can still describe runs with no report; describing a run is not proof
  of a normal report. Historical completed jobs are not silently backfilled by
  adapter replay. New plan success returns the committed artifact's ID/status/
  archive state only; no arbitrary path, URL, body or private archive URI is added.

### Evidence And Integration Boundaries

New tests use disposable SQLite, real queue/job/result/slow-SQL writers, real
report readback, signing, serializers, renderers and export handlers. SQL/health
and Oracle detection are explicit fixtures; socket connections are prohibited.
The new cases cover:

- Native MySQL/Oracle populated evidence, actual slow-SQL IDs, matching rule
  reports, one real artifact ID, real job terminal ID reference, idempotent replay,
  metadata/retention/signature, and bounded plan results.
- Cross-Session reads after the artifact was flushed still see no new slow SQL,
  results or artifact, and still see the old running job until the root commit.
- Render, signature, insert and post-artifact-flush exceptions remove all new
  data, including a previously allocated artifact ID; failed jobs carry the
  explicit report-index failure code, not a success verdict.
- Last-moment guard refusal and the real queue ownership fence prevent publishing
  both the result and an invented failure after the artifact ID was allocated.
- Existing source indexes reuse their ID, preserve archive/retention state and
  signed historical metadata, and restore the old index if its update rolls back.
- Four-engine success/partial/failed indexes agree with authoritative outcomes;
  four-engine cancellation/force-close/interruption/timeout without results create
  no normal artifact. Legacy default jobs and default source-sync commits remain
  compatible; explicit `commit=False` remains caller-owned and rollback-safe.
- Real AuthMiddleware with real action grants and DB asset scope protects the
  artifact detail and Markdown/HTML/JSON/Word download routes. Source-report
  detail/Markdown/HTML/full Word exports are tested separately, not replaced with
  a fake report or an unrestricted user.

**Confirmed pre-existing scoped source-report blocker, outside this write set:**
`request_scope._rule` does not register the legacy `/reports/{task_id}/detail`,
`/reports/{task_id}/export` and source Word/report paths. A user with `report.read`,
`report.export` and the actual instance scope can use the new artifact routes,
but receives 403 at original report detail. The new `source` HTTP cases keep their
200/403 expectations; they are not skipped, xfailed or weakened. The parent must
register the reviewed report paths in `_rule` and resolve their string task IDs
through actual `DBInspectionJob` ownership (with legacy result-row fallback),
cross-checking the explicit instance against stored ownership. Trusting only a
query/path instance ID or relaxing scope is not an acceptable fix. This agent does
not modify `request_scope.py` or broaden the allowed API edits.

Existing C05 tests asserting `not_synced` immediately after new plan execution
need a reviewed expectation advance to the new real artifact, not a disabled
publisher. The old manually-created artifact test should update the newly
published row rather than insert a second identical source key. Old plan guard
observations assuming every callback precedes staged writes must distinguish the
new final fence: same-Session staged rows are visible there, but remain invisible
to another Session and disappear on refusal. New tests verify those stronger
transaction facts; production writes must not be hidden from a guard to satisfy
an obsolete observation.

Initial test iterations exposed test-only CMDB fixture required fields and an
incorrect readback dictionary assertion; those were corrected without changing
production schema or relaxing access control. The first broader run naturally
ended 261 passed / 6 failed / 2 deselected in 163.77s. Four failures were the extra
guard call on the unchanged legacy default path; that call was subsequently
restricted to explicit atomic-index publication. The remaining two concern old
plan guard observations described above. These historical counts are not a claim
that the final C08 suite passed. Final source snapshots and results follow below.

The acceptance boundary remains source/isolated regression only. No real DB,
driver cancellation/commit ambiguity, MySQL permission/locking race, field report
export, deployment or C01-C16 completion is claimed. No model, Agent, network,
commit or push was used. This document also holds the pending vault summary;
the vault is outside the authorized write set.

### Signing Isolation Correction

The earlier C08 and C05 compatibility runs did not override
`settings.JWT_PRIVATE_KEY_PATH`; their signing calls could load or create the
workspace platform key. They are historical diagnostic runs, not evidence of
test-key isolation. No claim is made about whether that existing key was newly
created or previously present, and no platform key was inspected, removed or
rotated as part of this correction.

The new `test_inspection_report_atomic_publication.py` has an autouse fixture
that sets an absolute key path beneath each test's own `tmp_path` before its
fixtures execute. It calls the original `jwt_keys.signing_key()` to generate a
real independent RSA PEM, verifies its 0600 mode and confines the configured
path to that temporary directory. Healthy publication/export tests use the real
signer and verifier, not a mock signature or platform credential. The new file
imports the original adapter/staged fixtures but its autouse key override also
covers their execution inside this module. Running those original modules by
themselves still requires their owners to add an equivalent wrapper; this agent
does not change those fixtures under the current C08 write authorization. The
main publication/workflow modules now contain their own temporary-path wrappers.

This correction does not change the publication boundary: only new plan
execution explicitly opts into eager atomic indexing. Ordinary `commit_results`
still defaults to `sync_report_artifact=False`; all-platform/legacy-entry C08
acceptance requires reviewed migration and release of those entry points.

The signature-failure case corrupts only its own temporary PEM and lets the real
key loader/signer raise. It does not substitute a fake successful signature or
mock the signing implementation. Its expected durable result is the explicit
`inspection_report_index_failed` failure, with the attempted successful terminal,
slow SQL, findings and artifact all rolled back before any guarded compensation.
The unchanged-source manual catalogue-sync case invokes the existing API helper
twice and requires the original ID, signature, signed-at, retention, creator,
representations and metadata to remain byte/value identical. An unexpected
render/sign invocation fails that idempotency test.

### C08 Frozen Source Snapshot

The following SHA-256 hashes cover this agent's C08 production write set. The
existing `runner.py` and cancellation/queue integration were not edited in C08.

```text
7c8ee38c85d5436b0745279384e50e5ce0205928b93313031f83c63c4bf2ff43  backend/app/services/inspection/jobs.py
26686af9bda619f27cb05e522cfcd646fc943f3c5f42164d152fb2109134ab76  backend/app/services/inspection/plan_adapter.py
412691d78528d5218ad2a7d8632676d9d42def5d8de3e1e42e22b7146c76d1af  backend/app/services/report_center.py
669d4d5655a490e9ce57068463815c9ed92f03b01000ec3308982ef16fb4a5a6  backend/app/api/v1/report.py
c23ea0baf7e80d6bfb8727116152789cbffb4dad250ef3691dd7a3f9b9a1d2be  backend/app/services/inspection/report_artifacts.py
2c02862d310f46f7493dc69d797ade0180cb069b3cd0e8515be07625d29ca4d2  backend/tests/test_inspection_report_atomic_publication.py
```

### Final Isolated C08 Regression

Command (2026-10-20, local SQLite only):

```sh
env DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests \
  .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider --tb=short \
  backend/tests/test_inspection_report_atomic_publication.py \
  --basetemp=/tmp/c08-real-key-final \
  --junitxml=/tmp/c08-real-key-final.xml \
  > /tmp/c08-real-key-final.log 2>&1
```

Result: **54 passed, 2 failed in 58.56s**, exit 1. Session `95965` completed
naturally and was not restarted after an observation timeout. Log and JUnit:
`/tmp/c08-real-key-final.log`, `/tmp/c08-real-key-final.xml`. All cases use
temporary independent real RSA keys. Actual PEM-load/signing failure, artifact
insert/post-flush failure, optional guard refusal and real queue fence rejection
are covered, including rollback of allocated artifact IDs, slow SQL, results and
the attempted successful job terminal state. The explicit error code survives
guarded failed-job publication instead of becoming `collector_error`. The tests
also prove default legacy jobs do not eagerly index and explicit commit=False
does not internally commit.

The two failures are precisely the `mysql-source` and `oracle-source` HTTP cases
described above: a scoped report reader gets 403 at
`GET /api/v1/reports/{task_id}/detail`, before the subsequent full source export
assertions execute. The real source report payload exists and matches its slow
SQL IDs; it is not missing. Artifact detail, signatures and artifact-format
Markdown/HTML/JSON/Word export with actual allow/deny scope checks pass. Full
source detail/export under the restricted user remains **not accepted**, pending
the owner-authorized `request_scope.py` route/true-job ownership integration.
Those cases remain ordinary failing tests, not skips or expected failures.

Earlier completed runs: `/tmp/c08-atomic-report-frozen.log` had 52 passed /
2 failed in 63.43s before the signing-isolation fix; the original compatibility
run `/tmp/c08-c05-compatibility.log` had 107 passed / 10 failed in 79.23s (seven
old not-synced expectations and three old callback/staged-visibility assertions).
Neither is used as key-isolation evidence. The first isolated run
`/tmp/c08-isolated-signing-final.log` had 54 passed / 2 failed in 63.48s; it still
injected signing failure via an exception stub. The final run above supersedes
that injection with a real temporary-key load failure. No test process remains
running for this handoff.

Only the five production files hashed above, the new C08 test module and this
document were changed for C08 by this agent. `plan_service`, `plan_queue`, models,
migrations, request-scope policy and parent-owned fixtures/tests were not edited.
The release flag remains unchanged. This is an atomic **new plan path** index
implementation with a documented source-route authorization blocker, not a claim
of all-entry/platform C08, C01-C16, deployment or field acceptance.

### C08 Source-Report 403 Recheck

Read-only investigation on 2026-10-20 confirms an integration coverage gap in the
global scope gate, **not missing report actions or a missing report payload**.
The authorization policy is still fail-closed; this is not a demonstrated data
disclosure. No global request-scope, main, action-policy or permission-catalogue
file was modified. Production C08 files retain the frozen hashes above.

The actual call chain and evidence are:

1. `access_control.py:272-277` maps GET report details to `report.read`, and the
   source exports to `report.export`. The fixture has an active explicit role
   granting exactly those two actions; `effective_action_keys` and
   `is_action_allowed` both confirm them. Granting `inspection.read`, `*`, admin
   or unrestricted asset scope is neither necessary nor an acceptable workaround.
2. `resolve_scope` confirms the actual CMDB AssetInstance ID and its
   native-instance mapping, with read access to exactly the real job's instance.
   Another real native instance and queued job are outside that scope. Their
   producer is the existing authorized execution actor, not the read-only report
   user. An initial added-fixture attempt used that reader to enqueue the foreign
   job and correctly failed with `permission_revoked`; correcting the producer
   did not add any action to the report reader or change the original 403 cause.
3. `registry.py:102` registers `report_center.router` before `report.router`,
   matching the test's router order. The real `_matched_route` resolves each
   tested source URL to its intended task-ID route, not a catch-all artifact or
   numeric report route. The extracted inspection mapping's source export URL
   exactly matches the existing Word route; it is not changed to a different,
   smaller index export to conceal the failure.
4. `request_scope.py:220-228` registers the existing inspection and artifact
   families but not the four source-report route templates below. Therefore
   `_rule` returns None, and `scope_allows_request_sync` returns False at
   lines 600-602 **before** ownership resolution. `main.py:345-371` first passed
   the action check, then returns the scope-specific 403 message.
5. A read-only `_Targets.reference("task_id", job_id)` probe shows the second
   required integration point: `request_scope.py:497-501` resolves only
   artifact_id/schedule_id under `/reports/`, so it raises
   `Unresolved report resource` for task_id even if route registration alone is
   added. Both levels need the owner's reviewed change.

Four missing reviewed route templates (GET; existing read-method policy applies):

```text
/reports/{task_id}/detail
/reports/{task_id}/export
/reports/inspections/{instance_id}/{task_id}
/reports/inspections/{instance_id}/{task_id}/export-word
```

Minimal patch recommendation for the main scope owner:

- Register only these exact read route templates in `_rule`, with the persisted
  task ID as an ownership anchor. Do not allow the entire `/reports/*` prefix or
  accept an arbitrary `instance_id` as proof.
- Add a source-task resolver for these routes. Prefer the real
  `DBInspectionJob.instance_id`; if the job is absent, resolve the existing legacy
  `DBCheckResult.task_id` rows and require all represented instances in scope.
  Missing/unresolvable tasks must stay denied. Normal no-result jobs may pass
  ownership authorization but the existing report handler still returns 404;
  authorization must not invent a report or a successful artifact.
- Cross-check explicit path/query instance IDs against stored job ownership,
  including when the user happens to hold both asset scopes. The query/path
  hint must not override a foreign task. `result_writer.py:233-241` reads every
  row for an unfiltered task ID, so mixed-owner rows must also be checked or
  rejected; a caller hint must not mask such inconsistent evidence.
- Preserve existing action checks and artifact ownership rules. No changes to
  global auth bypasses, report reader permissions or normal writer release
  defaults are needed for these task-addressed read routes.

The owned C08 tests now collect all six allowed source URL variants (detail with
and without instance query, native structured payload, Markdown, HTML and Word),
then run negative cases before asserting all positive responses are 200. This
keeps the two ordinary acceptance failures visible while ensuring the first
403 no longer prevents later negative tests. JUnit properties contain the real
`source_scope_proof` and `source_http_statuses`: action allowed true, native
scope containing the true owner, correct matched route, rule null, unresolved
task reference and all six HTTP statuses 403 for each engine. No expected status
was weakened and no scope/action function was patched to allow a request.

Additional negative cases cover an actual foreign job with an allowed instance
hint, mismatched native-instance paths/queries, a nonexistent task, removal of
only report.export while report.read still works, and actual asset-scope
revocation. A separate two-engine test directly exercises the original source
payload/detail/HTML/Word builders against the real saved rows and slow-SQL IDs.
This proves content/rendering existence without claiming an authenticated HTTP
pass. The focused recheck ended **4 passed, 2 failed, 52 deselected in 10.58s**:
only mysql-source/oracle-source fail the preserved positive HTTP expectations.
Log/JUnit: `/tmp/c08-scope-investigation-v2.log` and
`/tmp/c08-scope-investigation-v2.xml`.

Read-only global snapshots for this recheck:

```text
9bb02d02ede7ea6e718d36684443d2147b400d8585cd090a711e6d7a247f2e4d  backend/app/services/request_scope.py
562b993603179eac19e5afe7fc54568ca0a82d276a780321c0c6f56285d677c0  backend/app/main.py
37d25ea64ada5b24f79e7f45afa2bf2fcddf78a0ff6d42b0a5491ece10dc9d3d  backend/app/services/access_control.py
a037ec6a600fe8022006b457a1bf73f0118f2fbc36b3465152de96797f5d2cb9  backend/tests/test_inspection_report_atomic_publication.py
```

The last hash supersedes the earlier C08 test-file snapshot only. This follow-up
changes the owned test module and this document, not any production report
mapping or shared authorization source. The earlier signature/isolation/atomic
index contract remains unchanged; whole-platform and field acceptance remain
open.

Full recheck of the entire owned C08 batch completed with **56 passed, 2 failed
in 61.46s**, exit 1, session `84564`. Command is the prior full-suite command with
`--basetemp=/tmp/c08-scope-complete`,
`--junitxml=/tmp/c08-scope-complete.xml`, and output redirected to
`/tmp/c08-scope-complete.log`. No cases were deselected in this full run, and all
use isolated temporary real signing keys. The two failures are still exactly
mysql-source/oracle-source, now after exercising the complete URL and negative
matrix rather than stopping at the first positive request. Atomic publication,
real-key signing failure, insert/flush failure, lost guards, real IDs, manual sync
signature/retention stability, no-result cancellation/unknown and original source
rendering all pass. Global and production-file hashes were rechecked unchanged
after the run; `git diff --check` passes. No test process was restarted due to an
observation timeout, and all processes for this follow-up have ended.

Main-agent handoff remains necessary for the exact global scope changes above.
The current ordinary failing tests preserve the intended 200 contract and real
403 boundary. This agent did not broaden permissions or edit `request_scope`,
`main`, access-control services/catalogues, plan services, queues, or the report
mapping during this investigation. Only the owned new C08 test and this handoff
document changed. No external network, field system, commit or push was used.

### Independent Acceptance Of The Main Scope Fix

The main agent subsequently implemented the exact source-report scope family.
This supersedes the earlier unresolved source-route blocker for the source
snapshot below; the earlier failing logs remain historical evidence, not the
current result. This agent only read that implementation and added the new owned
`backend/tests/test_inspection_report_source_scope.py` plus this document.
No global authorization, permission, route or production report source was
changed by this follow-up.

The new module runs the original native MySQL/Oracle offline collection fixtures
through real queued jobs, staged slow-SQL/result writers and explicit atomic
artifact publication, then invokes real AuthMiddleware and original report
handlers. A real restricted report reader holds only report.read/report.export;
the identity lookup is an explicit offline fixture. The original temporary RSA
key fixture is imported as autouse, so no platform signing key is used. A
transparent observer calls the original report builder and records entry: a
scope rejection must not reach it; an authorized 200/404 must reach it exactly
once. The real scope function is independently asserted without changing its
decision or mocking permissions.

Twenty additional cases cover:

- **Legacy fallback (2):** no DBInspectionJob exists for either legacy task.
  Actual result rows are saved through the existing writer. Every normal source
  format for the allowed owner returns 200; the foreign legacy task remains 403
  despite an allowed instance hint. Reading legacy content creates no artifact.
- **Zero-result actual jobs (6):** queued, acknowledged cancelled-before-run,
  and reaper force-closed jobs have real persisted identities but no result rows.
  Scope authorization is True, the original builder is invoked, and all source
  handlers return their actual 404 `巡检记录不存在`. No normal report or artifact
  is invented, including cancellation/uncertainty cases.
- **Wrong hint with two real scopes (4):** both canonical asset/native scopes are
  explicitly granted and remain restricted, not scope=all. A real job or a
  one-owner legacy task succeeds with its actual owner, but wrong query/path
  hints, conflicting path/query IDs and duplicate query IDs in both orders are
  denied before the builder even though both IDs are individually in scope.
- **Mixed persisted owners (8):** an explicit inconsistent-evidence fixture adds
  a foreign result with the original writer. A job-backed task cannot conceal it
  behind the job's legitimate instance hint, nor omit the hint to bypass the
  check. Legacy multi-owner rows also reject a single-owner hint. Both cases are
  exercised with one scope and both scopes granted. All denials precede builder
  entry; the pre-existing signed artifact is not overwritten by a read request.

The tests do not generalize the legacy hinted-task assertion to all possible
unhinted multi-owner historical tasks, and do not stress the 4097 distinct-owner
cardinality boundary. They verify the requested ownership/HTTP cases without
loosening access or changing source report mapping/rendering.

Focused independent run: **20 passed in 18.07s**, exit 0, session `95854`.
Log/JUnit: `/tmp/c08-source-owner-independent.log` and
`/tmp/c08-source-owner-independent.xml`. Its only test module is
`test_inspection_report_source_scope.py`; disposable SQLite, explicit source
fixtures and temporary real signing keys are used throughout.

Snapshots during the independent recheck:

```text
97e2f75b36f4d514f13fc531533a604dabd7c60e8ca664d610d5ec5afa31c368  backend/app/services/request_scope.py  (main-agent change, read only here)
562b993603179eac19e5afe7fc54568ca0a82d276a780321c0c6f56285d677c0  backend/app/main.py
37d25ea64ada5b24f79e7f45afa2bf2fcddf78a0ff6d42b0a5491ece10dc9d3d  backend/app/services/access_control.py
a037ec6a600fe8022006b457a1bf73f0118f2fbc36b3465152de96797f5d2cb9  backend/tests/test_inspection_report_atomic_publication.py
15e67e98673f8e5d9a244d6509f38e74045ff6bee42ceb44433378b9128107b8  backend/tests/test_inspection_report_source_scope.py
```

Independent combined acceptance completed with **78 passed in 75.72s**, exit 0,
session `40272`, no deselection or expected failures:

```sh
env DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests \
  .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider --tb=short \
  backend/tests/test_inspection_report_atomic_publication.py \
  backend/tests/test_inspection_report_source_scope.py \
  --basetemp=/tmp/c08-independent-complete \
  --junitxml=/tmp/c08-independent-complete.xml \
  > /tmp/c08-independent-complete.log 2>&1
```

Log/JUnit: `/tmp/c08-independent-complete.log` and
`/tmp/c08-independent-complete.xml`. The original two source HTTP cases now pass
their unchanged 200 contract, full source exports and actual permission/scope
revocation checks, alongside all atomic publication/signing/flush/ownership-loss
and no-report uncertainty cases. The new twenty-case module passed both alone
and in the combined run. Test processes completed naturally; none remain active.
Global scope and production source hashes were rechecked against the snapshots
above, with no further changes by this agent. `git diff --check` passes.

The scoped source-report blocker is therefore independently closed for this
local isolated snapshot under the main agent's scope patch. This does **not**
change the existing release gate or publication opt-in: only the new plan path
eagerly indexes; ordinary jobs still default sync_report_artifact=False. No
all-entry/platform C08, C01-C16, migration rollout, real MySQL locking/permissions,
field driver/export or production acceptance is claimed. No live target,
network, model, Agent, commit or push was used. Current-turn writes are only the
new source-scope test module and this document.

### C05/C08 Standalone Fixture And Compatibility Closure

The user subsequently authorized edits to the original adapter/staged test
modules to close the historical 107-pass/10-fail compatibility run. This is a
test/fixture-only update; no production service is changed. The main agent had
already advanced the two publication assertions to the new actual-artifact
contract. This agent leaves workflow, publication and cancellation test sources
read-only and jointly verifies them below.

Changed test ownership and reasons:

- `test_inspection_plan_adapter.py`: the four successful engine cases now require
  the real committed source artifact ID, exact job/instance/source-key binding,
  valid real signature, readback and replay identity. The bounded-reference case
  updates and signs the existing actual index and archives it with the original
  archive service, rather than inserting a second identical source key. Private
  URI/content/large-output exclusion checks remain intact.
- The successful plan guard protocol now expects exactly five callbacks in the
  same Session, not four. The last sees staged results/artifact after explicit
  flush. A separate Session must still see no newly published rows and a running
  job until the root commit. All old denial calls remain; a fifth-call False
  denial and fifth-call BaseException loss are added. They must remove results
  and allocated artifacts and leave the job running without a fabricated
  terminal failure. Permission, template/script race and failure assertions are
  not removed or weakened.
- `test_inspection_staged_publication.py`: the plan entry now expects counts
  `[0,0,0,0,2]`, with cross-Session invisibility of slow SQL/results/artifact at
  the final guard. It requires the real final artifact ID. Native execute,
  submit/idempotent replay and legacy runner still require **zero artifacts**;
  direct default `commit_results` also requires `not_synced` and no terminal
  artifact reference. This verifies the old default False rather than changing
  production defaults to satisfy acceptance.
- New plain helper `inspection_fixture_signing.py` is called directly by both
  base `env` fixtures. It overrides any non-test key path before invoking the
  actual RSA loader, requires a key of at least 2048 bits and a 0600 PEM beneath
  that test's tmp_path, and never reads the platform default key. It preserves a
  containing fixture's already-isolated path, including the C08 bad-PEM target;
  symlink escape from the test root is rejected. It neither mocks signing nor
  requires another test module's autouse wrapper. Standalone adapter/staged runs
  and imports of their env fixtures are now isolated themselves, superseding the
  earlier wrapper-gap handoff.

The adapter module alone completed **71 passed in 36.02s**, exit 0, session
`43327`. No other test module was selected and no wrapper was installed. Log and
JUnit: `/tmp/c05-c08-adapter-standalone.log` and
`/tmp/c05-c08-adapter-standalone.xml`. The two extra cases are the new last-fence
denials; none of the old fault cases was dropped.

Initial test/fixture snapshots for the compatibility run:

```text
a7c599eaae61a18aedf80f756cc060d0f361ff1f6a7ec2e9092aec054b3aa40d  backend/tests/inspection_fixture_signing.py
fe5fc87c46828a20dad51e5421ac894f2ce1c088e40ace909e4f76f1fcca3796  backend/tests/test_inspection_plan_adapter.py
a6a78d233c74eb69636083d44ae5de286d1a2691830ad7b9dcbf81d9549030c4  backend/tests/test_inspection_staged_publication.py
f12edc4d5fafc1b0f73684a108795455632f257c6528a26f3d4e930a7ee8d009  backend/tests/test_inspection_plan_workflow.py  (read only)
e7561c7a3fa5d4d620981fe06e5ef426ec9189fe6cf85f4e89800e8bb7343706  backend/tests/test_inspection_plan_publication.py  (read only)
218b277eb5d85e52faea3235bb74033f4b3e3f5aef6223c2f3445e8ef876a4a1  backend/tests/test_inspection_plan_cancellation.py  (read only)
```

The staged module alone completed **44 passed in 22.18s**, exit 0, session
`98970`, also without another module or wrapper. Log/JUnit:
`/tmp/c05-c08-staged-standalone.log` and
`/tmp/c05-c08-staged-standalone.xml`. Native default non-indexing, original
findings/count/slow-SQL IDs, rollback/ghost-ID rejection, SQLite outer transaction,
guard-held writers, cancellation/timeout re-fencing and actor revocation all
remain covered. The standalone adapter/staged commands used the same isolated
environment below, selecting only their own module, with respective basetemp
directories `/tmp/c05-c08-adapter-standalone` and
`/tmp/c05-c08-staged-standalone`.

The expanded joint compatibility command also covers the new C08 modules, so
their real bad-PEM signing fault and task-ownership fixtures must continue working
when imported base fixtures now isolate themselves:

```sh
env DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests \
  .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider --tb=short \
  backend/tests/test_inspection_plan_adapter.py \
  backend/tests/test_inspection_staged_publication.py \
  backend/tests/test_inspection_plan_workflow.py \
  backend/tests/test_inspection_plan_publication.py \
  backend/tests/test_inspection_plan_cancellation.py \
  backend/tests/test_inspection_report_atomic_publication.py \
  backend/tests/test_inspection_report_source_scope.py \
  --basetemp=/tmp/c05-c08-joint-compatibility \
  --junitxml=/tmp/c05-c08-joint-compatibility.xml \
  > /tmp/c05-c08-joint-compatibility.log 2>&1
```

Final joint result: **265 passed in 232.17s**, exit 0, session `92089`.
JUnit confirms 265 cases, zero failures, zero errors and zero skips. Counts by
module, not additional independent runs:

| Module | Passed | Source Treatment |
| --- | ---: | --- |
| test_inspection_plan_adapter | 71 | Authorized expectation/fixture update |
| test_inspection_staged_publication | 44 | Authorized expectation/fixture update |
| test_inspection_plan_workflow | 8 | Read only |
| test_inspection_plan_publication | 4 | Read only |
| test_inspection_plan_cancellation | 60 | Read only |
| test_inspection_report_atomic_publication | 58 | Unchanged; fixture-composition recheck |
| test_inspection_report_source_scope | 20 | Unchanged; fixture-composition recheck |

The five requested compatibility modules account for 187 of the joint cases;
the C08 extensions account for the other 78. Workflow/publication/cancellation
hashes above are identical before/after execution. The C08 bad-PEM failure still
uses the intended temporary file and returns the explicit index-failure code;
mixing the newly self-isolating base fixtures did not bypass it. Original
permission/race/cancellation/failure assertions and native default non-indexing
all pass. No new product defect requiring a production fix was found in this
regression scope. The historical 107-pass/10-fail result is now superseded for
these reviewed contract expectations, not erased or reclassified as field proof.

Authoritative joint log/JUnit: `/tmp/c05-c08-joint-compatibility.log` and
`/tmp/c05-c08-joint-compatibility.xml`. Both standalone sessions and the joint
session completed naturally; no tests remain running and no observation timeout
caused a restart. `git diff --check` passes. Production C08 hashes remain those
recorded above; the current integration snapshots also remained unchanged:

```text
ed878b20ee22be7ccf042a9415e296ea74565c7e28165e2a8036ea0fdc033538  backend/app/services/inspection/runner.py
93ff32fc599aedd5ed155731051356168d685195f2d1c99984a88a806ba11a60  backend/app/services/plan_service.py
876d436e81665a40161991839e6598201311d4ef86d42a2efdfc9de2b702800d  backend/app/services/plan_queue.py
97e2f75b36f4d514f13fc531533a604dabd7c60e8ca664d610d5ec5afa31c368  backend/app/services/request_scope.py
```

Current-turn writes are exactly the two authorized test modules, new test helper
and this document. No production service, shared authorization source, model,
migration or read-only integration test was edited. Report publication is still
opt-in for the plan path; legacy/native default False and the unchanged release
flag remain acceptance boundaries. This closes isolated C05/C08 compatibility
regression, not all-entry/platform C08, real-engine permission/locking races,
field cancellation/export, deployment, or C01-C16 live acceptance. No external
network, real target, model, Agent, commit or push was used.
