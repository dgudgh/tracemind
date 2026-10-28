"""Signed self-contained synthetic DBAPI process cell; not a real-driver certificate."""
import os
import tempfile
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.runtime.engine_packs.trust import TrustedEngineKey
from app.runtime.engine_sdk.contracts import GenericDescriptor, digest_json
from app.runtime.engine_host.provider_packages import build_provider_package, build_extension_package
from app.runtime.engine_host.provider_supervisor import ProviderSupervisor
from app.runtime.engine_host.supervisor import FixtureSupervisor, SupervisorError
from app.runtime.secret_broker import SecretBroker


DRIVER = '''
class Cursor:
    def execute(self, sql):
        if sql.startswith('SELECT CONCAT('):
            self.description=[('version',)];self.rows=[('MySQL 8.0 synthetic',)]
        elif sql.startswith('SELECT @@server_uuid'):
            self.description=[('server_uuid',)];self.rows=[('fixture-server',)]
        elif 'information_schema.processlist' in sql:
            self.description=[('session_count',)];self.rows=[(2,)]
        else:
            self.description=[('version',)];self.rows=[('MySQL synthetic',)]
    def fetchmany(self, limit):return self.rows[:limit]
    def close(self):pass
class Connection:
    def cursor(self):return Cursor()
    def close(self):pass
def connect(**config):
    if config!={'host':'fixture-target','user':'fixture-user','password':'TEST_ONLY_SECRET_VALUE'}:
        raise ValueError('bad synthetic config')
    return Connection()
'''


def _setup(*, slow=False):
    root=Path(tempfile.mkdtemp(prefix='provider-rpc-',dir='/tmp'))
    src=root/'pymysql.py'
    src.write_text(DRIVER.replace("self.description=[('session_count',)];self.rows=[(2,)]",
        "import time; time.sleep(.5); self.description=[('session_count',)];self.rows=[(2,)]") if slow else DRIVER)
    private=Ed25519PrivateKey.generate()
    trusted=TrustedEngineKey.create(private.public_key().public_bytes_raw(),
        namespace='tmengine.test',channel='test',publisher='tracemind',
        engine_scopes=('mysql',),fixture_only=True)
    package=build_provider_package(root,engine_id='mysql',private_key=private,
        trusted_key=trusted,synthetic_driver_source=src)
    supervisor_holder={}
    broker=SecretBroker(root/'broker',authorize=lambda p,c,phase:{'scope':{'read':True}},
        resolve_secret=lambda ref,p,c:{'config':{'host':'fixture-target','user':'fixture-user',
            'password':'TEST_ONLY_SECRET_VALUE'},'expected_identity':{'server_uuid':'fixture-server'}},
        on_close_request=lambda launch,reason:None,
        on_terminate=lambda launch,reason:supervisor_holder['supervisor'].quarantine('lease_expired'),
        scan_interval=.02,grace_seconds=.05).start()
    asset=lambda asset_id,op:{'target_digest':digest_json({'asset':'fixture-target'}),
        'policy_digest':digest_json({'policy':'test-read'}),'scope':{'read':True},'fence_epoch':1,
        'principal':{'id':'fixture-user'},'secret_ref':'vault/fixture'}
    supervisor=ProviderSupervisor(workdir=root/'worker',broker=broker,asset_resolver=asset,
        restart=False)
    supervisor_holder['supervisor']=supervisor
    return root,package,broker,supervisor


def test_signed_native_read_uses_broker_and_rejects_business_credentials():
    root,package,broker,supervisor=_setup()
    try:
        with pytest.raises(SupervisorError,match='fixture_only_required'):
            fake=FixtureSupervisor(workdir=root/'fake',restart=False)
            try:fake.start(package.source,package.manifest,package.signature,package.trusted_keys,
                            expected_namespace='tmengine.test',expected_channel='test')
            finally:fake.stop()
        record=supervisor.start(package.source,package.manifest,package.signature,package.trusted_keys,
                                expected_namespace='tmengine.test',expected_channel='test')
        assert record.pid>0
        assert supervisor.identity['engine_id']=='mysql'
        issued=[]
        original_grant=broker.grant
        def capture_grant(*args,**kwargs):
            lease=original_grant(*args,**kwargs);issued.append(lease);return lease
        broker.grant=capture_grant
        result=supervisor.invoke_read('asset-1','sessions')
        assert result['rows']==[{'session_count':2}]
        assert result['instance_identity']=={'server_uuid':'fixture-server'}
        assert result['engine_id']=='mysql'
        with pytest.raises(SupervisorError,match='sdk_params_invalid'):
            supervisor.invoke_read('asset-1','sessions',arguments={'rawSQL':'DROP TABLE x'})
        with pytest.raises(SupervisorError,match='capability_not_registered'):
            supervisor.invoke_read('asset-1','arbitrary_sql')
        with pytest.raises(SupervisorError,match='use_host_granted_read'):
            supervisor.call('invoke_read',{'operation':'sessions','arguments':{},'lease_id':'fake'})
        with pytest.raises(SupervisorError,match='native_write_unsupported'):
            supervisor.call('execute',{})
        with pytest.raises(SupervisorError,match='request_identity_mismatch'):
            supervisor.call('describe',context={'engine_id':'oracle'})
        stale={'operation':'sessions','arguments':{},'lease_id':issued[-1]}
        replay=FixtureSupervisor.submit(supervisor,'invoke_read',stale,timeout=2,
                                        context={'capability_id':'sessions'})
        with pytest.raises(SupervisorError,match='broker_scope_mismatch'):
            replay.result(timeout=3)
        assert supervisor.heartbeat()['target_connections']==0
        assert b'TEST_ONLY_SECRET_VALUE' not in broker.db_path.read_bytes()
        assert b'TEST_ONLY_SECRET_VALUE' not in ' '.join(__import__('psutil').Process(record.pid).cmdline()).encode()
        assert b'TEST_ONLY_SECRET_VALUE' not in repr(__import__('psutil').Process(record.pid).environ()).encode()
    finally:
        supervisor.stop();broker.close()


