"""First-party source templates and generic SDK descriptors must agree."""
from importlib import import_module

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.runtime.engine_sdk.contracts import GenericDescriptor, SdkError
from app.runtime.engine_sdk.profiles import PROFILES, validate_signed_profile
from app.runtime.engine_host.provider_packages import build_provider_package, build_extension_package
from app.runtime.engine_packs.trust import TrustedEngineKey


@pytest.mark.parametrize('engine_id', tuple(PROFILES))
def test_packaged_driver_and_operations_match_actual_source(engine_id, tmp_path):
    profile = PROFILES[engine_id]
    provider = import_module('app.engine_providers.' + profile['module']).Provider
    assert provider.engine_id == engine_id
    assert provider.driver == profile['driver']
    assert tuple(provider.queries) == profile['operations']
    private = Ed25519PrivateKey.generate()
    key = TrustedEngineKey.create(private.public_key().public_bytes_raw(),
        namespace='tmengine.test', channel='test', publisher='tracemind',
        engine_scopes=(engine_id,), fixture_only=True)
    package = build_provider_package(tmp_path, engine_id=engine_id, private_key=private, trusted_key=key)
    assert validate_signed_profile(package.manifest, package.source)['driver'] == provider.driver


@pytest.mark.parametrize('engine_id', ('dm', 'kingbasees', 'vendor.engine'))
def test_domestic_and_new_engine_publishers_are_not_reserved_to_core(engine_id, tmp_path):
    private = Ed25519PrivateKey.generate()
    key = TrustedEngineKey.create(private.public_key().public_bytes_raw(),
        namespace='tmengine.test', channel='test', publisher='authorized-vendor',
        engine_scopes=(engine_id,), fixture_only=True)
    package = build_extension_package(tmp_path, engine_id=engine_id, adapter_source=b'# vendor adapter\n',
        driver='vendor_driver', operations=('identify',), private_key=private, trusted_key=key,
        development_fixture=True)
    assert package.verify()['engine_id'] == engine_id
    descriptor = dict(engine_id=engine_id, publisher=key.publisher, provider_version='1.0.0',
        package_digest=package.digest, runtime_digest=package.manifest['runtime_digest'],
        sdk_version='1.0', capabilities=('identify',))
    assert GenericDescriptor(**descriptor).publisher == 'authorized-vendor'
    for change in ({'capabilities':['identify']}, {'sdk_version':'0.8'}, {'package_digest':'d1'},
                   {'capabilities':('identify','identify')}, {'fixture_only':1}, {'engine_id':None}):
        with pytest.raises(SdkError, match='descriptor_generic_invalid'):
            GenericDescriptor(**(descriptor | change))
