"""Signed portable Provider runtime with one reader and bounded concurrent jobs."""
from __future__ import annotations
import base64
import hashlib
import importlib.util
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from . import wire
from .contracts import SdkError, validate_envelope, standard_result

IDENTITY_FIELDS=('engine_id','package_digest','payload_digest','runtime_digest','provider_version',
                 'sdk_version','generation','launch_instance_id','host_id')


class Runtime:
    def __init__(self,config,application,sock,keys,transcript):
        self.config,self.app,self.sock=config,application,sock
        self.host_key,self.provider_key=keys
        self.connection_id=transcript['connection_id']
        self.identity=config['identity']
        self.native=config.get('provider_mode')=='native'
        self.native_ops=tuple(config.get('capabilities',())) if self.native else ()
        self.target_connections=0
        if self.native:
            from .broker_client import BrokerClient
            self.broker=BrokerClient.from_launch_config(config)
        self.lock=threading.RLock();self.send_lock=threading.Lock()
        self.jobs={};self.completed={};self.seq=0;self.executed=0;self.draining=False
        self.limit=config['limits']['max_inflight']
        self.output_limit=config['limits']['max_output_bytes']
        self.pool=ThreadPoolExecutor(max_workers=self.limit,thread_name_prefix='provider-job')

    def send(self,value):
        with self.send_lock:
            wire.send_frame(self.sock,value,self.provider_key,self.seq,
                connection_id=self.connection_id,direction='provider-to-host')
            self.seq+=1

    def response(self,request,result=None,error=None):
        message={'type':'response','request_id':request['request_id'],'context':request['context']}
        if error is not None:
            message['error']=error.as_dict() if isinstance(error,SdkError) else SdkError('provider_failed',category='runtime',effect_state='unknown').as_dict()
            self.send(message);return
        raw=wire.canonical(result)
        if len(raw)>self.output_limit:
            self.response(request,error=SdkError('output_limit_exceeded',category='limit'));return
        if len(raw)<300*1024:
            message['result']=result;self.send(message);return
        message.update(type='result_begin',size=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        self.send(message)
        for index,offset in enumerate(range(0,len(raw),256*1024)):
            self.send({'type':'result_chunk','request_id':request['request_id'],'context':request['context'],
                       'index':index,'data':base64.b64encode(raw[offset:offset+256*1024]).decode('ascii')})
        self.send({'type':'result_end','request_id':request['request_id'],'context':request['context']})

    def wait(self,event,seconds,ignore=False):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            if event.is_set() and not ignore:raise SdkError('call_cancelled',category='cancelled')
            time.sleep(min(.01,max(0,until-time.monotonic())))
        if event.is_set() and not ignore:raise SdkError('call_cancelled',category='cancelled')

    def effect(self,request):
        path=self.config.get('store_path')
        if not path:raise SdkError('effect_store_unavailable')
        context=request['context'];operation=context['operation_id'];method=request['method']
        db=sqlite3.connect(path,timeout=10,isolation_level=None)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS provider_effects (operation_id TEXT PRIMARY KEY,input_digest TEXT NOT NULL,value TEXT NOT NULL,result TEXT NOT NULL,committed_at REAL NOT NULL)')
            if method=='prepare':return {'prepared':True,'operation_id':operation,'effect_state':'none'}
            if method=='verify':
                row=db.execute('SELECT input_digest,result FROM provider_effects WHERE operation_id=?',(operation,)).fetchone()
                if row and row[0]!=context['input_digest']:raise SdkError('effect_idempotency_conflict')
                return {'found':bool(row),'result':json.loads(row[1]) if row else None,'effect_state':'completed' if row else 'none'}
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT input_digest,result FROM provider_effects WHERE operation_id=?',(operation,)).fetchone()
            if prior:
                if prior[0]!=context['input_digest']:raise SdkError('effect_idempotency_conflict')
                result=json.loads(prior[1])
            else:
                result={'operation_id':operation,'effect_key':request['params'].get('effect_key',operation),
                        'value':request['params'].get('value'),'effect_state':'completed','marker':self.app.MARKER}
                db.execute('INSERT INTO provider_effects VALUES (?,?,?,?,?)',(operation,context['input_digest'],
                    json.dumps(request['params'].get('value'),sort_keys=True),json.dumps(result,sort_keys=True),time.time()))
            db.execute('COMMIT');return result
        except Exception:
            if db.in_transaction:db.execute('ROLLBACK')
            raise
        finally:db.close()

    def execute(self,request,event):
        if self.native:
            return self.execute_native(request,event)
        child=None;result=None;error=None;committed=False
        try:
            params=request['params'];method=request['method']
            if request['context']['deadline']<=time.time():raise SdkError('deadline_exceeded',category='timeout')
            with self.lock:self.executed+=1
            if params.get('spawn_child'):
                child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)'],
                                       stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            self.wait(event,params.get('sleep',0),params.get('ignore_cancel',False))
            if method in {'prepare','execute','verify'}:
                result=self.effect(request)
                committed=method=='execute' and result.get('effect_state')=='completed'
            else:
                if params.get('fail'):raise SdkError('fixture_read_failed',category='runtime')
                count=params.get('output_bytes',0)
                if count>self.output_limit:raise SdkError('output_limit_exceeded',category='limit')
                result=self.app.invoke_read(params)
                if count:result['blob']='x'*count
            self.wait(event,params.get('reply_delay',0),params.get('ignore_cancel',False))
            result=standard_result(provider_version=self.app.PROVIDER_VERSION,values=result,
                context=request['context'],optional_fields=request.get('optional_fields',{}))
        except SdkError as exc:
            if committed:exc.effect_state='completed'
            error=exc
        except Exception:
            error=SdkError('provider_failed',category='runtime',effect_state='completed' if committed else 'unknown')
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=1)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=1)
            with self.lock:
                self.jobs.pop(request['request_id'],None)
                self.completed[request['request_id']]='done'
                if len(self.completed)>1024:self.completed.pop(next(iter(self.completed)))
        self.response(request,result=result,error=error)

    def execute_native(self,request,event):
        result=None;error=None;lease_id=request['params']['lease_id']
        context=request['context']
        try:
            if event.is_set() or context['deadline']<=time.time():
                raise SdkError('deadline_exceeded',category='timeout')
            secret=self.broker.redeem(lease_id,context)
            if (set(secret)!={'config','expected_identity'} or not isinstance(secret['config'],dict)
                    or not isinstance(secret['expected_identity'],dict)):
                raise SdkError('broker_secret_invalid')
            with self.lock:
                self.target_connections+=1;self.executed+=1
            try:
                rows=self.app.Provider().invoke(request['params']['operation'],secret['config'],
                                                request['params']['arguments'],
                                                expected_identity=secret['expected_identity'])
            finally:
                with self.lock:self.target_connections-=1
            if event.is_set() or context['deadline']<=time.time():
                raise SdkError('call_cancelled',category='cancelled')
            result=standard_result(provider_version=self.app.PROVIDER_VERSION,values=rows,
                context=context,optional_fields=request.get('optional_fields',{}))
        except SdkError as exc:
            error=exc
        except Exception as exc:
            code=getattr(exc,'code',None)
            if code not in {'unsupported','invalid_parameters','invalid_config','expected_identity_required',
                            'driver_unavailable','identity_mismatch','identity_unavailable','permission_denied',
                            'database_error','row_limit_exceeded','byte_limit_exceeded','result_not_serializable',
                            'broker_lease_unavailable','broker_unauthorized','broker_scope_mismatch'}:
                code='provider_failed'
            error=SdkError(code,category='runtime')
        finally:
            # ReadProvider.invoke closes cursor/connection before this release.
            try:self.broker.release(lease_id,context)
            except Exception:
                if error is None:error=SdkError('broker_release_failed',category='runtime')
            with self.lock:
                self.jobs.pop(request['request_id'],None)
                self.completed[request['request_id']]='done'
        self.response(request,result=result,error=error)

    def dispatch(self,request):
        try:
            validate_envelope(request,require_context=True,profile='native' if self.native else 'fixture')
            if request['method']!='verify':
                from .contracts import digest_json
                if request['context']['input_digest']!=digest_json(request['params']):raise SdkError('input_digest_mismatch')
            for key in IDENTITY_FIELDS:
                if request['context'][key]!=self.identity[key]:raise SdkError('request_identity_mismatch')
            method=request['method'];rid=request['request_id']
            if method in {'invoke_read','prepare','execute','verify'}:
                if self.native:
                    if (method!='invoke_read' or request['params']['operation'] not in self.native_ops
                            or request['context']['capability_id']!=request['params']['operation']):
                        raise SdkError('capability_not_registered')
                else:
                    expected_cap='test.effect' if method in {'prepare','execute','verify'} else 'test.read'
                    if request['context']['capability_id']!=expected_cap:raise SdkError('capability_not_registered')
                with self.lock:
                    if self.draining and method!='verify':raise SdkError('provider_draining')
                    if rid in self.jobs or rid in self.completed:raise SdkError('request_replay')
                    if len(self.jobs)>=self.limit:raise SdkError('provider_busy',category='limit')
                    event=threading.Event();self.jobs[rid]=event
                self.pool.submit(self.execute,request,event);return
            with self.lock:
                if method=='cancel':
                    event=self.jobs.get(request['params'].get('request_id'))
                    if event:event.set()
                    result={'cancel_requested':event is not None}
                elif method=='drain':self.draining=True;result={'draining':True,'inflight':len(self.jobs)}
                elif method in {'heartbeat','status'}:
                    result={'ok':True,'launch_instance_id':self.identity['launch_instance_id'],
                            'queue':0,'inflight':len(self.jobs),'connections':1,'target_connections':self.target_connections,
                            'executed':self.executed,'draining':self.draining,'marker':self.app.MARKER}
                    if method=='status' and request['params'].get('request_id'):
                        target=request['params']['request_id']
                        result['request_state']='running' if target in self.jobs else self.completed.get(target,'unknown')
                elif method=='selftest':
                    if self.native:
                        try:driver_present=importlib.util.find_spec(self.app.DRIVER) is not None
                        except (ImportError,ValueError,AttributeError):driver_present=False
                        if (self.app.ENGINE_ID!=self.identity['engine_id']
                                or self.app.PROVIDER_VERSION!=self.identity['provider_version']
                                or not driver_present):
                            raise SdkError('driver_unavailable',category='runtime')
                    result={'ok':True,'marker':self.app.MARKER,'identity':self.identity,
                            'fixture_only':bool(self.config.get('fixture_only',True)),
                            'server_certified':False}
                elif method in {'handshake','describe'}:result=dict(self.identity)
                elif method=='capabilities':result={'capabilities':list(self.native_ops) if self.native else ['test.read','test.effect'],
                                                    'fixture_only':bool(self.config.get('fixture_only',True)),
                                                    'server_certified':False}
                elif method=='validate_config':result={'valid':True,'connected':False}
                else:raise SdkError('sdk_method_unknown')
            self.response(request,result=standard_result(provider_version=self.app.PROVIDER_VERSION,
                values=result,context=request['context'],optional_fields=request.get('optional_fields',{})))
        except SdkError as exc:
            if isinstance(request,dict) and 'request_id' in request and 'context' in request:self.response(request,error=exc)
            else:raise

    def run(self):
        sequence=0
        try:
            while True:
                request=wire.recv_frame(self.sock,self.host_key,sequence,
                    connection_id=self.connection_id,direction='host-to-provider')
                sequence+=1;self.dispatch(request)
        finally:
            with self.lock:
                for event in self.jobs.values():event.set()
            self.pool.shutdown(wait=True,cancel_futures=True)
            self.sock.close()


