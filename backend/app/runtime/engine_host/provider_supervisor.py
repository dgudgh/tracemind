"""Signed native read Provider process with Host-granted broker capabilities."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from app.runtime.engine_packs.contract import build_payload_inventory
from app.runtime.engine_packs.trust import canonical_manifest_bytes, verify_engine_manifest_signature
from app.runtime.engine_sdk import wire
from app.runtime.engine_sdk.contracts import TASK_SCHEMA, RESULT_SCHEMA, digest_json
from app.runtime.engine_sdk.profiles import validate_signed_profile
from .lifecycle import State
from .packages import LAUNCHER, describe_context, runtime_digest
from .provider_packages import ENTRYPOINT
from .supervisor import FixtureSupervisor, SupervisorError


class ProviderSupervisor(FixtureSupervisor):
    ENTRYPOINT_ID = ENTRYPOINT
    ENTRYPOINT_FILE = "engine_provider.py"
    def __init__(self, *, broker, asset_resolver, workdir, **kwargs):
        super().__init__(workdir=workdir, **kwargs)
        self.broker=broker
        self.asset_resolver=asset_resolver
        self._target_connection_limit=8
        self._channel_key=None

    def _sdk_profile(self):
        return 'native'

    def _launcher_path(self):
        return LAUNCHER

    def _stage(self, source, manifest, signature, keys, *, expected_namespace, expected_channel):
        """与父类同签名：父类按关键字传入期望的命名空间/通道。

        子类原先用位置参数 (namespace, channel)，父类演进成关键字后直接 TypeError——
        这不是"测试写错"，而是子类没跟上被覆盖方法的契约。
        """
        namespace, channel = expected_namespace, expected_channel
        verified=verify_engine_manifest_signature(manifest,signature,trusted_keys=keys,
            expected_namespace=namespace,expected_channel=channel)
        try:profile=validate_signed_profile(manifest,source)
        except ValueError as exc:raise SupervisorError(getattr(exc,'args',['provider_profile_invalid'])[0]) from exc
        inv=build_payload_inventory(source,manifest)
        context=describe_context()
        if manifest.get('entrypoint_id')!=ENTRYPOINT:
            raise SupervisorError('provider_profile_invalid')
        if (manifest.get('synthetic_driver') is True and manifest.get('fixture_only') is not True
                or channel!='test' and manifest.get('fixture_only') is True):
            raise SupervisorError('fixture_channel_required')
        for key in ('os','cpu','python_abi'):
            if manifest.get(key)!=context[key]:raise SupervisorError('runtime_abi_mismatch',key)
        if manifest.get('runtime_digest')!=runtime_digest():raise SupervisorError('runtime_identity_mismatch')
        if (manifest.get('sdk_version') not in ('1.0','0.9')
                or manifest.get('rpc_version')!='engine.rpc/v1'
                or manifest.get('task_schema')!=TASK_SCHEMA
                or manifest.get('result_schema')!=RESULT_SCHEMA):
            raise SupervisorError('runtime_contract_mismatch')
        limits=manifest.get('limits',{})
        for field,attr,cap in (('max_inflight','max_inflight',8),('max_connections','max_connections',32),
                               ('max_output_bytes','max_output_bytes',64*1024*1024)):
            value=limits.get(field,cap)
            if type(value) is not int or not 0<value<=cap:raise SupervisorError('invalid_limits',field)
            setattr(self,attr,min(getattr(self,attr),value))
        target=self.workdir/'runtime'/verified.manifest_sha256
        if not target.exists():
            target.mkdir(parents=True,mode=0o700)
            for item in inv['inventory']:
                src=Path(source)/item['path'];dst=target/item['path']
                if src.is_symlink():raise SupervisorError('payload_link_rejected')
                dst.parent.mkdir(parents=True,exist_ok=True)
                dst.write_bytes(src.read_bytes())
            (target/'ENGINE-MANIFEST.json').write_bytes(canonical_manifest_bytes(manifest))
            (target/'ENGINE-MANIFEST.sig').write_bytes(wire.canonical(signature))
        staged_manifest=wire.strict_json((target/'ENGINE-MANIFEST.json').read_bytes())
        staged_sig=wire.strict_json((target/'ENGINE-MANIFEST.sig').read_bytes())
        again=verify_engine_manifest_signature(staged_manifest,staged_sig,trusted_keys=keys,
            expected_namespace=namespace,expected_channel=channel)
        actual=build_payload_inventory(target,staged_manifest)
        if (again.manifest_sha256!=verified.manifest_sha256
                or actual['self_payload_digest']!=inv['self_payload_digest']):
            raise SupervisorError('staging_byte_mismatch')
        try:validate_signed_profile(staged_manifest,target)
        except ValueError as exc:raise SupervisorError(getattr(exc,'args',['provider_profile_invalid'])[0]) from exc
        for path in target.rglob('*'):
            if path.is_file():os.chmod(path,0o400)
            elif path.is_dir():os.chmod(path,0o500)
        os.chmod(target,0o500)
        identity={'engine_id':verified.engine_id,'package_digest':'sha256:'+verified.manifest_sha256,
                  'payload_digest':inv['self_payload_digest'],'runtime_digest':manifest['runtime_digest'],
                  'provider_version':manifest['version'],'sdk_version':manifest['sdk_version'],
                  'generation':self._generation}
        self._staged=target;self._base_identity=identity
        return target,identity

    def _prepare_launch_config(self, config):
        key=os.urandom(32)
        self.broker.register_launch({**vars(self.record),'engine_id':self.identity['engine_id']},
                                    channel_key=key)
        self._channel_key=key
        config.update(provider_mode='native',capabilities=self._spec[1]['capabilities'],
                      fixture_only=bool(self._spec[1].get('fixture_only')),
                      secret_broker={'broker_path':str(self.broker.path),
                                     'broker_pid':os.getpid(),'broker_key':key.hex()})
        return config

    def _launch_failed(self):
        if self.record:
            self.broker.revoke(launch_instance_id=self.record.launch_instance_id,
                               reason='launch_exited')

    def submit(self, method, params=None, *, timeout=60, context=None, optional_fields=None):
        if method=='invoke_read':
            raise SupervisorError('use_host_granted_read')
        if method in {'prepare','execute','verify'}:
            raise SupervisorError('native_write_unsupported')
        return super().submit(method,params,timeout=timeout,context=context,optional_fields=optional_fields)

    def submit_read(self, asset_id, operation, *, arguments=None, timeout=60, lease_ttl=None):
        if self.state not in (State.READY,State.ACTIVE):
            raise SupervisorError('worker_not_ready')
        if operation not in self._spec[1]['provider_profile']['operations']:
            raise SupervisorError('capability_not_registered')
        if arguments not in (None,{}):
            raise SupervisorError('sdk_params_invalid')
        # This callback returns Host-frozen authorization facts, never user config.
        asset=self.asset_resolver(asset_id,operation)
        if (not isinstance(asset,dict)
                or set(asset)!={'target_digest','policy_digest','scope','principal','secret_ref','fence_epoch'}
                or not isinstance(asset['scope'],dict) or not asset['scope']):
            raise SupervisorError('asset_authorization_invalid')
        if type(asset['fence_epoch']) is not int or asset['fence_epoch']<1:
            raise SupervisorError('asset_authorization_invalid')
        operation_id=uuid.uuid4().hex
        deadline=time.time()+timeout-.01
        context={**self.identity,'operation_id':operation_id,'attempt':1,'fence_epoch':asset['fence_epoch'],
                 'task_schema':TASK_SCHEMA,'result_schema':RESULT_SCHEMA,'capability_id':operation,
                 'input_digest':digest_json({'operation':operation,'arguments':{},'lease_id':'pending'}),
                 'target_digest':asset['target_digest'],'scope':asset['scope'],
                 'policy_digest':asset['policy_digest'],'deadline':deadline}
        ttl=min(60,deadline-time.time()-.01)
        if lease_ttl is not None:
            if type(lease_ttl) not in (int,float) or lease_ttl<=0:
                raise SupervisorError('broker_grant_invalid')
            ttl=min(ttl,lease_ttl)
        if ttl<=0:raise SupervisorError('deadline_exceeded',category='timeout')
        lease=self.broker.grant(asset['secret_ref'],asset['principal'],context,ttl)
        params={'operation':operation,'arguments':{},'lease_id':lease}
        try:
            return FixtureSupervisor.submit(self,'invoke_read',params,timeout=timeout,
                context={'operation_id':operation_id,'capability_id':operation,
                         'fence_epoch':asset['fence_epoch'],
                         'target_digest':asset['target_digest'],'scope':asset['scope'],
                         'policy_digest':asset['policy_digest'],'deadline':deadline})
        except Exception:
            self.broker.revoke(lease_id=lease,reason='explicit_revocation')
            raise

    def invoke_read(self, asset_id, operation, *, arguments=None, timeout=60, lease_ttl=None):
        future=self.submit_read(asset_id,operation,arguments=arguments,timeout=timeout,
                                lease_ttl=lease_ttl)
        return future.result(timeout=timeout+self.cancellation_grace+5)['values']

    def quarantine(self, reason):
        if self.record:
            self.broker.revoke(launch_instance_id=self.record.launch_instance_id,reason='quarantine')
        return super().quarantine(reason)

    def stop(self):
        result=super().stop()
        if self.record:
            self.broker.revoke(launch_instance_id=self.record.launch_instance_id,reason='launch_exited')
        return result
