from __future__ import annotations
import json
import os
import random
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
import psutil
import pytest
from app.runtime.engine_host.packages import build_fixture_package
from app.runtime.engine_host.supervisor import FixtureSupervisor, SupervisorError
from app.runtime.engine_host.lifecycle import CrashPolicy, State
from app.runtime.engine_sdk.contracts import digest_json, decode_result, SdkError


def wait_for(predicate, timeout=4):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if predicate():return
        time.sleep(.005)
    assert predicate(), 'condition did not become true'


def host(tmp_path, **kwargs):
    package=build_fixture_package(tmp_path)
    supervisor=FixtureSupervisor(workdir=tmp_path/'host',restart=False,**kwargs)
    supervisor.start(package.source,package.manifest,package.signature,package.trusted_keys,
        expected_namespace='tmengine.test',expected_channel='test',store_path=tmp_path/'effects.db')
    return package,supervisor


def test_eight_inflight_multiplex_without_crossed_responses(tmp_path):
    package,s=host(tmp_path)
    try:
        jobs=[s.submit('invoke_read',{'echo':str(i),'sleep':.03}) for i in range(40)]
        values=[f.result(timeout=5) for f in jobs]
        assert [v['values']['echo'] for v in values]==[str(i) for i in range(40)]
        assert len({v['context']['operation_id'] for v in values})==40
        assert all(v['context']['package_digest']==package.digest for v in values)
        assert s.heartbeat()['executed']==40
    finally:s.stop()


def test_queue_is_bounded_cancel_frees_slot_and_deadline_never_dispatches(tmp_path):
    _,s=host(tmp_path,max_queue=2,max_inflight=1)
    try:
        running=s.submit('invoke_read',{'sleep':.25})
        wait_for(lambda:s.status()['inflight']==1)
        queued=s.submit('invoke_read',{'echo':'cancelled'})
        expired=s.submit('invoke_read',{'echo':'expired'},timeout=.03)
        with pytest.raises(SupervisorError,match='queue_full'):
            s.submit('invoke_read',{'echo':'must-not-run'})
        assert queued.cancel()
        replacement=s.submit('invoke_read',{'echo':'replacement'})
        with pytest.raises(SupervisorError,match='queued_deadline_exceeded'):
            expired.result(timeout=2)
        running.result(timeout=2)
        assert replacement.result(timeout=2)['values']['echo']=='replacement'
        assert s.heartbeat()['executed']==2
    finally:s.stop()


def test_running_cooperative_cancel_and_read_deadline(tmp_path):
    _,s=host(tmp_path)
    try:
        future=s.submit('invoke_read',{'sleep':1})
        wait_for(lambda:s.heartbeat()['inflight']==1)
        assert s.cancel(future.request_id)
        with pytest.raises(SupervisorError) as error:future.result(timeout=2)
        assert error.value.code=='call_cancelled' and error.value.effect_state=='none'
        with pytest.raises(SupervisorError,match='call_timeout_reconcile_required'):
            s.call('invoke_read',{'sleep':1},timeout=.03)
        assert s.call('selftest')['ok']
    finally:s.stop()


def test_uncooperative_call_terminates_owned_process_tree(tmp_path):
    _,s=host(tmp_path,cancellation_grace=.03)
    pid=s.record.pid
    try:
        future=s.submit('invoke_read',{'sleep':1,'ignore_cancel':True,'spawn_child':True},timeout=.08)
        wait_for(lambda:len(psutil.Process(pid).children())==1)
        child=psutil.Process(pid).children()[0]
        with pytest.raises(SupervisorError,match='call_timeout_reconcile_required'):
            future.result(timeout=3)
        wait_for(lambda:not psutil.pid_exists(pid) or psutil.Process(pid).status()==psutil.STATUS_ZOMBIE)
        wait_for(lambda:not child.is_running() or child.status()==psutil.STATUS_ZOMBIE)
    finally:s.stop()
    assert not s.cleanup_report()['processes_remaining']


class ShortWaitPolicy(CrashPolicy):
    def delay(self,now=None):
        value=super().delay(now)
        return None if value is None else .02


def test_real_crashes_consume_policy_open_circuit_and_manual_reset(tmp_path):
    package=build_fixture_package(tmp_path)
    policy=ShortWaitPolicy(rng=random.Random(2))
    s=FixtureSupervisor(workdir=tmp_path/'host',crash_policy=policy,restart=True)
    try:
        s.start(package.source,package.manifest,package.signature,package.trusted_keys,
            expected_namespace='tmengine.test',expected_channel='test')
        for count in range(6):
            old=s.record.launch_instance_id
            s.kill()
            if count<5:wait_for(lambda:s.state==State.READY and s.record.launch_instance_id!=old)
            else:wait_for(lambda:s.state==State.QUARANTINED)
        assert len(policy.crashes)==6 and policy.circuit_until is not None
        with pytest.raises(SupervisorError):s.call('selftest')
        s.manual_reset()
        assert s.call('selftest')['ok'] and policy.circuit_until is None
        assert len([x for x in s.events if x['event']=='crash'])==6
    finally:s.stop()