def test_expired_live_native_lease_quarantines_slow_synthetic_worker():
    root,package,broker,supervisor=_setup(slow=True)
    try:
        record=supervisor.start(package.source,package.manifest,package.signature,package.trusted_keys,
                                expected_namespace='tmengine.test',expected_channel='test')
        future=supervisor.submit_read('asset-1','sessions',timeout=2,lease_ttl=.05)
        with pytest.raises(SupervisorError):
            future.result(timeout=3)
        until=time.time()+2
        while supervisor.state.value!='quarantined' and time.time()<until:
            time.sleep(.01)
        assert supervisor.state.value=='quarantined'
        until=time.time()+2
        while supervisor._child.poll() is None and time.time()<until:
            time.sleep(.01)
        assert supervisor._child.poll() is not None
    finally:
        supervisor.stop();broker.close()


def test_missing_real_driver_never_becomes_ready():
    if __import__('importlib').util.find_spec('psycopg2') is not None:
        pytest.skip('psycopg2 present in this interpreter')
    root,_,broker,supervisor=_setup()
    private=Ed25519PrivateKey.generate()
    trusted=TrustedEngineKey.create(private.public_key().public_bytes_raw(),
        namespace='tmengine.test',channel='test',publisher='tracemind',
        engine_scopes=('postgresql',),fixture_only=True)
    package=build_provider_package(root,engine_id='postgresql',private_key=private,
                                   trusted_key=trusted)
    try:
        assert package.manifest['fixture_only'] is False
        assert package.manifest['server_certified'] is False
        with pytest.raises(SupervisorError,match='driver_unavailable'):
            supervisor.start(package.source,package.manifest,package.signature,package.trusted_keys,
                             expected_namespace='tmengine.test',expected_channel='test')
        assert supervisor.state.value!='ready'
    finally:
        supervisor.stop();broker.close()


def test_signed_extension_engine_without_core_engine_list_change():
    root,_,broker,supervisor=_setup()
    private=Ed25519PrivateKey.generate()
    trusted=TrustedEngineKey.create(private.public_key().public_bytes_raw(),
        namespace='tmengine.test',channel='test',publisher='test-extension',
        engine_scopes=('demo.engine',),fixture_only=True)
    adapter=b'''ENGINE_ID = 'demo.engine'
DRIVER = 'demo_driver'
CAPABILITIES = ('sessions',)
PROVIDER_VERSION = '0.3.0'
MARKER = 'signed-extension-fixture'
class Provider:
    def invoke(self, operation, config, params=None, *, expected_identity=None):
        import demo_driver
        if operation != 'sessions' or params not in (None, {}):
            raise ValueError('unsupported')
        conn = demo_driver.connect(**config)
        try:
            cursor = conn.cursor()
            try:
                cursor.execute('SELECT @@server_uuid AS server_uuid')
                rows = cursor.fetchmany(2)
                if rows != [('fixture-server',)] or expected_identity != {'server_uuid':'fixture-server'}:
                    raise ValueError('identity_mismatch')
                return {'schema':'engine.rows/v1','engine_id':ENGINE_ID,'capability_id':operation,
                        'rows':[{'session_count':2}], 'instance_identity':expected_identity}
            finally:
                cursor.close()
        finally:
            conn.close()
'''
    package=build_extension_package(root,engine_id='demo.engine',adapter_source=adapter,
        driver='demo_driver',operations=('sessions',),private_key=private,trusted_key=trusted,
        synthetic_driver_source=root/'pymysql.py')
    try:
        assert package.manifest['publisher']=='test-extension'
        assert package.manifest['fixture_only'] is True
        assert package.manifest['synthetic_driver'] is True
        descriptor=GenericDescriptor(engine_id='demo.engine',publisher='test-extension',
            provider_version=package.manifest['version'],package_digest=package.digest,
            runtime_digest=package.manifest['runtime_digest'],sdk_version='1.0',
            capabilities=('sessions',))
        assert descriptor.as_dict()['server_certified'] is False
        supervisor.start(package.source,package.manifest,package.signature,package.trusted_keys,
                         expected_namespace='tmengine.test',expected_channel='test')
        result=supervisor.invoke_read('asset-1','sessions')
        assert result['engine_id']=='demo.engine'
        assert result['rows']==[{'session_count':2}]
        with pytest.raises(SupervisorError,match='capability_not_registered'):
            supervisor.invoke_read('asset-1','version')
    finally:
        supervisor.stop();broker.close()
