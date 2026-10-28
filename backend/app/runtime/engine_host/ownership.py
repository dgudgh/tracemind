"""Identity-matched cleanup; missing leaders never authorize bare PGID signals."""
from __future__ import annotations
import json
import os
import shutil
import signal
import stat
import time
import uuid
from pathlib import Path
import psutil

class OwnershipError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


def ensure_private_workdir(path):
    path=Path(path)
    if not path.is_absolute() or path.is_symlink():raise OwnershipError('workdir_unsafe')
    if path.exists() and (not path.is_dir() or any(path.iterdir())):raise OwnershipError('workdir_not_empty')
    path.mkdir(parents=True,mode=0o700,exist_ok=True);os.chmod(path,0o700);path=path.resolve()
    marker={'schema':'tmengine.owner/v1','uid':os.getuid(),'host_pid':os.getpid(),
            'host_start_time':psutil.Process().create_time(),'token':uuid.uuid4().hex}
    (path/'OWNERSHIP').write_text(json.dumps(marker,sort_keys=True));os.chmod(path/'OWNERSHIP',0o600)
    return path


def mark_ipc(root,launch_instance_id):
    root=Path(root).resolve();os.chmod(root,0o700)
    (root/'OWNERSHIP').write_text(json.dumps({'uid':os.getuid(),'launch_instance_id':launch_instance_id},sort_keys=True))
    os.chmod(root/'OWNERSHIP',0o600)


def _record(value):return dict(value) if isinstance(value,dict) else vars(value).copy()


def _member(process):
    return {'pid':process.pid,'start_time':process.create_time(),'uid':process.uids().real,
            'pgid':os.getpgid(process.pid)}


def _group_members(pgid,started,uid):
    """Discovery is not ownership proof. Never signal this list without matching identities."""
    found=[]
    for process in psutil.process_iter(['pid','create_time','uids','status']):
        try:
            if os.getpgid(process.pid)==pgid and process.status()!=psutil.STATUS_ZOMBIE:found.append(process)
        except (psutil.NoSuchProcess,ProcessLookupError,psutil.AccessDenied,FileNotFoundError):continue
    return found


def capture_process(pid,*,launch_instance_id,socket_path,workdir,package_digest):
    process=psutil.Process(pid)
    record={**_member(process),'launch_instance_id':launch_instance_id,
        'socket_path':str(Path(socket_path).resolve()),'ipc_root':str(Path(socket_path).parent.resolve()),
        'workdir':str(Path(workdir).resolve()),'package_digest':package_digest}
    record['owner_token']=json.loads((Path(workdir)/'OWNERSHIP').read_text())['token']
    record['members']=[_member(process)]
    return record


def _validate_root(record):
    root=Path(record['workdir']);marker_path=root/'OWNERSHIP'
    if root.is_symlink() or marker_path.is_symlink() or root.stat().st_uid!=os.getuid():raise OwnershipError('ownership_mismatch')
    marker=json.loads(marker_path.read_text())
    if marker.get('token')!=record.get('owner_token') or record.get('uid')!=os.getuid():raise OwnershipError('ownership_mismatch')
    if int(record['pgid'])!=int(record['pid']) or int(record['pgid'])==os.getpgrp():raise OwnershipError('process_group_identity_mismatch')


def _same(process,identity):
    return (process.pid==int(identity['pid']) and
        abs(process.create_time()-float(identity['start_time']))<=.001 and
        process.uids().real==int(identity['uid']) and os.getpgid(process.pid)==int(identity['pgid']))