def test_output_limit_streamed_result_and_unknown_optional_roundtrip(tmp_path):
    _,s=host(tmp_path,max_output_bytes=1024*1024)
    try:
        result=s.call('invoke_read',{'output_bytes':700000},return_envelope=True,
                      optional_fields={'future_feature':{'x':1}})
        assert len(result['values']['blob'])==700000
        assert result['optional_fields']=={'future_feature':{'x':1}}
        with pytest.raises(SupervisorError,match='output_limit_exceeded'):
            s.call('invoke_read',{'output_bytes':1024*1024+1})
        with pytest.raises(SupervisorError,match='frame_too_large'):
            s.call('invoke_read',{'echo':'x'*(1024*1024)})
    finally:s.stop()
    assert not s.cleanup_report()['temporary_artifacts_remaining']


@pytest.mark.parametrize('params',[[],{'sleep':True},{'shell':'id'},{'output_bytes':True}])
def test_sdk_dispatch_rejects_bad_params_before_provider(tmp_path,params):
    _,s=host(tmp_path)
    try:
        with pytest.raises(SupervisorError):s.call('invoke_read',params)
        assert s.heartbeat()['executed']==0
    finally:s.stop()


def test_forged_input_digest_cannot_disable_effect_idempotency(tmp_path):
    _,s=host(tmp_path)
    try:
        with pytest.raises(SupervisorError,match='input_digest_mismatch'):
            s.call('execute',{'value':'first'},context={'operation_id':'same','input_digest':digest_json({})})
        first=s.call('execute',{'value':'first'},context={'operation_id':'same'})
        assert first['value']=='first'
        with pytest.raises(SupervisorError,match='effect_idempotency_conflict'):
            s.call('execute',{'value':'second'},context={'operation_id':'same'})
    finally:s.stop()


def test_cancel_after_commit_reports_completed_effect_and_verify(tmp_path):
    _,s=host(tmp_path)
    params={'value':'committed','reply_delay':.5}
    try:
        future=s.submit('execute',params,context={'operation_id':'committed-once'})
        def committed():
            try:
                with sqlite3.connect(tmp_path/'effects.db') as db:
                    return db.execute('SELECT count(*) FROM provider_effects').fetchone()[0]==1
            except sqlite3.OperationalError:return False
        wait_for(committed)
        s.cancel(future.request_id)
        with pytest.raises(SupervisorError) as error:future.result(timeout=2)
        assert error.value.effect_state=='completed'
        receipt=s.call('verify',{},context={'operation_id':'committed-once','input_digest':digest_json(params)})
        assert receipt['found'] and receipt['result']['value']=='committed'
    finally:s.stop()


@pytest.mark.parametrize('updates,code',[
    ({'os':'wrong-os'},'runtime_abi_mismatch'),
    ({'entrypoint_id':'arbitrary.module'},'entrypoint_invalid'),
    ({'sdk_version':'0.8'},'contract_version_unsupported'),
    ({'runtime_digest':'sha256:'+'0'*64},'runtime_identity_mismatch'),
])
def test_signed_incompatible_package_is_rejected_before_spawn(tmp_path,updates,code):
    package=build_fixture_package(tmp_path,manifest_updates=updates)
    s=FixtureSupervisor(workdir=tmp_path/'host')
    try:
        with pytest.raises(SupervisorError,match=code):
            s.start(package.source,package.manifest,package.signature,package.trusted_keys,
                expected_namespace='tmengine.test',expected_channel='test')
        assert not s.owned_resources()
    finally:s.stop()


def test_stage_tamper_between_verify_and_launch_is_rejected(tmp_path):
    package=build_fixture_package(tmp_path)
    def hook(phase,detail):
        if phase=='staged':
            path=Path(detail['staging_dir'])/'fake_provider.py'
            os.chmod(path,0o600);path.write_text('raise RuntimeError("must not execute")')
    s=FixtureSupervisor(workdir=tmp_path/'host',phase_hook=hook)
    try:
        with pytest.raises(ValueError):
            s.start(package.source,package.manifest,package.signature,package.trusted_keys,
                expected_namespace='tmengine.test',expected_channel='test')
        assert not s.owned_resources()
    finally:s.stop()


