from __future__ import annotations
import hashlib
import hmac
import json
import os
import socket
import struct
from dataclasses import replace
from pathlib import Path
import psutil
import pytest
from app.runtime.engine_sdk import wire
from app.runtime.engine_host.packages import build_fixture_package
from app.runtime.engine_host.supervisor import FixtureSupervisor, SupervisorError
from app.runtime.engine_host.ownership import cleanup_owned, OwnershipError


@pytest.mark.parametrize('body',[
    b'{"seq":0,"payload":{"n":1,"n":2}}',
    b'{"seq":0,"payload":{"n":NaN}}',
    b'{"seq":0,"payload":{"n":Infinity}}',
    b'{"seq":0,"payload":{"n":1e999}}',
    b'{"seq":false,"payload":{}}',
    b'{"seq":0,"payload":[]}',
    b'{"seq":0,"payload":{"n":"\xff"}}',
    b'{"seq":0,"payload":{"n":'+b'['*40+b'0'+b']'*40+b'}}',
])
def test_authenticated_bad_json_and_type_frames_fail_before_dispatch(body):
    key=b'k'*32
    raw=hmac.new(key,body,hashlib.sha256).hexdigest().encode()+b'.'+body
    with pytest.raises(wire.IpcError):wire.decode_frame(raw,key,0)


@pytest.mark.parametrize('prefix',[0,1024*1024+66,0xffffffff])
def test_wire_prefix_limit_and_truncation_are_bounded(prefix):
    left,right=socket.socketpair()
    try:
        left.sendall(struct.pack('!I',prefix));left.shutdown(socket.SHUT_WR)
        with pytest.raises(wire.IpcError):wire.recv_frame(right,b'k'*32,0)
    finally:left.close();right.close()


def test_direction_connection_sequence_and_nonce_are_cryptographically_bound():
    token=b't'*32;host_nonce=b'h'*32;provider_nonce=b'p'*32
    identity={'engine':'test.fake','package':'sha256:'+'a'*64,'launch':'one','sdk':'1.0','deadline':10}
    keys=wire.session_keys(token,host_nonce,provider_nonce,transcript=identity)
    changed=wire.session_keys(token,host_nonce,provider_nonce,transcript={**identity,'launch':'two'})
    assert keys!=changed and keys[0]!=keys[1]
    frame=wire.encode_frame({'ok':True},keys[0],0,connection_id='one',direction='host-to-provider')[4:]
    assert wire.decode_frame(frame,keys[0],0,connection_id='one',direction='host-to-provider')['ok']
    for key,seq,cid,direction in [(keys[1],0,'one','provider-to-host'),
        (keys[0],1,'one','host-to-provider'),(keys[0],0,'two','host-to-provider'),
        (keys[0],0,'one','provider-to-host'),(changed[0],0,'one','host-to-provider')]:
        with pytest.raises(wire.IpcError):wire.decode_frame(frame,key,seq,connection_id=cid,direction=direction)
    proof=wire.mac(token,b'host-to-provider\0'+wire.canonical(identity))
    with pytest.raises(wire.IpcError):wire.verify_mac(token,b'provider-to-host\0'+wire.canonical(identity),proof)
    with pytest.raises(wire.IpcError):wire.session_keys(token,b'short',provider_nonce,transcript=identity)


def test_actual_uds_peer_api_reports_pid_uid_gid():
    left,right=socket.socketpair()
    try:
        peer=wire.peer_credentials(right)
        assert peer.pid==os.getpid() and peer.uid==os.getuid() and peer.gid==os.getgid()
    finally:left.close();right.close()


