# C06 Durable Batches and Independent Worker

Date: 2026-10-20. Scope: C06 / U06 foundation, not C07 or field acceptance.

## Design Before Implementation

- Persist a batch and its ordered, explicit plan IDs in one committed transaction before
  returning acceptance. No target inference, handler strings, scripts or executor copies.
  Maximum 100 members per batch, 1000 globally pending/uncertain members, 8 active members.
- Reuse `transaction_lock.lock_transaction` with key `capability_queue`; claims additionally
  compare state and owner token. Only one non-cancelled membership per plan prevents duplicate
  delivery across different keys. Freeze the original revision, binding hash and exclusion key.
- Check current actor, owner, scope, frozen plan and live readiness during enqueue/claim;
  repeat authorization in the execution guard. Store only the authenticated user ID.
- Commit claims before executing `plan_service.execute_plan`; independent heartbeats renew
  leases in separate sessions. Never hold the queue write lock across handler I/O.
- Expired ownership: never-started validated plans can return to queued; running/uncertain
  plans enter `reconciliation_required`, with no automatic replay. Settled plans archive only
  when persisted terminal evidence is present. Batch progress is derived from actual plans,
  never from an ACK, a returned handler dict, or queue completion alone.
- Queued cancellation cancels delivery, not the underlying reusable plan. Running cancellation
  only records a request; it cannot claim that an external operation has stopped.

## Integration Contract

Owned files: `capability_execution.py`, `plan_queue.py`, `plan_worker.py`,
`v20261020_04_capability_execution.py`, `test_plan_queue.py`, this document, and the subsequently
authorized `scripts/capability_worker.py` thin CLI entrypoint.
The primary agent owns model import, migration registration, HTTP, executor and frontend.

Public service contracts:

```python
enqueue_batch(db, plan_ids: list[str], *, actor: dict, idempotency_key: str,
              deadline_seconds: int = 3600) -> dict
read_batch(db, batch_id, *, actor) -> dict
list_batches(db, *, actor, page=1, page_size=50) -> dict
process_one(session_factory, *, worker_id: str) -> dict | None
cancel_batch(db, batch_id, *, actor) -> dict
assert_plan_editable(db, plan_id) -> None
```

Idempotency is scoped to the requesting user; input includes ordered plan IDs and deadline
duration. Replays recheck current read authorization. Changed input rejects with
`idempotency_conflict`. Writes own their transaction and reject pre-existing uncommitted
ORM changes. Deadlines cover waiting and execution; they are not proof of external stop.
Deadline duration is restricted to 1..86400 seconds; keys to 128 characters. An explicit
settled plan can be archived at admission only with durable terminal evidence. A new run
of an already-executed plan still requires a new plan, not a new batch key.

`plan_service.execute_plan(db, plan_id, actor={"user_id": ...}, execution_guard=guard)`
calls `guard()` (or `guard(db)`) before claiming, each step and result publication. The guard
does a live, read-only check in an independent short session; it does not hold write locks
through handler I/O. It also installs a session-local SQLAlchemy `before_commit` fence:

1. Lock `capability_plan_state`, then `capability_queue`.
2. Read the member, batch and Core plan projection using `FOR UPDATE` where supported.
3. Check current token, worker, lease, deadline, owner and frozen binding; reject staged
   immutable-plan changes. Recheck live permission, scope, capability, target and approval.
4. Commit the executor's staged marker/result under those same locks. Never refresh the
   plan/step ORM objects here, because that would erase staged outcomes before the flush.

This catches even failed/skipped/early-return publications and closes the race between a
checkpoint and a commit. The listener is removed when execution ends. The executor must
end old read snapshots after I/O and must not keep an uncommitted write transaction across
external I/O. The fence covers this session, not arbitrary separate-session external writers.

`ExecutionLeaseLost` currently derives from `RuntimeError` and carries a machine-readable
code. Safety does not rely on bypassing every generic `except Exception` through inheritance:

1. `plan_service._guard_execution` converts guard exceptions/false returns to
   `PlanOwnershipLost("execution_ownership_lost")`. The executor catches that type before its
   generic handler-failure branch, rolls back and re-raises; it does not publish a failed step.
2. The queue's session-local `before_commit` fence remains installed throughout execution.
   It rechecks the current owner/token/lease and live binding/authorization under the short
   transaction locks above. It converts refusal to `PlanOwnershipLost` and aborts commit,
   including a failed/skipped/early-return result staged by an intermediate generic catch.
3. `process_one` explicitly catches both ownership exception types and rolls back before
   removing the listener. Its generic exception branch also rolls back, never settles a plan.
   `_complete` only archives separately persisted, validated terminal evidence; unfinished or
   uncertain started work remains reconciliation_required, and a replaced token cannot update
   the old queue member. A request failure/ACK is not business failure or success evidence.

