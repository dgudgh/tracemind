"""Real isolated queue/results/readback; only collection and Oracle probe are fixtures."""
from contextlib import contextmanager
from copy import deepcopy
import json
import shutil
import socket
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, update
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.analyzers.rules import Finding
from app.models.access_control import AccessRole, RoleActionPermission, UserAccessRole, UserAssetScope
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.models.report_center import ReportArtifact
from app.models.slow_sql import DBSlowSQL
from app.models.user import User
from app.core.database import Base
from app.services import capability_handler, capability_registry, report_center
from app.services.inspection import execution_policy, jobs, plan_adapter as adapter, policy_service, report_builder, run_readback, runner
from app.services.inspection.collectors import postgresql_inspection_collector, sqlserver_inspection_collector
from inspection_fixture_signing import isolate_report_signing


KEYS = {
    "oracle": "oracle.tablespace.used_pct", "mysql": "mysql.connection.used_pct",
    "postgresql": "postgresql.connection.used_pct", "sqlserver": "sqlserver.database.used_pct",
}


def finding(engine="mysql", status="success"):
    return Finding(
        "fixture", "Fixture check", "normal", KEYS[engine], "1", "", "fixture evidence", "",
        {"execution_status": status, **({"error": "fixture connection refused"} if status == "failed"
                                       else {"sql": "SELECT 1 AS n", "rows": [{"n": 1}]})},
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    isolate_report_signing(tmp_path, monkeypatch)
    def deny_network(*args, **kwargs):
        pytest.fail("No live DB/model/Agent/network is allowed")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    engine = create_engine(f"sqlite:///{tmp_path / 'platform.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(jobs, "SessionLocal", factory)
    seed_calls, probes, collections = [], [], []
    monkeypatch.setattr(policy_service, "ensure_default_policies", lambda db: seed_calls.append(True))

    @contextmanager
    def oracle_probe(instance, **kwargs):
        probes.append(instance.id)
        yield object()

    monkeypatch.setattr(execution_policy, "oracle_connection", oracle_probe)
    monkeypatch.setattr(execution_policy, "detect_oracle_profile", lambda conn: SimpleNamespace(
        major_version=11, cdb="NO", version="11.2.0.4",
    ))
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    original = adapter.oracle_inspection_profile.SCRIPT_DIR
    for name in ("mysql_check.sh", "pg_check.sh", "mssql_check.sh", "dbcheck_noncdb.sql", "dbcheck10g.sql", "dbcheck_cdb.sql"):
        shutil.copyfile(original / name, script_dir / name)
    monkeypatch.setattr(adapter.oracle_inspection_profile, "SCRIPT_DIR", script_dir)
    monkeypatch.setattr(execution_policy, "SCRIPT_DIR", script_dir)

    state = SimpleNamespace(
        engine=engine, factory=factory, script_dir=script_dir, seed_calls=seed_calls,
        probes=probes, collections=collections, action=None, result_statuses=["success"],
    )

    def collect(instance, policy=None):
        collections.append(instance.db_type)
        if state.action:
            state.action()
        return [finding(instance.db_type, status) for status in state.result_statuses]

    def native_collect(db, instance, policy=None):
        return collect(instance, policy)

    monkeypatch.setattr(runner, "_collect_oracle", native_collect)
    monkeypatch.setattr(runner, "_collect_mysql", native_collect)
    monkeypatch.setattr(postgresql_inspection_collector, "collect", lambda instance, policy_items=None: collect(instance))
    monkeypatch.setattr(sqlserver_inspection_collector, "collect", lambda instance, policy_items=None: collect(instance))
    with factory() as db:
        actor = User(username="plan-fixture", password_hash="fixture-only", role="user", status="active")
        role = AccessRole(role_key="plan-fixture", name="Fixture", status="active")
        db.add_all([actor, role])
        db.flush()
        grant = RoleActionPermission(role_id=role.id, action_key="inspection.execute")
        scope = UserAssetScope(user_id=actor.id, scope_type="all", access_level="operate")
        db.add_all([grant, scope, UserAccessRole(user_id=actor.id, role_id=role.id)])
        instance = DBInstance(
            instance_name="plan-fixture", db_type="mysql", host="fixture.invalid", port=3306,
            username="fixture", password_cipher="fixture-unused", enabled=1,
        )
        policy = DBInspectionPolicy(
            policy_key="plan-fixture", name="Fixture", enabled=1, db_types_json=["mysql"],
            item_config_json=[{"key": KEYS["mysql"], "enabled": True}],
        )
        db.add_all([instance, policy])
        db.commit()
        state.db, state.actor, state.instance, state.policy = db, actor, instance, policy
        state.grant, state.scope = grant, scope
        yield state
    engine.dispose()


def context(env, *, execution_id="fixture-plan:1:1", guard=None):
    return SimpleNamespace(
        db=env.db, actor_id=env.actor.id, instance=env.instance,
        params=adapter.prepare_params(env.db, env.instance, {"policy_id": env.policy.id}),
        execution_id=execution_id, execution_guard=guard,
    )


def mutate(env, model, identity, **values):
    with env.factory() as other:
        other.execute(update(model).where(model.id == identity).values(**values))
        other.commit()


def empty_results(env, output):
    assert env.db.query(DBInspectionJob).count() == 1
    assert env.db.query(DBCheckResult).count() == 0
    assert output["status"] != "success"
    assert output["run"]["result_count"] == 0
    assert run_readback.build_run_read(env.db, output["job_id"])["evidence"]["state"] == "missing"
    assert output["report"]["available"] is False


@pytest.mark.parametrize("values", [
    {}, {"policy_id": True}, {"policy_id": "1"}, {"policy_id": 0}, {"policy_id": -1},
    {"policy_id": 1, "timeout_seconds": 29}, {"policy_id": 1, "timeout_seconds": 3601},
    {"policy_id": 1, "timeout_seconds": 30.0}, {"policy_id": 1, "sql": "SELECT 1"},
    {"policy_id": 1, "runtime_digest": "not-a-hash"},
])
def test_schema_strict_extra_forbidden(values):
    with pytest.raises(ValidationError):
        adapter.InspectionPlanParams.model_validate(values)


@pytest.mark.parametrize("timeout", [30, 900, 3600])
def test_timeout_bounds(timeout):
    assert adapter.InspectionPlanParams(policy_id=1, timeout_seconds=timeout).timeout_seconds == timeout


def test_prepare_and_validate_are_select_only_no_seeding_no_probe_no_sql_params(env):
    statements = []
    event.listen(env.engine, "before_cursor_execute", lambda conn, cursor, sql, *args: statements.append(sql))
    prepared = context(env).params
    assert prepared == adapter.validate_params(env.db, env.instance, prepared)
    assert set(prepared) == {"policy_id", "timeout_seconds", "policy_revision", "runtime_digest", "target_fingerprint"}
    assert all(len(prepared[key]) == 64 for key in ("policy_revision", "runtime_digest", "target_fingerprint"))
    assert env.seed_calls == env.probes == env.collections == []
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert env.db.query(DBInspectionPolicy).count() == 1
    assert "SELECT " not in json.dumps(prepared)
    assert "fixture-unused" not in json.dumps(prepared)


def test_caller_cannot_choose_controlled_hashes_or_execute_unfrozen(env):
    prepared = context(env).params
    with pytest.raises(adapter.InspectionPlanError) as rejected:
        adapter.prepare_params(env.db, env.instance, prepared)
    assert rejected.value.code == "inspection_frozen_fields_forbidden"
    with pytest.raises(adapter.InspectionPlanError) as rejected:
        adapter.validate_params(env.db, env.instance, {"policy_id": env.policy.id})
    assert rejected.value.code == "inspection_params_not_frozen"


@pytest.mark.parametrize("change,code", [
    ("missing", "template_missing"), ("disabled", "template_missing"),
    ("inapplicable", "inspection_policy_not_applicable"), ("empty", "inspection_template_not_executable"),
    ("unmapped", "inspection_template_not_executable"), ("script", "inspection_runtime_unavailable"),
    ("instance", "instance_disabled"), ("host", "inspection_engine_unsupported"),
])
def test_prepare_refuses_unusable_configuration_without_default_fallback(env, change, code):
    if change == "missing":
        env.db.delete(env.policy)
    elif change == "disabled":
        env.policy.enabled = 0
    elif change == "inapplicable":
        env.policy.db_types_json = ["postgresql"]
    elif change == "empty":
        env.policy.item_config_json = []
    elif change == "unmapped":
        env.policy.item_config_json = [{"key": "not-a-collector"}]
    elif change == "script":
        (env.script_dir / "mysql_check.sh").unlink()
    elif change == "instance":
        env.instance.enabled = 0
    elif change == "host":
        env.instance.db_type = "host"
    env.db.commit()
    with pytest.raises(adapter.InspectionPlanError) as rejected:
        adapter.prepare_params(env.db, env.instance, {"policy_id": 1})
    assert rejected.value.code == code
    assert env.seed_calls == env.probes == env.collections == []
    assert env.db.query(DBInspectionJob).count() == 0


@pytest.mark.parametrize("engine", KEYS)
def test_four_engines_real_queue_results_rule_report_readback_and_replay(env, engine):
    env.instance.db_type = engine
    env.policy.db_types_json = [engine]
    env.policy.item_config_json = [{"key": KEYS[engine], "enabled": True}]
    env.policy.report_config_json = {"oracle_profile": "noncdb"} if engine == "oracle" else {}
    env.db.commit()
    ctx = context(env)
    output = adapter.execute(ctx)
    replay = adapter.execute(ctx)
    assert output["schema"] == "inspection.plan_result/v1"
    assert output["status"] == output["outcome"] == "success"
    assert replay["job_id"] == output["job_id"]
    assert env.collections == [engine]
    assert env.db.query(DBInspectionJob).count() == env.db.query(DBCheckResult).count() == 1
    read = run_readback.build_run_read(env.db, output["job_id"])
    for key, value in output["run"].items():
        assert value == (read["results"]["total_count"] if key == "result_count" else read["run"][key])
    assert output["run"]["policy_revision"] != ctx.params["policy_revision"]
    artifact = env.db.query(ReportArtifact).one()
    assert output["report"] == {
        "available": True, "state": "synced",
        "artifacts": [{"id": artifact.id, "status": "success", "archive_state": "active"}],
    }
    assert replay["report"] == output["report"]
    assert artifact.source_key == f"source:inspection:{output['job_id']}"
    assert artifact.source_id == output["job_id"] and artifact.instance_id == env.instance.id
    assert report_center.verify_artifact(artifact)["valid"]
    assert read["reports"]["artifacts"][0]["id"] == artifact.id
    assert read["report"]["report"]["analysis_source"] == "rule_engine"
    assert env.db.query(DBCheckResult).one().db_type == engine


@pytest.mark.parametrize("statuses,expected", [(["failed"], "failed"), (["success", "failed"], "partial")])
def test_collector_failure_and_partial_are_not_success(env, statuses, expected):
    env.result_statuses = statuses
    ctx = context(env)
    output = adapter.execute(ctx)
    assert output["status"] == output["outcome"] == expected
    read = run_readback.build_run_read(env.db, output["job_id"])
    assert read["results"]["task_status"] == expected
    assert read["report"]["task"]["task_status"] == expected
    assert adapter.execute(ctx)["job_id"] == output["job_id"]
    assert env.collections == ["mysql"]


def test_connection_exception_uses_existing_runner_degraded_result(env):
    def fail():
        raise ConnectionError("fixture target unavailable")
    env.action = fail
    output = adapter.execute(context(env))
    assert output["status"] == "failed"
    assert output["run"]["result_count"] == 1
    assert "fixture target unavailable" in str(run_readback.build_run_read(env.db, output["job_id"])["results"])


@pytest.mark.parametrize("phase", ["before", "collecting"])
def test_concurrent_template_edit_is_not_silently_substituted(env, phase):
    ctx = context(env)
    def edit():
        mutate(env, DBInspectionPolicy, env.policy.id, threshold_config_json={"connection_usage_pct": 1})
    if phase == "before":
        edit()
        with pytest.raises(adapter.InspectionPlanError) as rejected:
            adapter.execute(ctx)
        assert rejected.value.code == "inspection_policy_changed"
        assert env.db.query(DBInspectionJob).count() == 0
    else:
        env.action = edit
        output = adapter.execute(ctx)
        empty_results(env, output)
        assert output["error_code"] == "inspection_policy_changed"


@pytest.mark.parametrize("phase", ["before", "collecting"])
def test_script_bytes_change_same_name_is_refused(env, phase):
    ctx = context(env)
    def edit():
        path = env.script_dir / "mysql_check.sh"
        path.write_bytes(path.read_bytes() + b"\n# fixture concurrent edit\n")
    if phase == "before":
        edit()
        with pytest.raises(adapter.InspectionPlanError) as rejected:
            adapter.execute(ctx)
        assert rejected.value.code == "inspection_runtime_changed"
    else:
        env.action = edit
        output = adapter.execute(ctx)
        empty_results(env, output)
        assert output["error_code"] == "inspection_runtime_changed"


@pytest.mark.parametrize("change,code", [
    ("permission", "permission_revoked"), ("scope", "scope_revoked"),
    ("actor", "actor_inactive"), ("instance", "instance_disabled"),
    ("target", "target_changed"), ("policy", "template_missing"),
])
def test_current_authority_and_target_rechecked_before_publish(env, change, code):
    ctx = context(env)
    def revoke():
        if change == "permission":
            mutate(env, RoleActionPermission, env.grant.id, action_key="inspection.read")
        elif change == "scope":
            mutate(env, UserAssetScope, env.scope.id, scope_type="instance", scope_id=999999)
        elif change == "actor":
            mutate(env, User, env.actor.id, status="inactive")
        elif change == "instance":
            mutate(env, DBInstance, env.instance.id, enabled=0)
        elif change == "target":
            mutate(env, DBInstance, env.instance.id, host="changed.invalid")
        else:
            mutate(env, DBInspectionPolicy, env.policy.id, enabled=0)
    env.action = revoke
    output = adapter.execute(ctx)
    empty_results(env, output)
    assert output["error_code"] == code


@pytest.mark.parametrize("deny_call", [1, 2, 3, 4, 5])
def test_guard_covers_pre_enqueue_pre_claim_pre_collect_and_pre_publish(env, deny_call):
    calls = []
    def guard():
        calls.append(True)
        return len(calls) != deny_call
    ctx = context(env, guard=guard)
    with pytest.raises(adapter.InspectionPlanError) as rejected:
        adapter.execute(ctx)
    assert rejected.value.code == "inspection_execution_guard_rejected"
    assert env.db.query(DBInspectionJob).count() == (0 if deny_call == 1 else 1)
    if deny_call >= 3:
        assert env.db.query(DBInspectionJob).one().status == "running"
    assert len(env.collections) == (1 if deny_call >= 4 else 0)
    assert env.db.query(DBCheckResult).count() == 0
    assert env.db.query(ReportArtifact).count() == 0


def test_guard_db_argument_receives_the_publication_session(env):
    sessions, staged = [], []
    def guard(db):
        sessions.append(db)
        counts = (db.query(DBCheckResult).count(), db.query(ReportArtifact).count())
        staged.append(counts)
        if counts != (0, 0):
            with env.factory() as outside:
                assert outside.query(DBCheckResult).count() == outside.query(ReportArtifact).count() == 0
                assert outside.query(DBInspectionJob).one().status == "running"
    output = adapter.execute(context(env, guard=guard))
    assert output["status"] == "success"
    assert sessions == [env.db] * 5
    assert staged == [(0, 0)] * 4 + [(1, 1)]


@pytest.mark.parametrize("deny_call", [4, 5])
def test_worker_baseexception_lease_loss_unwinds_without_publishing_failure(env, deny_call):
    class ExecutionLeaseLost(BaseException):
        pass

    calls = []
    def guard(db):
        calls.append(db)
        if len(calls) == deny_call:
            raise ExecutionLeaseLost("fixture parent lease lost")

    with pytest.raises(ExecutionLeaseLost):
        adapter.execute(context(env, guard=guard))
    assert env.collections == ["mysql"]
    assert env.db.query(DBCheckResult).count() == 0
    assert env.db.query(ReportArtifact).count() == 0
    job = env.db.query(DBInspectionJob).one()
    assert job.status == "running"
    assert job.failure_reason is None
    assert run_readback.build_run_read(env.db, job.id)["report"] is None


def test_validation_failure_also_requires_guard_before_failure_commit(env):
    calls = []
    def guard(db):
        calls.append(db)
        if len(calls) == 4:
            return False
    ctx = context(env, guard=guard)
    env.action = lambda: mutate(env, DBInspectionPolicy, env.policy.id, enabled=0)
    with pytest.raises(adapter.InspectionPlanError) as rejected:
        adapter.execute(ctx)
    assert rejected.value.code == "inspection_execution_guard_rejected"
    assert env.db.query(DBInspectionJob).one().status == "running"
    assert env.db.query(DBCheckResult).count() == 0


def test_late_job_owner_cannot_publish_or_rewrite_new_owner(env):
    ctx = context(env)
    def replace_owner():
        row = env.db.query(DBInspectionJob).one()
        mutate(env, DBInspectionJob, row.id, owner_token="replacement")
    env.action = replace_owner
    output = adapter.execute(ctx)
    empty_results(env, output)
    assert output["status"] == "running"
    assert output["outcome"] != "success"
    assert env.db.query(DBInspectionJob).one().owner_token == "replacement"


def test_cancel_during_collection_uses_existing_queue_cancel_semantics(env):
    ctx = context(env)
    def cancel():
        with env.factory() as other:
            jobs.cancel(other, other.query(DBInspectionJob).one(), requested_by=env.actor.id)
    env.action = cancel
    output = adapter.execute(ctx)
    empty_results(env, output)
    assert output["status"] == "cancelled"
    assert output["error_code"] == "cancelled_during_run"


def test_timed_out_run_remains_unknown_not_success(env):
    ctx = context(env)
    def timeout():
        from datetime import timedelta
        row = env.db.query(DBInspectionJob).one()
        mutate(env, DBInspectionJob, row.id, started_at=jobs.utcnow() - timedelta(seconds=901))
    env.action = timeout
    output = adapter.execute(ctx)
    empty_results(env, output)
    assert output["status"] == "timed_out"
    assert output["outcome"] == "unknown"


def test_execution_identity_required_and_ordinary_read_context_cannot_run(env):
    ctx = capability_handler.CapabilityContext(db=env.db, actor_id=env.actor.id,
                                              instance=env.instance, params=context(env).params)
    with pytest.raises(adapter.InspectionPlanError) as rejected:
        adapter.execute(ctx)
    assert rejected.value.code == "inspection_execution_id_required"
    assert env.db.query(DBInspectionJob).count() == 0


def test_ordinary_execute_read_cannot_invoke_the_write_capability(env):
    with pytest.raises(capability_registry.CapabilityError):
        capability_registry.execute_read(
            env.db, "inspection.run.execute", 1,
            actor={"user_id": env.actor.id, "role": "user", "source": "db"},
            instance_id=env.instance.id, params=context(env).params,
        )
    assert env.collections == []
    assert env.db.query(DBInspectionJob).count() == 0


@pytest.mark.parametrize("profile,major,cdb", [("10g", 10, None), ("noncdb", 11, "NO"), ("cdb", 19, "YES")])
def test_oracle_auto_freezes_dispatch_and_resolves_actual_profile_at_execution(env, monkeypatch, profile, major, cdb):
    env.instance.db_type = "oracle"
    env.policy.db_types_json = ["oracle"]
    env.policy.item_config_json = [{"key": KEYS["oracle"], "enabled": True}]
    env.policy.report_config_json = {"oracle_profile": "auto"}
    env.db.commit()
    monkeypatch.setattr(execution_policy, "detect_oracle_profile", lambda conn: SimpleNamespace(
        major_version=major, cdb=cdb, version=str(major),
    ))
    ctx = context(env)
    assert env.probes == []
    output = adapter.execute(ctx)
    assert output["status"] == "success"
    read = run_readback.build_run_read(env.db, output["job_id"])
    metadata = read["results"]["items"][0]["raw_data"]["inspection_template"]
    assert metadata["oracle_profile"] == profile
    assert metadata["source_script"]["name"] == adapter.oracle_inspection_profile.SCRIPT_CATALOG[profile]["script_name"]
    assert len(metadata["source_script"]["sha256"]) == 64
    assert adapter.execute(ctx)["job_id"] == output["job_id"]


def test_oracle_auto_binds_unselected_candidate_content_and_requires_all_candidates(env):
    env.instance.db_type = "oracle"
    env.policy.db_types_json = ["oracle"]
    env.policy.item_config_json = [{"key": KEYS["oracle"], "enabled": True}]
    env.db.commit()
    ctx = context(env)
    path = env.script_dir / "dbcheck_cdb.sql"
    path.write_bytes(path.read_bytes() + b"\n-- changed unselected candidate\n")
    with pytest.raises(adapter.InspectionPlanError) as rejected:
        adapter.validate_params(env.db, env.instance, ctx.params)
    assert rejected.value.code == "inspection_runtime_changed"
    path.unlink()
    with pytest.raises(adapter.InspectionPlanError) as rejected:
        adapter.prepare_params(env.db, env.instance, {"policy_id": env.policy.id})
    assert rejected.value.code == "inspection_runtime_unavailable"
    assert env.probes == []


def test_oracle_auto_unknown_profile_is_failed_by_existing_runtime_rule(env, monkeypatch):
    env.instance.db_type = "oracle"
    env.policy.db_types_json = ["oracle"]
    env.policy.item_config_json = [{"key": KEYS["oracle"], "enabled": True}]
    env.db.commit()
    monkeypatch.setattr(execution_policy, "detect_oracle_profile", lambda conn: SimpleNamespace(
        major_version=None, cdb=None, version="unknown fixture version",
    ))
    output = adapter.execute(context(env))
    assert output["status"] == "failed"
    assert output["run"]["result_count"] == 1
    assert env.collections == []


def test_new_revision_allows_new_run_but_same_id_different_binding_is_refused(env):
    ctx = context(env)
    first = adapter.execute(ctx)
    new = SimpleNamespace(**vars(ctx),)
    new.execution_id = "fixture-plan:1:2"
    second = adapter.execute(new)
    assert first["job_id"] != second["job_id"]
    changed = deepcopy(ctx.params)
    changed["timeout_seconds"] = 30
    ctx.params = changed
    with pytest.raises(jobs.JobError):
        adapter.execute(ctx)
    assert env.collections == ["mysql", "mysql"]


def test_replay_cannot_rebind_changed_runtime_under_same_execution_id(env):
    ctx = context(env)
    adapter.execute(ctx)
    path = env.script_dir / "mysql_check.sh"
    path.write_bytes(path.read_bytes() + b"\n# new runtime\n")
    ctx.params = adapter.prepare_params(env.db, env.instance, {"policy_id": env.policy.id})
    with pytest.raises(adapter.InspectionPlanError) as rejected:
        adapter.execute(ctx)
    assert rejected.value.code == "inspection_idempotency_conflict"
    assert env.collections == ["mysql"]


def test_loaded_sql_map_change_is_bound_even_when_shell_script_unchanged(env, monkeypatch):
    from app.collectors.mysql import mysql_health_collector

    ctx = context(env)
    monkeypatch.setitem(mysql_health_collector.MYSQL_QUERIES, "slow_sql", "SELECT 'changed fixture SQL'")
    with pytest.raises(adapter.InspectionPlanError) as rejected:
        adapter.execute(ctx)
    assert rejected.value.code == "inspection_runtime_changed"
    assert env.collections == []


def test_reentrant_duplicate_delivery_reads_running_job_without_recollection(env):
    ctx = context(env)
    replays = []
    env.action = lambda: replays.append(adapter.execute(ctx))
    finished = adapter.execute(ctx)
    assert finished["status"] == "success"
    assert replays[0]["status"] == "running"
    assert replays[0]["job_id"] == finished["job_id"]
    assert replays[0]["run"]["result_count"] == 0
    assert env.collections == ["mysql"]
    assert env.db.query(DBInspectionJob).count() == env.db.query(DBCheckResult).count() == 1


def test_template_deleted_while_collecting_has_real_zero_result_run_readback(env):
    ctx = context(env)
    def delete():
        with env.factory() as other:
            other.delete(other.get(DBInspectionPolicy, env.policy.id))
            other.commit()
    env.action = delete
    output = adapter.execute(ctx)
    empty_results(env, output)
    assert output["error_code"] == "template_missing"
    assert output["run"]["status"] == run_readback.build_run_read(env.db, output["job_id"])["run"]["status"]


def test_oracle_explicit_profile_mismatch_fails_without_collector_fallback(env):
    env.instance.db_type = "oracle"
    env.policy.db_types_json = ["oracle"]
    env.policy.item_config_json = [{"key": KEYS["oracle"], "enabled": True}]
    env.policy.report_config_json = {"oracle_profile": "cdb"}
    env.db.commit()
    output = adapter.execute(context(env))
    assert output["status"] == "failed"
    assert env.collections == []
    assert len(env.probes) == 1
    assert output["run"]["result_count"] == 1


def test_custom_sql_is_hashed_not_copied_into_plan_parameters(env):
    env.policy.item_config_json = [{
        "key": "custom-metric", "source_type": "database_sql", "db_type": "mysql",
        "sql": "SELECT 1 AS metric_value", "value_type": "number",
        "warning_value": 10, "critical_value": 20,
    }]
    env.db.commit()
    prepared = context(env).params
    assert "SELECT" not in json.dumps(prepared)
    assert "sql" not in prepared
    mutate(env, DBInspectionPolicy, env.policy.id, item_config_json=[{
        **env.policy.item_config_json[0], "sql": "SELECT 2 AS metric_value",
    }])
    with pytest.raises(adapter.InspectionPlanError) as rejected:
        adapter.validate_params(env.db, env.instance, prepared)
    assert rejected.value.code == "inspection_policy_changed"


def test_large_findings_and_markdown_never_enter_the_plan_result(env, monkeypatch):
    large = finding()
    large.raw_data["rows"] = [{"large": "fixture " * 300000}]
    monkeypatch.setattr(runner, "_collect_mysql", lambda db, instance, policy=None: [large])
    def forbid_report_materialization(*args, **kwargs):
        pytest.fail("Plan result must not materialize the full report")
    monkeypatch.setattr(report_builder, "build_report_payload", forbid_report_materialization)
    output = adapter.execute(context(env))
    assert len(json.dumps(env.db.query(DBCheckResult).one().raw_data)) > 1024 * 1024
    encoded = json.dumps(output)
    assert len(encoded.encode()) < 8192
    assert "rows" not in encoded and "SELECT" not in encoded
    assert "findings" not in encoded and "markdown" not in encoded
    assert "progress" not in output["run"]
    assert output["run"]["result_count"] == 1


def test_report_reference_only_exposes_existing_artifact_id_and_archive_state(env):
    ctx = context(env)
    output = adapter.execute(ctx)
    artifact = env.db.query(ReportArtifact).one()
    assert output["report"] == {
        "available": True, "state": "synced",
        "artifacts": [{"id": artifact.id, "status": "success", "archive_state": "active"}],
    }
    artifact.content_markdown = "large fixture " * 100000
    artifact.source_export_url = "https://fixture.invalid/do-not-follow"
    report_center.sign_artifact(artifact)
    report_center.archive_artifact(env.db, artifact, actor="fixture", archive_uri="/private/fixture-do-not-expose")
    assert report_center.verify_artifact(artifact)["valid"]
    replay = adapter.execute(ctx)
    assert replay["report"] == {
        "available": True, "state": "synced",
        "artifacts": [{"id": artifact.id, "status": "success", "archive_state": "archived"}],
    }
    assert env.db.query(ReportArtifact).count() == 1
    encoded = json.dumps(replay)
    assert len(encoded.encode()) < 8192
    assert "fixture-do-not-expose" not in encoded
    assert "fixture.invalid/do-not-follow" not in encoded


def test_parameter_runtime_checks_and_collection_are_outside_guard_locks(env, monkeypatch):
    locked, validations = [False], []
    event.listen(env.db, "after_commit", lambda db: locked.__setitem__(0, False))
    event.listen(env.db, "after_rollback", lambda db: locked.__setitem__(0, False))
    original = adapter._runtime_digest
    def validate_without_lock(*args):
        assert not locked[0], "Runtime filesystem validation must not hold the publication lock"
        validations.append(True)
        return original(*args)
    monkeypatch.setattr(adapter, "_runtime_digest", validate_without_lock)
    def guard(db):
        assert db is env.db
        locked[0] = True
    def collect():
        assert not locked[0], "Collection must not retain the pre-I/O ownership lock"
    env.action = collect
    output = adapter.execute(context(env, guard=guard))
    assert output["status"] == "success"
    assert len(validations) == 4  # prepare, execution preflight, pre-collection, pre-publication
    assert not locked[0]


def test_existing_slow_sql_sidewrite_is_not_silently_rolled_back_then_claimed_saved(env):
    saved = []
    def persist_sidewrite():
        saved.append(runner._save_mysql_slow_sql(env.db, env.instance, {"slow_sql": [{
            "digest": "fixture-sql-id", "digest_text": "SELECT 1", "count_star": 1,
            "avg_seconds": 0.1, "total_seconds": 0.1,
        }]}))
    env.action = persist_sidewrite
    output = adapter.execute(context(env))
    assert saved[0]["status"] == "success" and saved[0]["record_ids"]
    empty_results(env, output)
    assert output["error_code"] == "inspection_collector_pending_writes"
    assert env.db.query(DBSlowSQL).count() == 0


def test_collection_read_snapshot_ends_before_fresh_validation(env, monkeypatch):
    rollbacks = []
    event.listen(env.db, "after_rollback", lambda db: rollbacks.append(len(env.collections)))
    original = adapter._validated
    def fresh_validation(*args):
        if env.collections:
            assert rollbacks and rollbacks[-1] == 1
        return original(*args)
    monkeypatch.setattr(adapter, "_validated", fresh_validation)
    output = adapter.execute(context(env))
    assert output["status"] == "success"
    assert rollbacks == [1]