def test_same_uid_wrong_real_pid_cannot_claim_provider_endpoint(tmp_path):
    import subprocess
    import sys
    import time
    package=build_fixture_package(tmp_path)
    intruders=[]
    marker=tmp_path/'intruder-connected'
    def hook(phase,detail):
        if phase=='provider_started':
            # The real Provider is still blocked reading its inherited secret.
            program='import socket,sys; from pathlib import Path; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); Path(sys.argv[2]).write_text("connected"); s.recv(1)'
            process=subprocess.Popen([sys.executable,'-I','-c',program,detail['socket_path'],str(marker)],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            intruders.append(process)
            until=time.monotonic()+2
            while not marker.exists() and time.monotonic()<until:time.sleep(.005)
            assert marker.exists()
    supervisor=FixtureSupervisor(workdir=tmp_path/'host',phase_hook=hook,restart=False)
    try:
        with pytest.raises(SupervisorError,match='peer_identity_mismatch'):
            supervisor.start(package.source,package.manifest,package.signature,package.trusted_keys,
                expected_namespace='tmengine.test',expected_channel='test')
    finally:
        supervisor.stop()
        for process in intruders:
            if process.poll() is None:process.terminate()
            process.wait(timeout=2)
    report=supervisor.cleanup_report()
    assert not report['processes_remaining'] and not report['sockets_remaining']


def test_ownership_mismatch_never_kills_or_unlinks_a_live_instance(tmp_path):
    package=build_fixture_package(tmp_path)
    supervisor=FixtureSupervisor(workdir=tmp_path/'host',restart=False)
    try:
        supervisor.start(package.source,package.manifest,package.signature,package.trusted_keys,
            expected_namespace='tmengine.test',expected_channel='test')
        original=supervisor.owned_resources()[0]
        wrong={**original,'start_time':original['start_time']+10}
        report=cleanup_owned(wrong,timeout=.01)
        assert not report['identity_matched'] and 'process_identity_mismatch' in report['blocked_reasons']
        assert Path(original['socket_path']).exists() and supervisor.call('selftest')['ok']
    finally:supervisor.stop()
    assert not supervisor.cleanup_report()['temporary_artifacts_remaining']


def test_missing_leader_and_reused_pgid_never_signal_unknown_processes(tmp_path,monkeypatch):
    from app.runtime.engine_host import ownership
    from types import SimpleNamespace
    work=ownership.ensure_private_workdir(tmp_path/'host')
    ipc=tmp_path/'ipc';ipc.mkdir();ownership.mark_ipc(ipc,'old-launch')
    record={'pid':12345,'pgid':12345,'start_time':100.,'uid':os.getuid(),
            'workdir':str(work),'ipc_root':str(ipc),'socket_path':str(ipc/'provider.sock'),
            'owner_token':json.loads((work/'OWNERSHIP').read_text())['token'],
            'launch_instance_id':'old-launch','members':[
                {'pid':12345,'pgid':12345,'start_time':100.,'uid':os.getuid()}]}
    signals=[]
    class Reused:
        pid=12345
        def create_time(self):return 200.
        def uids(self):return SimpleNamespace(real=os.getuid())
        def send_signal(self,sig):signals.append(sig)
    def missing(_):raise psutil.NoSuchProcess(12345)
    original=os.getpgid
    monkeypatch.setattr(ownership.psutil,'Process',missing)
    monkeypatch.setattr(ownership,'_group_members',lambda *args:[Reused()])
    monkeypatch.setattr(os,'getpgid',lambda pid:12345 if pid==12345 else original(pid))
    monkeypatch.setattr(os,'killpg',lambda *args:signals.append(args))
    result=ownership.cleanup_owned(record,timeout=.001)
    assert not result['identity_matched']
    assert 'orphan_group_unverified' in result['blocked_reasons']
    assert signals==[] and ipc.exists()


def test_explicit_private_workdir_rejects_existing_content_and_links(tmp_path):
    existing=tmp_path/'existing';existing.mkdir();(existing/'keep').write_text('keep')
    with pytest.raises(OwnershipError):FixtureSupervisor(workdir=existing)
    assert (existing/'keep').read_text()=='keep'
    link=tmp_path/'linked';link.symlink_to(existing,target_is_directory=True)
    with pytest.raises(OwnershipError):FixtureSupervisor(workdir=link)
    with pytest.raises(OwnershipError):FixtureSupervisor(workdir=Path('relative-workdir'))


def test_launch_secret_never_enters_argv_environment_events_or_files(tmp_path,monkeypatch):
    package=build_fixture_package(tmp_path)
    secret=(b'RAW_SECRET_NO_LOG\n\x00 \r\t'+b'x'*32)[:32]
    original=os.urandom;used=[]
    def controlled(size):
        if size==32 and not used:used.append(True);return secret
        return original(size)
    monkeypatch.setattr(os,'urandom',controlled)
    supervisor=FixtureSupervisor(workdir=tmp_path/'host',restart=False)
    try:
        record=supervisor.start(package.source,package.manifest,package.signature,package.trusted_keys,
            expected_namespace='tmengine.test',expected_channel='test')
        assert used
        process=psutil.Process(record.pid)
        observed=json.dumps({'argv':process.cmdline(),'env':process.environ(),'events':supervisor.events}).encode()
        assert secret not in observed and secret.hex().encode() not in observed
        for file in tmp_path.rglob('*'):
            if file.is_file():assert secret not in file.read_bytes()
    finally:supervisor.stop()
