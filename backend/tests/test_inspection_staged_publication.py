"""Real queued/staged writes and rollback truth; explicit SQL-source fixtures, no network."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event, update
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.analyzers.rules import Finding
from app.core.database import Base
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.models.report_center import ReportArtifact
from app.models.slow_sql import DBSlowSQL
from app.models.user import User
from app.services.inspection import execution_policy, jobs, plan_adapter, policy_service, result_writer, runner, run_readback
from app.services.inspection import oracle_script_checks, script_db_checks
from inspection_fixture_signing import isolate_report_signing


class ExecutionLeaseLost(BaseException):
    pass


@pytest.fixture(params=("mysql", "oracle"))
def env(request, tmp_path, monkeypatch):
    isolate_report_signing(tmp_path, monkeypatch)
    def no_network(*args, **kwargs):
        pytest.fail("No live target, model, or Agent is allowed")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    engine = create_engine(f"sqlite:///{tmp_path / 'staged.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(jobs, "SessionLocal", factory)
    monkeypatch.setattr(policy_service, "ensure_default_policies", lambda db: None)
    kind = request.param
    state = SimpleNamespace(factory=factory, engine=engine, kind=kind, action=None, source_calls=[])
    state.mysql_rows = [{
        "digest": f"fixture-{index}", "schema_name": "FIXTURE", "digest_text": f"SELECT {index}",
        "count_star": 2, "avg_seconds": 0.5, "total_seconds": 1, "sum_rows_examined": 4,
    } for index in (1, 2)]
    rows = [{
        "sql_id": f"fixture-{index}", "schema_name": "FIXTURE", "sql_text": f"SELECT {index} FROM dual",
        "executions": 2, "elapsed_seconds": 1, "buffer_gets": 4, "disk_reads": 0,
        "rows_processed": 4, "con_id": 0, "child_number": 0, "plan_hash": index,
    } for index in (1, 2)]
    sources = ("top_sql_elapsed", "top_sql_buffer_gets", "top_sql_disk_reads")
    state.oracle_metrics = {source: deepcopy(rows) for source in sources}
    state.oracle_metrics["_query_evidence"] = {
        source: {"status": "success", "sql": "SELECT fixture", "columns": list(rows[0])}
        for source in sources
    }

    def source():
        state.source_calls.append(kind)
        assert state.db.query(DBSlowSQL).count() == 0
        if state.action:
            state.action()

    @contextmanager
    def mysql_connection(instance, **kwargs):
        source()
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.side_effect = lambda: deepcopy(state.mysql_rows)
        yield conn

    @contextmanager
    def oracle_probe(instance, **kwargs):
        yield object()

    def oracle_collect(collector, **kwargs):
        source()
        return deepcopy(state.oracle_metrics)

    metric = "mysql.slow_queries.per_min" if kind == "mysql" else "oracle.performance.top_sql_elapsed"
    def base_finding():
        return Finding("fixture", "Source check", "normal", metric, "1", "", "fixture source", "", {
            "execution_status": "success", "sql": "SELECT fixture", "rows": [{"n": 1}],
        })

    monkeypatch.setattr(runner, "mysql_connection", mysql_connection)
    monkeypatch.setattr(script_db_checks, "collect_mysql_script_findings", lambda *args, **kwargs: [base_finding()])
    monkeypatch.setattr(runner.OracleHealthCollector, "collect", oracle_collect)
    monkeypatch.setattr(runner, "analyze_oracle_metrics", lambda metrics: [base_finding()])
    monkeypatch.setattr(oracle_script_checks, "collect_oracle_script_findings", lambda *args, **kwargs: [])
    monkeypatch.setattr(execution_policy, "oracle_connection", oracle_probe)
    monkeypatch.setattr(execution_policy, "detect_oracle_profile", lambda conn: SimpleNamespace(
        major_version=11, cdb="NO", version="11.2.0.4",
    ))
    with factory() as db:
        actor = User(username="staged-fixture", password_hash="unused", role="admin", status="active")
        instance = DBInstance(
            instance_name="staged-target", db_type=kind, host="fixture.invalid", port=3306,
            username="fixture", password_cipher="unused", enabled=1,
        )
        policy = DBInspectionPolicy(
            policy_key=f"staged-{kind}", name="Staged fixture", db_types_json=[kind], enabled=1,
            item_config_json=[{"key": metric, "enabled": True}],
        )
        db.add_all([actor, instance, policy])
        db.commit()
        state.db, state.actor, state.instance, state.policy = db, actor, instance, policy
        yield state
    engine.dispose()


def claimed(env):
    row = jobs.enqueue(env.db, env.instance, env.kind, env.policy, requested_by=env.actor.id)
    assert jobs.claim(env.db, row.id, "owner")
    return row


def prepared(env, row):
    target, findings = runner.prepare_findings(env.db, env.instance, policy=jobs.frozen_policy(row), frozen=True)
    assert target.id == row.instance_id
    assert env.db.query(DBSlowSQL).count() == 0
    assert isinstance(findings, runner.PreparedFindings)
    assert len(findings.staged_writes) == 1
    env.db.rollback()
    env.db.refresh(row)
    return findings


def persistence(findings):
    return next(item for item in findings if item.metric_name.endswith(".slow_sql.persistence"))


def verify_saved(env, job_id, count=2):
    read = run_readback.build_run_read(env.db, job_id)
    evidence = next(item["raw_data"] for item in read["results"]["items"]
                    if item["metric_name"].endswith(".slow_sql.persistence"))
    assert evidence["status"] == evidence["execution_status"] == "success"
    assert evidence["count"] == count
    assert len(evidence["record_ids"]) == count
    with env.factory() as verify:
        saved = verify.query(DBSlowSQL).all()
        assert {item.id for item in saved} == set(evidence["record_ids"])
        assert len(saved) == count
        assert all(item.db_type == env.kind for item in saved)
    assert read["results"]["task_status"] == read["report"]["task"]["task_status"]
    return read


def test_prepare_stages_without_writes_and_materialization_does_not_mutate_original(env):
    row = claimed(env)
    findings = prepared(env, row)
    original = deepcopy(persistence(findings).raw_data)
    published = runner.materialize_staged_writes(env.db, findings, instance_id=row.instance_id)
    assert persistence(published).raw_data["count"] == 2
    assert len(persistence(published).raw_data["record_ids"]) == 2
    assert persistence(findings).raw_data == original
    assert original["record_ids"] == [] and original["status"] == "pending"
    env.db.rollback()
    with env.factory() as verify:
        assert verify.query(DBSlowSQL).count() == 0
    assert runner._STAGED_SLOW_SQL.get() is None
    assert jobs.commit_results(env.db, row.id, "owner", findings, sync_report_artifact=False)
    read = verify_saved(env, row.id)
    assert env.db.query(ReportArtifact).count() == 0
    assert read["reports"]["state"] == "not_synced" and read["reports"]["artifacts"] == []
    assert "report_artifact_id" not in jobs.last_terminal_event(env.db.get(DBInspectionJob, row.id))
    assert persistence(findings).raw_data["record_ids"] == []


@pytest.mark.parametrize("entry", ["execute", "submit", "plan", "legacy"])
def test_all_execution_entries_publish_real_ids_from_the_same_staged_writers(env, entry):
    observed = []
    def guard(db):
        assert db is env.db
        count = db.query(DBSlowSQL).count()
        observed.append(count)
        if entry == "plan" and count:
            assert count == 2 and db.query(DBCheckResult).count() > 0
            assert db.query(ReportArtifact).count() == 1
            with env.factory() as outside:
                assert outside.query(DBSlowSQL).count() == outside.query(DBCheckResult).count() == 0
                assert outside.query(ReportArtifact).count() == 0
                assert outside.query(DBInspectionJob).one().status == "running"
    if entry == "execute":
        row = claimed(env)
        finished = jobs.execute(env.db, row, "owner", execution_guard=guard)
        verify_saved(env, finished.id)
        # 0 before collection, 0 before results exist, 2 after the report artifact is
        # published from the same transaction (the guard runs again before commit).
        assert observed == [0, 0, 2]
    elif entry == "submit":
        row = jobs.submit(env.db, env.instance, env.policy, requested_by=env.actor.id,
                          idempotency_key="staged-request", execution_guard=guard)
        verify_saved(env, row.id)
        replay = jobs.submit(env.db, env.instance, env.policy, requested_by=env.actor.id,
                             idempotency_key="staged-request", execution_guard=guard)
        assert replay.id == row.id and len(env.source_calls) == 1
        assert env.db.query(DBInspectionJob).count() == 1
    elif entry == "plan":
        ctx = SimpleNamespace(
            db=env.db, actor_id=env.actor.id, instance=env.instance, execution_id="staged-plan:1:1",
            execution_guard=guard,
            params=plan_adapter.prepare_params(env.db, env.instance, {"policy_id": env.policy.id}),
        )
        output = plan_adapter.execute(ctx)
        assert output["status"] == "success"
        verify_saved(env, output["job_id"])
        assert observed == [0, 0, 0, 0, 2]
        artifact = env.db.query(ReportArtifact).one()
        assert output["report"]["artifacts"][0]["id"] == artifact.id
        assert artifact.source_id == output["job_id"]
    else:
        task_id, rows = runner.run_inspection(env.db, env.instance, env.policy)
        assert rows and all(item.task_id == task_id for item in rows)
        saved = next(item.raw_data for item in rows if item.metric_name.endswith(".slow_sql.persistence"))
        assert saved["count"] == 2
        assert {item.id for item in env.db.query(DBSlowSQL).all()} == set(saved["record_ids"])
    if entry == "legacy":
        # The no-job compatibility helper writes rows but has no job row to index; it has no
        # production callers. Every real entry point must now agree on the report artifact.
        assert env.db.query(ReportArtifact).count() == 0
    else:
        artifact = env.db.query(ReportArtifact).one()
        assert artifact.source_id == (output["job_id"] if entry == "plan" else finished.id if entry == "execute" else row.id)


def test_default_template_keeps_populated_slow_sql_and_oracle_ranking_deduplication(env):
    default = next(item for item in policy_service.DEFAULT_POLICIES if item["policy_key"] == f"{env.kind}_standard")
    policy = policy_service._make_policy(default)
    env.db.add(policy)
    env.db.commit()
    ctx = SimpleNamespace(
        db=env.db, actor_id=env.actor.id, instance=env.instance, execution_id="default-plan:1:1",
        execution_guard=None,
        params=plan_adapter.prepare_params(env.db, env.instance, {"policy_id": policy.id}),
    )
    output = plan_adapter.execute(ctx)
    assert output["status"] in {"success", "partial"}
    read = verify_saved(env, output["job_id"])
    if env.kind == "oracle":
        template = next(item["raw_data"]["inspection_template"] for item in read["results"]["items"]
                        if item["metric_name"].endswith(".slow_sql.persistence"))
        assert template["oracle_profile"] == "noncdb"
        assert template["source_script"]["name"] == "dbcheck_noncdb.sql"


def test_result_insert_failure_rolls_back_auxiliary_rows_and_leaves_no_ghost_ids(env):
    row = claimed(env)
    findings = prepared(env, row)
    def fail(*args):
        raise RuntimeError("fixture result insert failure")
    event.listen(DBCheckResult, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="fixture result insert failure"):
            jobs.commit_results(env.db, row.id, "owner", findings)
    finally:
        event.remove(DBCheckResult, "before_insert", fail)
    with env.factory() as verify:
        assert verify.query(DBSlowSQL).count() == verify.query(DBCheckResult).count() == 0
        assert verify.get(DBInspectionJob, row.id).status == "running"
    assert persistence(findings).raw_data["record_ids"] == []
    assert jobs.commit_results(env.db, row.id, "owner", findings)
    verify_saved(env, row.id)


def test_writer_failure_preserves_partial_report_and_no_nonexistent_ids(env):
    row = claimed(env)
    findings = prepared(env, row)
    def fail(*args):
        raise RuntimeError("fixture slow SQL insert failure")
    event.listen(DBSlowSQL, "before_insert", fail)
    try:
        assert jobs.commit_results(env.db, row.id, "owner", findings)
    finally:
        event.remove(DBSlowSQL, "before_insert", fail)
    read = run_readback.build_run_read(env.db, row.id)
    assert read["run"]["status"] == read["report"]["task"]["task_status"] == "partial"
    evidence = next(item["raw_data"] for item in read["results"]["items"]
                    if item["metric_name"].endswith(".slow_sql.persistence"))
    assert evidence["count"] == 0 and evidence["record_ids"] == []
    assert evidence["status"] == "failed" and evidence["errors"]
    assert env.db.query(DBSlowSQL).count() == 0


def test_empty_source_still_publishes_successful_zero_count_not_a_failure(env):
    env.mysql_rows = []
    for source in ("top_sql_elapsed", "top_sql_buffer_gets", "top_sql_disk_reads"):
        env.oracle_metrics[source] = []
    row = jobs.submit(env.db, env.instance, env.policy, requested_by=env.actor.id)
    verify_saved(env, row.id, count=0)


def test_sqlite_select_only_writer_outer_rollback_removes_all_rows(env):
    db = env.db
    target = db.query(DBInstance).one()
    assert not db.connection().connection.driver_connection.in_transaction
    if env.kind == "mysql":
        saved = runner._save_mysql_slow_sql(db, target, {"slow_sql": env.mysql_rows})
    else:
        saved = runner._save_oracle_slow_sql(db, target, env.oracle_metrics)
    assert saved["status"] == "success" and len(saved["record_ids"]) == 2
    db.rollback()
    with env.factory() as verify:
        assert verify.query(DBSlowSQL).count() == 0


@pytest.mark.parametrize("branch", ["cancelled", "timed_out"])
@pytest.mark.parametrize("deny", [False, True])
def test_internal_rollback_terminal_branch_rechecks_the_same_session_guard(env, branch, deny):
    row = claimed(env)
    findings = prepared(env, row)
    if branch == "cancelled":
        jobs.cancel(env.db, row, requested_by=env.actor.id)
    else:
        row.started_at = jobs.utcnow() - timedelta(seconds=901)
        env.db.commit()
    calls, rollbacks = [], []
    event.listen(env.db, "after_rollback", lambda db: rollbacks.append(True))
    def guard(db):
        assert db is env.db
        calls.append(len(rollbacks))
        if len(calls) == 2:
            assert rollbacks
            if deny:
                raise ExecutionLeaseLost("fixture lost after internal rollback")
    if deny:
        with pytest.raises(ExecutionLeaseLost):
            jobs.commit_results(env.db, row.id, "owner", findings, execution_guard=guard)
    else:
        assert not jobs.commit_results(env.db, row.id, "owner", findings, execution_guard=guard)
    assert len(calls) == 2
    with env.factory() as verify:
        assert verify.query(DBSlowSQL).count() == verify.query(DBCheckResult).count() == 0
        assert verify.get(DBInspectionJob, row.id).status == ("running" if deny else branch)


def test_fence_stays_held_through_slow_sql_result_and_terminal_publication(env):
    row = claimed(env)
    findings = prepared(env, row)
    locked, writes = [False], []
    def committed(db):
        if not db.in_nested_transaction():
            locked[0] = False
    event.listen(env.db, "after_commit", committed)
    event.listen(env.db, "after_rollback", lambda db: locked.__setitem__(0, False))
    def guard(db):
        assert db is env.db
        locked[0] = True
    def inserted(mapper, connection, target):
        assert locked[0], "Publication escaped the parent fence"
        writes.append(type(target))
    event.listen(DBSlowSQL, "before_insert", inserted)
    event.listen(DBCheckResult, "before_insert", inserted)
    try:
        assert jobs.commit_results(env.db, row.id, "owner", findings, execution_guard=guard)
    finally:
        event.remove(DBSlowSQL, "before_insert", inserted)
        event.remove(DBCheckResult, "before_insert", inserted)
    assert DBSlowSQL in writes and DBCheckResult in writes
    assert not locked[0]
    verify_saved(env, row.id)


def test_revoked_actor_after_collection_prevents_auxiliary_and_result_writes(env):
    actor_id = env.actor.id
    def revoke():
        with env.factory() as other:
            other.execute(update(User).where(User.id == actor_id).values(status="inactive"))
            other.commit()
    env.action = revoke
    row = claimed(env)
    jobs.execute(env.db, row, "owner")
    read = run_readback.build_run_read(env.db, row.id)
    assert read["run"]["status"] == "failed"
    assert read["run"]["outcome_kind"] == "actor_inactive"
    assert env.db.query(DBSlowSQL).count() == env.db.query(DBCheckResult).count() == 0


def test_guard_false_never_becomes_an_authoritative_domain_failure(env):
    row = claimed(env)
    with pytest.raises(jobs.JobError) as refused:
        jobs.execute(env.db, row, "owner", execution_guard=lambda db: False)
    assert refused.value.code == "execution_guard_rejected"
    assert env.db.query(DBInspectionJob).one().status == "running"
    assert env.db.query(DBSlowSQL).count() == env.db.query(DBCheckResult).count() == 0


@pytest.mark.parametrize("when", ["result_insert", "final_commit"])
def test_baseexception_aborts_both_data_sets_without_turning_loss_into_failure(env, when):
    row = claimed(env)
    findings = prepared(env, row)
    def lose(*args):
        if when == "result_insert" or not env.db.in_nested_transaction():
            raise ExecutionLeaseLost("fixture publication ownership lost")
    target, name = (DBCheckResult, "before_insert") if when == "result_insert" else (env.db, "before_commit")
    event.listen(target, name, lose)
    try:
        with pytest.raises(ExecutionLeaseLost):
            jobs.commit_results(env.db, row.id, "owner", findings)
    finally:
        event.remove(target, name, lose)
    with env.factory() as verify:
        assert verify.query(DBSlowSQL).count() == verify.query(DBCheckResult).count() == 0
        assert verify.get(DBInspectionJob, row.id).status == "running"
    assert persistence(findings).raw_data["record_ids"] == []


def test_jobs_execute_refences_its_terminal_failure_after_result_insert_rollback(env):
    row = claimed(env)
    calls = []
    def guard(db):
        calls.append(True)
        if len(calls) == 3:
            raise ExecutionLeaseLost("fixture failure publication ownership lost")
    def fail(*args):
        raise RuntimeError("fixture check result cannot be written")
    event.listen(DBCheckResult, "before_insert", fail)
    try:
        with pytest.raises(ExecutionLeaseLost):
            jobs.execute(env.db, row, "owner", execution_guard=guard)
    finally:
        event.remove(DBCheckResult, "before_insert", fail)
    assert len(calls) == 3
    with env.factory() as verify:
        assert verify.query(DBSlowSQL).count() == verify.query(DBCheckResult).count() == 0
        assert verify.get(DBInspectionJob, row.id).status == "running"


def test_retry_forwards_guard_and_only_new_attempt_gets_real_slow_sql_ids(env):
    parent = claimed(env)
    jobs.finish_error(env.db, parent.id, "owner", "failed", "fixture first attempt")
    env.db.refresh(parent)
    calls = []
    child = jobs.retry(env.db, parent, execution_guard=lambda db: calls.append(db))
    assert child.parent_job_id == parent.id and child.attempt == 2
    assert calls == [env.db] * 5
    verify_saved(env, child.id)
    assert env.db.get(DBInspectionJob, parent.id).status == "failed"
    assert len(env.source_calls) == 1


def test_existing_sqlite_transaction_and_caller_changes_survive_oracle_and_mysql_savepoints(env):
    target = env.instance
    target.instance_name = "caller pending change"
    env.db.flush()
    if env.kind == "mysql":
        saved = runner._save_mysql_slow_sql(env.db, target, {"slow_sql": env.mysql_rows})
    else:
        saved = runner._save_oracle_slow_sql(env.db, target, env.oracle_metrics)
    assert saved["count"] == 2
    env.db.commit()
    with env.factory() as verify:
        assert verify.query(DBSlowSQL).count() == 2
        assert verify.get(DBInstance, target.id).instance_name == "caller pending change"
