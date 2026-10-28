"""Signed Provider profile grammar; official source templates are only a builder aid."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ENGINE_ID = re.compile(r'^[a-z][a-z0-9]*(?:[._][a-z][a-z0-9]*)*$')
MODULE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$')
OPERATION = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
OFFICIAL = frozenset({'mysql','oracle','postgresql','sqlserver','dm','kingbasees'})
NATIVE_IDS = frozenset({'mysql','oracle','postgresql','sqlserver'})
PROFILES = {
    'mysql': {'module': 'mysql', 'driver': 'pymysql', 'operations': ('identify','version','sessions','locks','metrics')},
    'oracle': {'module': 'oracle', 'driver': 'oracledb', 'operations': ('identify','version','sessions','locks','metrics')},
    'postgresql': {'module': 'postgresql', 'driver': 'psycopg2', 'operations': ('identify','version','sessions','locks','metrics')},
    'sqlserver': {'module': 'sqlserver', 'driver': 'pymssql', 'operations': ('identify','version','sessions','locks','metrics')},
    'dm': {'module': 'dm', 'driver': 'dmPython', 'operations': ('identify','version','sessions','locks','metrics')},
    'kingbasees': {'module': 'kingbasees', 'driver': 'ksycopg2', 'operations': ('identify','version','sessions','locks','metrics')},
}


def profile_for(engine_id):
    """Template for first-party source copying, not a runtime engine allowlist."""
    try:return PROFILES[engine_id]
    except (KeyError, TypeError) as exc:raise ValueError('native_engine_unsupported') from exc


def validate_signed_profile(manifest, root=None):
    """Validate declarative profile after detached manifest signature verification."""
    engine=manifest.get('engine_id')
    profile=manifest.get('provider_profile')
    if (type(engine) is not str or not ENGINE_ID.fullmatch(engine) or engine=='test.fake'
            or not isinstance(profile,dict)
            or set(profile)!={'schema','adapter_file','adapter_sha256','driver','operations'}
            or profile['schema']!='tmengine.provider-profile/v1'
            or profile['adapter_file']!='engine_provider.py'
            or type(profile['adapter_sha256']) is not str
            or not re.fullmatch(r'sha256:[0-9a-f]{64}',profile['adapter_sha256'])
            or type(profile['driver']) is not str or not MODULE.fullmatch(profile['driver'])
            or not isinstance(profile['operations'],list)
            or not 1<=len(profile['operations'])<=32
            or any(type(x) is not str or not OPERATION.fullmatch(x) for x in profile['operations'])
            or len(set(profile['operations']))!=len(profile['operations'])
            or manifest.get('driver')!=profile['driver']
            or manifest.get('capabilities')!=profile['operations']
            or manifest.get('server_certified') is True
            or type(manifest.get('fixture_only')) is not bool
            or type(manifest.get('synthetic_driver',False)) is not bool
            or manifest.get('synthetic_driver') is True and manifest.get('fixture_only') is not True
            or engine in NATIVE_IDS and manifest.get('publisher')!='tracemind'):
        raise ValueError('provider_profile_invalid')
    if root is not None:
        adapter=Path(root)/'engine_provider.py'
        if (not adapter.is_file() or adapter.is_symlink()
                or 'sha256:'+hashlib.sha256(adapter.read_bytes()).hexdigest()!=profile['adapter_sha256']):
            raise ValueError('provider_adapter_mismatch')
    return profile


def validate_application(application, manifest):
    profile=validate_signed_profile(manifest)
    if (getattr(application,'ENGINE_ID',None)!=manifest['engine_id']
            or getattr(application,'DRIVER',None)!=profile['driver']
            or tuple(getattr(application,'CAPABILITIES',()))!=tuple(profile['operations'])
            or getattr(application,'PROVIDER_VERSION',None)!=manifest['version']
            or not callable(getattr(getattr(application,'Provider',None),'invoke',None))):
        raise ValueError('provider_application_mismatch')
    return profile