The fence protects the executor's session, not arbitrary unfenced writers or external side
effects. A genuinely current worker can still publish a real handler failure; a lost worker
cannot use that failure path to bypass the commit check. An executor lacking an explicit
`execution_guard` parameter fails closed before any claim, with no unfenced fallback.

Correction on 2026-10-20: an earlier handoff proposal incorrectly required BaseException,
based on a stale summary. The user withdrew it after checking the source. No inheritance
change was made; the RuntimeError contract and real commit fence above describe current code.

### Lock Order and Revision Binding

The **only two-lock order is `capability_plan_state` then `capability_queue`**. Enqueue,
`assert_plan_editable` and commit fencing follow it. Claim/renew/recover/complete/cancel only
take queue and do not call plan-state writing entrypoints. Current readiness helpers are
read-only. Keep this rule when adding new adapters; never acquire plan-state while holding
only queue. `assert_plan_editable` holds both locks until its caller commits or rolls back;
it does not commit the caller's revision transaction.

All non-cancelled ledger states (including blocked, expired, archived and reconciliation)
reject editing. Only cancelled delivery with no started plan/step or result evidence allows
revision. Its historical member retains its original revision/hash and reports
`binding_current=false` after revision; it never exposes the new plan's result as an old
batch product. A new explicit batch key may bind that revised, never-started plan. The
primary agent's governance service continues to enforce owner and executed-plan reruns.

### Recovery, Cancellation and Bounds

- Leases last 45 seconds; independent heartbeats run every 10 seconds, capped by the batch
  deadline. A failed heartbeat does not extend ownership in memory.
- Each poll examines at most 32 expired/waiting members plus 8 uncertain members and at most
  32 claim candidates. Uncertain records rotate by last check time and cannot starve expired
  leases. Active/uncertain target keys exclude candidates before the claim scan.
- Queued/running/reconciliation count toward the global admission cap. An unresolved target
  stays excluded. Never-started recovery requires a validated/approved plan with all steps
  pending and no started timestamp or result. Running plans are never automatically replayed.
- Terminal archival checks the frozen current revision, terminal steps and typed references;
  succeeded and partial products use the existing `_result_ref` contract. Every inspection
  reference (including failed/unknown products) additionally uses
  `plans.validate_effect_reference(db, plan, step)`: the real local domain job must match ID,
  owner, target, execution idempotency, frozen policy and terminal outcome. The referenced
  outcome must also match the step verdict. A single partial effect step is a valid partial
  product, not a success or an empty result. Queue ACK alone cannot
  create a successful outcome. Even a persisted settled `unknown` (or partial result with an
  unknown step) stays in reconciliation and retains target exclusion; terminal uncertainty is
  not proof of a resolved side effect. Exception messages/payloads are not stored or logged.
- Queued cancellation stops only delivery; it deliberately does not rewrite the plan's
  outcome or call `cancel_plan`. Running cancellation records a request and leaves the
  lease/execution running until actual evidence says otherwise.
- Listing applies current access checks before counting/pagination and streams candidates.
  It currently scans owner-visible historical batches; indexed SQL authorization projection
  and large-history pagination/load acceptance remain future optimization work.

## CLI

From the repository root, with the normal application environment and registered migration:

```sh
PYTHONPATH=backend python scripts/capability_worker.py --once
PYTHONPATH=backend python scripts/capability_worker.py --worker-id worker-1
```

The approved thin script emits structured JSON logging. App modules contain no `print` or
`__main__` launcher, following the existing static gate. This intentionally replaces the
initial `python -m app.tasks.plan_worker` request, as subsequently authorized. Loop mode uses
bounded exponential idle/storage-error backoff. SIGTERM/SIGINT stop new claims, allowing
in-flight work and its heartbeat to finish; there is no false external-stop acknowledgement.
CLI does not auto-migrate schema or start/restart the platform.

## Verification and Remaining Boundaries

Implemented tests use isolated file-backed SQLite, concurrent sessions/workers, a new-engine
restart, independent heartbeat, real executor success/failure fencing after lease loss,
approval-at-commit fencing, concurrent enqueue/revision, cancellation and migration safeguards.
Subprocess checks exercise actual child-process death, CLI restart, `--once` and SIGTERM without
starting a platform. Safe never-started work resumes; already-started work does not replay.
No real database, Agent, platform restart, deployment, commit or push is authorized here.
MySQL/PostgreSQL live locking and isolation-level revocation races (including Repeatable Read),
full external fencing, distributed cancellation, HTTP acceptance, frontend wiring and C16
long-run/load acceptance remain primary-agent / later-stage work. SQLite regression is not
evidence of distributed side-effect fencing or field acceptance.
There is intentionally no manual reconciliation/clear-target interface in this foundation;
unresolved targets remain occupied until a later audited reconciliation implementation.
The inspection release flag defaults to false; tests enable it only in scoped fixtures and
seed isolated domain rows for reference checks. These are not live inspection acceptance.

### Historical C05 Handoff Verification

