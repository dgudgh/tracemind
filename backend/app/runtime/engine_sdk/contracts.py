"""Portable SDK schemas, compatibility and core-owned result decoding."""
from __future__ import annotations
import hashlib
import math
import re
from dataclasses import dataclass
from .wire import canonical, strict_json, validate_tree

SDK_VERSION = '1.0'
SUPPORTED_SDK_VERSIONS = ('0.9', '1.0')
TASK_SCHEMA = 'engine.task/v1'
RESULT_SCHEMA = 'engine.result/v1'
METHODS = frozenset({'handshake','describe','capabilities','validate_config','selftest',
                    'heartbeat','invoke_read','status','cancel','drain','prepare','execute','verify'})
REQUIRED_ENVELOPE = {'schema','request_id','method','params'}
CONTEXT_FIELDS = frozenset({'engine_id','package_digest','payload_digest','runtime_digest',
    'provider_version','sdk_version','generation','launch_instance_id','host_id',
    'operation_id','attempt','fence_epoch','task_schema','result_schema','capability_id',
    'input_digest','target_digest','scope','policy_digest','deadline'})
_DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')
_ENGINE = re.compile(r'^[a-z][a-z0-9]*(?:[._][a-z][a-z0-9]*)*$')
_OPERATION = re.compile(r'^[a-z][a-z0-9_]{0,63}$')

class SdkError(ValueError):
    def __init__(self, code, detail='', *, category='contract', retryable=False, effect_state='none'):
        self.code,self.detail,self.category = code,detail,category
        self.retryable,self.effect_state = retryable,effect_state
        self.safe_message,self.diagnostic_ref = code,None
        super().__init__(code)
    def as_dict(self):
        return {k:getattr(self,k) for k in ('code','category','retryable','effect_state','safe_message','diagnostic_ref')}


def canonical_json(value):
    try:return canonical(value)
    except ValueError as exc:raise SdkError('sdk_json_invalid') from exc


def digest_json(value):
    return 'sha256:'+hashlib.sha256(canonical_json(value)).hexdigest()


def negotiate(provider_versions):
    if not isinstance(provider_versions,(list,tuple)) or any(type(x) is not str for x in provider_versions):
        raise SdkError('sdk_version_unsupported')
    for line in reversed(SUPPORTED_SDK_VERSIONS):
        if line in provider_versions:return line
    raise SdkError('sdk_version_unsupported')


def validate_context(context):
    if not isinstance(context,dict) or set(context)!=CONTEXT_FIELDS:
        raise SdkError('sdk_context_invalid')
    for key in ('package_digest','payload_digest','runtime_digest','input_digest','target_digest','policy_digest'):
        if not isinstance(context[key],str) or not _DIGEST.fullmatch(context[key]):raise SdkError('sdk_context_invalid',key)
    for key in ('generation','attempt','fence_epoch'):
        if type(context[key]) is not int or context[key]<1:raise SdkError('sdk_context_invalid',key)
    if (type(context['engine_id']) is not str or not _ENGINE.fullmatch(context['engine_id'])
            or context['sdk_version'] not in SUPPORTED_SDK_VERSIONS):
        raise SdkError('sdk_context_invalid')
    if context['task_schema']!=TASK_SCHEMA or context['result_schema']!=RESULT_SCHEMA:
        raise SdkError('sdk_schema_unsupported')
    if type(context['deadline']) not in (float,int) or not math.isfinite(context['deadline']):
        raise SdkError('sdk_context_invalid','deadline')
    if not isinstance(context['scope'],dict):raise SdkError('sdk_context_invalid','scope')
    for key in ('operation_id','launch_instance_id','host_id','provider_version','capability_id'):
        if type(context[key]) is not str or not context[key]:raise SdkError('sdk_context_invalid',key)
    return context


def validate_params(method,params,*,profile='fixture'):
    if not isinstance(params,dict):raise SdkError('sdk_params_invalid')
    if profile == 'native':
        if method in {'prepare','execute','verify'}:raise SdkError('sdk_method_unavailable')
        if method == 'invoke_read':
            if (set(params) != {'operation','arguments','lease_id'}
                    or type(params['operation']) is not str or not _OPERATION.fullmatch(params['operation'])
                    or params['arguments'] != {} or type(params['lease_id']) is not str
                    or not 20 <= len(params['lease_id']) <= 128):
                raise SdkError('sdk_params_invalid')
            validate_tree(params)
            return params
        if method == 'validate_config' and params:
            raise SdkError('sdk_params_invalid')
    elif profile != 'fixture':
        raise SdkError('sdk_profile_invalid')
    allowed={
      'invoke_read':{'echo','sleep','output_bytes','spawn_child','ignore_cancel','fail','values'},
      'prepare':{'value','effect_key','sleep','reply_delay'},
      'execute':{'value','effect_key','sleep','reply_delay','ignore_cancel'},
      'verify':{'value','effect_key'},'cancel':{'request_id'},'status':{'request_id'},
      'validate_config':{'instance'},
    }.get(method,set())
    if set(params)-allowed:raise SdkError('sdk_params_invalid')
    for key in ('sleep','reply_delay'):
        if key in params and (type(params[key]) not in (float,int) or not math.isfinite(params[key]) or not 0<=params[key]<=60):
            raise SdkError('sdk_params_invalid',key)
    if 'output_bytes' in params and (type(params['output_bytes']) is not int or not 0<=params['output_bytes']<=128*1024*1024):
        raise SdkError('sdk_params_invalid','output_bytes')
    for key in ('spawn_child','ignore_cancel'):
        if key in params and type(params[key]) is not bool:raise SdkError('sdk_params_invalid',key)
    if 'fail' in params and type(params['fail']) is not bool:raise SdkError('sdk_params_invalid','fail')
    if 'values' in params and (not isinstance(params['values'],list) or len(params['values'])>10000
            or any(type(value) not in (int,float) or not math.isfinite(value) for value in params['values'])):
        raise SdkError('sdk_params_invalid','values')
    for key in ('request_id','effect_key'):
        if key in params and (type(params[key]) is not str or not params[key]):raise SdkError('sdk_params_invalid',key)
    validate_tree(params)
    return params


