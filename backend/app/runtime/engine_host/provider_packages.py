"""Signed test-channel Provider packages with fixed SDK entry and adapter path.

External callers supply an in-memory test key. No private key is generated,
serialized or available to the installed package.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.runtime.engine_packs.contract import build_payload_inventory, scan_payload_directory
from app.runtime.engine_packs.trust import TrustedEngineKey, canonical_manifest_bytes, verify_engine_manifest_signature
from app.runtime.engine_sdk.profiles import NATIVE_IDS, MODULE, OPERATION, ENGINE_ID, profile_for, validate_signed_profile
from .packages import SDK_ROOT, ROOT, describe_context, runtime_digest

ENTRYPOINT = 'python.sdk.provider.v1'
_SOURCE_ROOT = ROOT / 'backend/app/engine_providers'


def _sha(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _adapter_bytes(source):
    if isinstance(source,(bytes,bytearray)):
        data=bytes(source)
    else:
        path=Path(source)
        if not path.is_file() or path.is_symlink():raise ValueError('provider_adapter_invalid')
        data=path.read_bytes()
    if not data or len(data)>1024*1024:
        raise ValueError('provider_adapter_invalid')
    try:
        tree=ast.parse(data)
        for node in ast.walk(tree):
            if isinstance(node,ast.Import) and any(x.name=='app' or x.name.startswith('app.') for x in node.names):
                raise ValueError('provider_private_app_import')
            if isinstance(node,ast.ImportFrom) and (node.module=='app' or (node.module or '').startswith('app.')):
                raise ValueError('provider_private_app_import')
    except (SyntaxError,UnicodeError) as exc:raise ValueError('provider_adapter_invalid') from exc
    return data


@dataclass(frozen=True)
class ProviderPackage:
    source: Path
    manifest: dict
    signature: dict
    trusted_keys: tuple
    digest: str
    payload_digest: str

    def verify(self):
        m=self.manifest
        if m.get('entrypoint_id')!=ENTRYPOINT:
            raise ValueError('provider_profile_mismatch')
        validate_signed_profile(m,self.source)
        verified=verify_engine_manifest_signature(m,self.signature,trusted_keys=self.trusted_keys,
            expected_namespace=m['namespace'],expected_channel=m['channel'])
        inventory=build_payload_inventory(self.source,m)
        if (_sha(canonical_manifest_bytes(m))!=self.digest
                or inventory['self_payload_digest']!=self.payload_digest
                or 'sha256:'+verified.manifest_sha256!=self.digest):
            raise ValueError('provider_package_mismatch')
        return {'engine_id':m['engine_id'],'package_digest':self.digest,
                'payload_digest':self.payload_digest,'runtime_digest':m['runtime_digest'],
                'provider_version':m['version'],'sdk_version':m['sdk_version']}


def _build(root, *, engine_id, adapter_source, driver, operations, private_key,
           trusted_key, version, synthetic_driver_source=None, adapter_sources=None,
           development_fixture=False):
    if (type(engine_id) is not str or not ENGINE_ID.fullmatch(engine_id) or engine_id=='test.fake'
            or type(driver) is not str or not MODULE.fullmatch(driver)
            or not isinstance(operations,(list,tuple)) or not 1<=len(operations)<=32
            or any(type(x) is not str or not OPERATION.fullmatch(x) for x in operations)
            or len(set(operations))!=len(operations)):
        raise ValueError('provider_profile_invalid')
    if (type(development_fixture) is not bool or not isinstance(private_key,Ed25519PrivateKey)
            or trusted_key.publisher in ('','*') or engine_id not in trusted_key.engine_scopes
            or trusted_key.namespace!='tmengine.test' or trusted_key.channel!='test'
            or trusted_key.fixture_only is not True):
        raise ValueError('provider_signing_scope_invalid')
    if engine_id in NATIVE_IDS and trusted_key.publisher!='tracemind':
        raise ValueError('provider_publisher_scope_invalid')
    if private_key.public_key().public_bytes_raw()!=trusted_key.raw_public_key:
        raise ValueError('provider_signing_key_mismatch')
    adapter=_adapter_bytes(adapter_source)
    root=Path(root).resolve()
    source=root/('pkg-'+engine_id+'-'+version)
    source.mkdir(parents=True,mode=0o700,exist_ok=False)
    sdk=source/'sdk_runtime';sdk.mkdir()
    (sdk/'__init__.py').write_text('')
    for filename in ('wire.py','contracts.py','provider.py','broker_client.py','profiles.py'):
        shutil.copyfile(SDK_ROOT/filename,sdk/filename)
    (source/'engine_provider.py').write_bytes(adapter)
    if adapter_sources:
        directory=source/'adapter_sources';directory.mkdir()
        (directory/'__init__.py').write_text('')
        for filename,contents in adapter_sources.items():
            if type(filename) is not str or not filename.endswith('.py') or '/' in filename or filename=='__init__.py':
                raise ValueError('provider_adapter_invalid')
            (directory/filename).write_bytes(_adapter_bytes(contents))
    synthetic=synthetic_driver_source is not None
    if synthetic:
        src=Path(synthetic_driver_source)
        if not src.is_file() or src.is_symlink() or src.suffix!='.py' or '.' in driver:
            raise ValueError('synthetic_driver_invalid')
        shutil.copyfile(src,source/(driver+'.py'))
    components=[{'path':p.relative_to(source).as_posix(),'version':version,
                 'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
                 'license':'self-built-test' if synthetic else 'self-built',
                 'redistribution_status':'not-required'}
                for p in sorted(source.rglob('*.py'))]
    (source/'SBOM.json').write_bytes(canonical_manifest_bytes({'schema':'tmengine.sbom/v1','components':components}))
    files=scan_payload_directory(source)
    context=describe_context()
    profile={'schema':'tmengine.provider-profile/v1','adapter_file':'engine_provider.py',
             'adapter_sha256':_sha(adapter),'driver':driver,'operations':list(operations)}
    manifest={'format':1,'package_kind':'tmengine','package_id':trusted_key.publisher+'.'+engine_id,
              'version':version,'namespace':trusted_key.namespace,'channel':trusted_key.channel,
              'purpose':'tmengine.manifest-signing.v1','publisher':trusted_key.publisher,'engine_id':engine_id,
              'signing_key_id':trusted_key.key_id,'entrypoint_id':ENTRYPOINT,
              'sdk_version':'1.0','sdk_versions':['0.9','1.0'],
              'rpc_version':'engine.rpc/v1','task_schema':'engine.task/v1','result_schema':'engine.result/v1',
              'driver':driver,'provider_profile':profile,'os':context['os'],'cpu':context['cpu'],
              'python_abi':context['python_abi'],'runtime_digest':runtime_digest(),
              'capabilities':list(operations),'fixture_only':synthetic or development_fixture,
              'synthetic_driver':synthetic,'server_certified':False,
              'limits':{'max_inflight':8,'max_connections':32,'max_output_bytes':64*1024*1024},
              'package_file_inventory':files,'whole_package_signed_files':[x['path'] for x in files],
              'file_classification':{x['path']:'self-built' for x in files}}
    signature={'algorithm':'ed25519','key_id':trusted_key.key_id,
               'value':base64.b64encode(private_key.sign(canonical_manifest_bytes(manifest))).decode()}
    package=ProviderPackage(source,manifest,signature,(trusted_key,),
                            _sha(canonical_manifest_bytes(manifest)),
                            build_payload_inventory(source,manifest)['self_payload_digest'])
    package.verify()
    return package


def build_provider_package(root, *, engine_id, private_key: Ed25519PrivateKey,
                           trusted_key: TrustedEngineKey, version='0.3.0',
                           synthetic_driver_source=None, development_fixture=False):
    """Copy one official source slice into a signed test package."""
    profile=profile_for(engine_id)
    adapter=(
        '"""Signed fixed adapter entry. Business input cannot choose an import."""\n'
        f'from adapter_sources.{profile["module"]} import Provider\n'
        f'ENGINE_ID = {engine_id!r}\nDRIVER = {profile["driver"]!r}\n'
        f'CAPABILITIES = {profile["operations"]!r}\n'
        f'PROVIDER_VERSION = {version!r}\nMARKER = "signed-native-adapter"\n').encode()
    sources={'base.py':_SOURCE_ROOT.joinpath('base.py'),
             profile['module']+'.py':_SOURCE_ROOT.joinpath(profile['module']+'.py')}
    return _build(root,engine_id=engine_id,adapter_source=adapter,driver=profile['driver'],
                  operations=profile['operations'],private_key=private_key,trusted_key=trusted_key,
                  version=version,synthetic_driver_source=synthetic_driver_source,
                  adapter_sources=sources,development_fixture=development_fixture)


def build_extension_package(root, *, engine_id, adapter_source, driver, operations,
                            private_key: Ed25519PrivateKey, trusted_key: TrustedEngineKey,
                            version='0.3.0', synthetic_driver_source=None,
                            adapter_sources=None,development_fixture=False):
    """Sign an external fixed adapter for an authorized extension engine ID."""
    if engine_id in NATIVE_IDS:
        raise ValueError('provider_official_builder_required')
    return _build(root,engine_id=engine_id,adapter_source=adapter_source,driver=driver,
                  operations=operations,private_key=private_key,trusted_key=trusted_key,
                  version=version,synthetic_driver_source=synthetic_driver_source,
                  adapter_sources=adapter_sources,development_fixture=development_fixture)