def refresh_members(value):
    record=_record(value);_validate_root(record)
    try:
        leader=psutil.Process(record['pid'])
        if not _same(leader,record):raise OwnershipError('process_identity_mismatch')
    except (psutil.NoSuchProcess,ProcessLookupError):return record
    members=_group_members(record['pgid'],record['start_time'],record['uid'])
    if any(p.uids().real!=record['uid'] or p.create_time()<record['start_time']-.001 for p in members):
        raise OwnershipError('process_group_identity_mismatch')
    # Recheck the leader after discovery, before assigning lineage to any child.
    try:
        if not _same(psutil.Process(record['pid']),record):raise OwnershipError('process_identity_mismatch')
    except (psutil.NoSuchProcess,ProcessLookupError):return record
    captured=[]
    for process in members:
        try:captured.append(_member(process))
        except (psutil.NoSuchProcess,ProcessLookupError):continue
    record['members']=captured
    return record


def _verified_members(record):
    _validate_root(record)
    try:
        leader=psutil.Process(record['pid'])
        if not _same(leader,record):raise OwnershipError('process_identity_mismatch')
        record=refresh_members(record)
    except (psutil.NoSuchProcess,ProcessLookupError):
        # A missing leader does not authorize signals to a newly reused PGID.
        pass
    known={int(m['pid']):m for m in record.get('members',[])}
    current=_group_members(record['pgid'],record['start_time'],record['uid'])
    verified=[]
    for process in current:
        identity=known.get(process.pid)
        try:
            if identity is None or not _same(process,identity):raise OwnershipError('orphan_group_unverified')
        except (psutil.NoSuchProcess,ProcessLookupError):continue
        verified.append((process,identity))
    return verified


def signal_owned(value,sig):
    record=_record(value)
    members=_verified_members(record)
    # Signal verified children before the leader. psutil also protects against PID reuse.
    members.sort(key=lambda pair:pair[0].pid==record['pid'])
    sent=[]
    for process,identity in members:
        try:
            if not _same(process,identity):raise OwnershipError('process_identity_mismatch')
            process.send_signal(sig);sent.append(process.pid)
        except (psutil.NoSuchProcess,ProcessLookupError):continue
    return sent


def cleanup_owned(value,*,terminate=True,timeout=2.0):
    record=_record(value)
    report={'identity_matched':False,'processes_remaining':[],'sockets_remaining':[],
            'removed':[],'blocked_reasons':[]}
    try:
        members=_verified_members(record);report['identity_matched']=True
        if terminate and members:
            # Persist the exact snapshot so a leader exit cannot expand ownership.
            record['members']=[identity for _,identity in members]
            signal_owned(record,signal.SIGTERM)
            until=time.monotonic()+timeout
            while time.monotonic()<until and _verified_members(record):time.sleep(.01)
            if _verified_members(record):
                signal_owned(record,signal.SIGKILL)
                until=time.monotonic()+timeout
                while time.monotonic()<until and _verified_members(record):time.sleep(.01)
        report['processes_remaining']=[p.pid for p,_ in _verified_members(record)]
        ipc_root=Path(record['ipc_root'])
        if ipc_root.exists():
            if ipc_root.is_symlink() or ipc_root.stat().st_uid!=os.getuid():raise OwnershipError('ipc_ownership_mismatch')
            marker=ipc_root/'OWNERSHIP'
            if marker.is_symlink() or json.loads(marker.read_text()).get('launch_instance_id')!=record['launch_instance_id']:
                raise OwnershipError('ipc_ownership_mismatch')
            if not report['processes_remaining']:
                for entry in ipc_root.iterdir():
                    if entry.is_symlink() or (entry.name!='OWNERSHIP' and not stat.S_ISSOCK(entry.lstat().st_mode)):
                        raise OwnershipError('ipc_unexpected_object')
                if terminate:shutil.rmtree(ipc_root);report['removed'].append(str(ipc_root))
            report['sockets_remaining']=[str(p) for p in ipc_root.glob('*')
                if p.exists() and stat.S_ISSOCK(p.lstat().st_mode)] if ipc_root.exists() else []
    except (OwnershipError,KeyError,ValueError,OSError,psutil.Error) as exc:
        report['blocked_reasons'].append(getattr(exc,'code',f"{type(exc).__name__}:{exc}"))
    return report