def validate_envelope(envelope, *, require_context=False, profile='fixture'):
    optional={'context','optional_fields','required_fields','extensions'}
    if not isinstance(envelope,dict) or not REQUIRED_ENVELOPE<=set(envelope) or set(envelope)-(REQUIRED_ENVELOPE|optional):
        raise SdkError('sdk_envelope_invalid')
    if envelope['schema']!='engine.rpc/v1':raise SdkError('sdk_schema_unsupported')
    if type(envelope['request_id']) is not str or not envelope['request_id']:raise SdkError('sdk_request_id_invalid')
    if type(envelope['method']) is not str or envelope['method'] not in METHODS:raise SdkError('sdk_method_unknown')
    validate_params(envelope['method'],envelope['params'],profile=profile)
    if require_context or 'context' in envelope:validate_context(envelope.get('context'))
    required=envelope.get('required_fields',[])
    if not isinstance(required,list) or any(type(x) is not str or x not in REQUIRED_ENVELOPE|{'context'} for x in required):
        raise SdkError('sdk_mandatory_unsupported')
    if not isinstance(envelope.get('optional_fields',{}),dict):raise SdkError('sdk_envelope_invalid')
    extensions=envelope.get('extensions',{})
    if not isinstance(extensions,dict) or any(not isinstance(k,str) or '.' not in k for k in extensions):
        raise SdkError('sdk_extension_invalid')
    validate_tree(envelope)
    return envelope


@dataclass(frozen=True)
class Descriptor:
    engine_id:str
    publisher:str
    provider_version:str
    package_digest:str
    runtime_digest:str
    sdk_version:str
    os:str
    cpu:str
    python_abi:str
    generation:int
    fixture_only:bool=True
    rpc_version:str='engine.rpc/v1'
    task_schema:str=TASK_SCHEMA
    result_schema:str=RESULT_SCHEMA
    driver:str='none/fake'
    capabilities:tuple=('test.read','test.effect')
    availability:str='ready'
    def __post_init__(self):
        if not self.fixture_only:raise SdkError('descriptor_requires_real_acceptance')
        if self.engine_id!='test.fake':raise SdkError('descriptor_engine_not_fixture')
        if self.provider_version not in {'1.0.0','1.0.1'}:raise SdkError('descriptor_version_invalid')
        if self.sdk_version not in SUPPORTED_SDK_VERSIONS:raise SdkError('sdk_version_unsupported')
    def as_dict(self):return {k:getattr(self,k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class GenericDescriptor:
    engine_id: str
    publisher: str
    provider_version: str
    package_digest: str
    runtime_digest: str
    sdk_version: str
    capabilities: tuple
    fixture_only: bool = True
    server_certified: bool = False
    availability: str = 'requires_runtime_validation'
    def __post_init__(self):
        from .profiles import NATIVE_IDS
        if (type(self.engine_id) is not str or not _ENGINE.fullmatch(self.engine_id) or self.engine_id == 'test.fake'
                or type(self.publisher) is not str or not self.publisher or self.publisher=='*'
                or self.engine_id in NATIVE_IDS and self.publisher!='tracemind'
                or type(self.provider_version) is not str or not 1 <= len(self.provider_version) <= 128
                or type(self.package_digest) is not str or not _DIGEST.fullmatch(self.package_digest)
                or type(self.runtime_digest) is not str or not _DIGEST.fullmatch(self.runtime_digest)
                or self.sdk_version not in SUPPORTED_SDK_VERSIONS
                or type(self.fixture_only) is not bool
                or self.server_certified is not False
                or self.availability not in {'requires_runtime_validation','ready','driver_unavailable','disabled','isolated','incompatible'}
                or type(self.capabilities) is not tuple or not 1 <= len(self.capabilities) <= 32
                or any(type(x) is not str or not _OPERATION.fullmatch(x) for x in self.capabilities)
                or len(set(self.capabilities)) != len(self.capabilities)):
            raise SdkError('descriptor_generic_invalid')
    def as_dict(self):return {k:getattr(self,k) for k in self.__dataclass_fields__}


def standard_result(*,provider_version,values,context=None,optional_fields=None):
    result={'schema':RESULT_SCHEMA,'provider_version':provider_version,'values':values}
    if context is not None:result['context']=context
    if optional_fields is not None:result['optional_fields']=optional_fields
    return result


def decode_result(raw):
    value=strict_json(raw,max_bytes=64*1024*1024) if isinstance(raw,(bytes,bytearray)) else raw
    if not isinstance(value,dict) or value.get('schema')!=RESULT_SCHEMA or not isinstance(value.get('values'),dict):
        raise SdkError('result_schema_unsupported')
    if set(value)-{'schema','provider_version','values','context','optional_fields'}:
        raise SdkError('result_schema_unsupported')
    if 'context' in value:
        validate_context(value['context'])
        if value.get('provider_version')!=value['context']['provider_version']:
            raise SdkError('response_identity_mismatch')
    if not isinstance(value.get('optional_fields',{}),dict):raise SdkError('result_schema_unsupported')
    return value