def main(launch_fd,root):
    token,config=wire.read_launch(launch_fd)
    identity=config['identity']
    manifest=wire.strict_json((Path(root)/'ENGINE-MANIFEST.json').read_bytes())
    actual='sha256:'+hashlib.sha256(wire.canonical(manifest)).hexdigest()
    if actual!=identity['package_digest']:raise wire.IpcError('package_identity_mismatch')
    native=config.get('provider_mode')=='native'
    if native:
        from .profiles import validate_signed_profile, validate_application
        profile=validate_signed_profile(manifest,root)
        if (manifest.get('entrypoint_id')!='python.sdk.provider.v1'
                or manifest.get('engine_id')!=identity['engine_id']
                or tuple(config.get('capabilities',()))!=tuple(profile['operations'])):
            raise wire.IpcError('package_profile_mismatch')
        entry=Path(root)/'engine_provider.py'
    else:
        if manifest.get('entrypoint_id')!='python.sdk.fixture.v1':
            raise wire.IpcError('package_profile_mismatch')
        entry=Path(root)/'fake_provider.py'
    spec=importlib.util.spec_from_file_location('verified_provider_application',entry)
    application=importlib.util.module_from_spec(spec);spec.loader.exec_module(application)
    if application.PROVIDER_VERSION!=identity['provider_version']:raise wire.IpcError('handshake_version_mismatch')
    if native:
        try:validate_application(application,manifest)
        except ValueError as exc:raise wire.IpcError('package_profile_mismatch') from exc
    sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    sock.settimeout(10);sock.connect(config['socket_path'])
    host=wire.peer_credentials(sock)
    if host.uid!=os.getuid() or host.pid!=config['host_pid']:raise wire.IpcError('peer_identity_mismatch')
    provider_nonce=os.urandom(32)
    provider_peer={'pid':os.getpid(),'uid':os.getuid(),'gid':os.getgid()}
    wire.send_plain(sock,{'type':'hello','identity':identity,'nonce':provider_nonce.hex(),'peer':provider_peer})
    challenge=wire.recv_plain(sock)
    transcript=challenge.get('transcript')
    expected={'host_nonce':transcript.get('host_nonce'),'provider_nonce':provider_nonce.hex(),
        'host_peer':{'pid':host.pid,'uid':host.uid,'gid':host.gid},'provider_peer':provider_peer,
        'identity':identity,'connection_id':config['connection_id'],'deadline':config['handshake_deadline']}
    if transcript!=expected or config['handshake_deadline']<=time.time():raise wire.IpcError('handshake_binding_invalid')
    proof=bytes.fromhex(challenge.get('proof',''))
    wire.verify_mac(token,b'host-to-provider\0'+wire.canonical(transcript),proof)
    wire.send_plain(sock,{'type':'authenticated','proof':wire.mac(token,b'provider-to-host\0'+wire.canonical(transcript)).hex()})
    keys=wire.session_keys(token,bytes.fromhex(transcript['host_nonce']),provider_nonce,transcript=transcript)
    token=b'';sock.settimeout(None)
    Runtime(config,application,sock,keys,transcript).run()
    return 0