def test_wrong_peer_pid_fails_closed_and_cleans_launch(tmp_path,monkeypatch):
    from app.runtime.engine_sdk import wire
    package=build_fixture_package(tmp_path)
    original=wire.peer_credentials
    def wrong(sock):
        peer=original(sock)
        return wire.PeerCredentials(peer.uid,peer.gid,peer.pid+100000)
    monkeypatch.setattr(wire,'peer_credentials',wrong)
    s=FixtureSupervisor(workdir=tmp_path/'host',restart=False)
    try:
        with pytest.raises(SupervisorError,match='peer_identity_mismatch'):
            s.start(package.source,package.manifest,package.signature,package.trusted_keys,
                expected_namespace='tmengine.test',expected_channel='test')
    finally:s.stop()
    assert not s.cleanup_report()['processes_remaining'] and not s.cleanup_report()['sockets_remaining']


def test_result_identity_and_mandatory_bits_are_not_guessed(tmp_path):
    from app.runtime.engine_sdk.contracts import validate_envelope
    _,s=host(tmp_path)
    try:
        result=s.call('invoke_read',return_envelope=True)
        wrong={**result,'provider_version':'wrong'}
        with pytest.raises(SdkError,match='response_identity_mismatch'):decode_result(wrong)
        with pytest.raises(SdkError):decode_result({**result,'unexpected':True})
        with pytest.raises(SdkError,match='sdk_mandatory_unsupported'):
            validate_envelope({'schema':'engine.rpc/v1','request_id':'r','method':'selftest','params':{},
                               'required_fields':['future-mandatory']})
    finally:s.stop()


def test_s_minus_one_real_provider_and_core_decoder_after_removal(tmp_path):
    package=build_fixture_package(tmp_path,sdk_version='0.9')
    s=FixtureSupervisor(workdir=tmp_path/'host',restart=False)
    try:
        s.start(package.source,package.manifest,package.signature,package.trusted_keys,
            expected_namespace='tmengine.test',expected_channel='test')
        result=s.call('invoke_read',return_envelope=True)
        assert result['context']['sdk_version']=='0.9'
    finally:s.stop()
    import shutil
    shutil.rmtree(package.source)
    assert decode_result(json.dumps(result).encode())['values']['marker']=='v1'


def test_invalid_heartbeat_quarantines_live_worker(tmp_path,monkeypatch):
    _,s=host(tmp_path)
    original=s.call
    def invalid(method,*args,**kwargs):
        if method=='heartbeat':
            return {'launch_instance_id':s.identity['launch_instance_id'],'queue':0,
                    'inflight':-1,'connections':1,'target_connections':0}
        return original(method,*args,**kwargs)
    monkeypatch.setattr(s,'call',invalid)
    try:
        with pytest.raises(SupervisorError,match='heartbeat_count_invalid'):s.heartbeat()
        assert s.state==State.QUARANTINED
        with pytest.raises(SupervisorError):s.submit('invoke_read',{})
    finally:s.stop()


def test_stale_deadline_is_rejected_before_dispatch(tmp_path):
    _,s=host(tmp_path)
    try:
        with pytest.raises(SupervisorError,match='deadline_exceeded'):
            s.call('invoke_read',{},context={'deadline':time.time()-1})
        assert s.heartbeat()['executed']==0
        with pytest.raises(SupervisorError,match='call_timeout_reconcile_required'):
            s.call('invoke_read',{'sleep':1},timeout=.03,context={'deadline':time.time()+300})
    finally:s.stop()


def test_thirty_warm_restarts_reuse_immutable_stage_without_stale_reader_failure(tmp_path):
    _,s=host(tmp_path)
    staging=s.record.staging_dir
    launches={s.record.launch_instance_id}
    try:
        for index in range(30):
            s.drain()
            s.restart()
            assert s.record.staging_dir==staging
            assert s.record.launch_instance_id not in launches
            launches.add(s.record.launch_instance_id)
            assert s.call('invoke_read',{'echo':index})['echo']==index
        assert not [event for event in s.events if event['event']=='crash']
    finally:s.stop()


def test_leader_crash_reclaims_previously_observed_owned_child(tmp_path):
    _,s=host(tmp_path)
    try:
        future=s.submit('invoke_read',{'sleep':1,'spawn_child':True})
        wait_for(lambda:bool(psutil.Process(s.record.pid).children()))
        child=psutil.Process(s.record.pid).children()[0]
        s.heartbeat()  # persist an exact PID/start identity before the leader dies
        psutil.Process(s.record.pid).kill()
        with pytest.raises(SupervisorError):future.result(timeout=3)
        wait_for(lambda:not child.is_running() or child.status()==psutil.STATUS_ZOMBIE)
    finally:s.stop()
    assert not s.cleanup_report()['processes_remaining']