At an earlier C05 handoff, the isolated queue run was **53 passed, 1 failed**. The failing case was
`test_real_domain_evidence_including_single_partial_step_can_archive[succeeded]`:
`validate_effect_reference` directly compares the plan envelope's `succeeded` with the
inspection domain's `jobs.outcome_block(job)["outcome"] == "success"`. The primary agent owns
normalizing this boundary in the adapter/plan validator. Queue keeps the strict check and
does not guess a success conversion or change shared plan-service source. Partial, failed,
invalid domain identity/owner/target/policy/execution/terminal state and unknown-reference
tests pass. The inspection domain retains its historical vocabulary.

The earlier C06/governance/migration combined run passed 114 cases before the concurrent
C05 integration changed shared definitions. A later combined run stopped at three existing
`test_capability_plan.py` cases using an old hard-coded inspection definition version
(`stale_definition_version`); that file is outside this worker's edit set and was not changed.
These are distinct source phases, not a claim that the current entire suite is green.

Current backend static gate: **499 files, 0 findings**. Migration registration/deployment
regression passed **22 cases** using only isolated databases. Representative commands from
the repository root (the final interpreter path was verified locally):

```sh
env PYTHONPATH=backend DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false PYTHONDONTWRITEBYTECODE=1 .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_plan_queue.py
env PYTHONPATH=backend DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false PYTHONDONTWRITEBYTECODE=1 .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_migration_registration.py backend/tests/test_deployment_and_migrations.py
.venv-mac-primary/bin/python scripts/check_backend_static.py
```

### P1 Closure Follow-Up, 2026-10-20

The domain boundary is now explicit: inspection references preserve `outcome="success"`,
while a successful plan step uses `status="succeeded"`. A later independent review found
the queue still compared these raw strings directly, leaving otherwise successful batches
in reconciliation and retaining target exclusion. A new real-workflow batch-level assertion
reproduced this issue; checking only the worker's plan outcome had missed it.

Fixed only the comparison in `plan_queue._evidence`: first keep `_result_ref` strict and
validate the real domain job through `validate_effect_reference`; only then map local
comparison value `success` to `succeeded`. Do not change the stored reference. Partial,
failed and expired retain their original verdicts; unknown still requires reconciliation
and retains target exclusion. Wrong aliases such as domain `succeeded`, `SUCCESS`, `Success`
and `ok` are rejected before this mapping and cannot be archived.

Final current queue plus workflow regression: **75 passed in 46.99 seconds** (67 queue,
8 workflow cases). The expanded coverage includes unchanged raw reference payloads,
success/partial/failed/expired archival, terminal-verdict mismatches, domain alias rejection,
unknown followed by cancellation retaining target exclusion, truthful completion after an
in-flight cancellation request, and cancelled recovered claims rejecting old-owner commits.
Backend static gate: **499 files, 0 findings**. These results supersede the historical open
finding above; no other agent's source or test file was modified for this fix.

```sh
env PYTHONPATH=backend DATABASE_URL=sqlite:// DB_TYPE=sqlite APP_MODE=quality_test DEBUG=false PYTHONDONTWRITEBYTECODE=1 .venv-mac-primary/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_plan_queue.py backend/tests/test_inspection_plan_workflow.py
```

This is isolated regression evidence only, not a real database inspection, platform restart,
deployment, C16 field/load acceptance, commit or push.

### Confirmed Cancellation Follow-Up, 2026-10-20

`_evidence` now requires every started cancelled step, including one inside a partial plan,
to carry an inspection_job typed reference validated against Mencius's real domain helper.
Missing references cannot bypass validation. A started cancelled plan additionally requires
the exact persisted plan_steps reference, a cancelled first step and skipped remaining steps.
Never-started cancellation retains its separate contract; batch cancellation is not business
cancellation. Force-closed/unconfirmed/unknown effects keep target exclusion and never replay.

Frozen combined regression: **155 passed in 143.30s** (67 queue, 8 workflow, 56 domain
cancellation, 24 C07 schema). This includes handler success/failure after lease loss and an
old worker staging failed output after cancellation: both actual commit fences reject loss.
Current backend static: **501 files, 0 findings**, after primary-owned C07 import/migration
registration. No exception inheritance change was made. These remain isolated source/schema
results, not external stop/fencing or full C07/C16 acceptance.
This result binds plan_service `32bbd6f7`, queue `08935084` and cancellation tests `0222ee49`.
Mencius's subsequently authorized force-close/unknown reference-retention fix is not covered
by this snapshot and needs its own verification; queue/schema source remains frozen here.

Obsidian archival cannot be written in this task's permitted roots and the user limits edits
to the seven approved files. This document is the in-workspace archival draft; it is not a
knowledge-vault write. Historical context: Obsidian `10-Projects/mesdbtools/主页.md` stresses
missing evidence is not healthy/successful and isolated regression is not field acceptance;
all implementation and test claims here come from the current workspace, not that history.
