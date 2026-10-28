"""Synthetic production namespace gating; never an E07 handoff receipt."""
import hashlib
import json
import os

import pytest

from app.runtime.engine_packs.management import LabManagement, ManagementError, load_policy
from app.runtime.engine_packs.repository import EngineRepository
from app.runtime.engine_packs.trust_metadata import TrustStore
from app.runtime.engine_sdk.wire import canonical
from test_engine_trust_metadata import Fixture, NOW, digest


def _policy(path):
    value = {"schema": "tmengine.management-policy/v1", "role": "local-engine-admin",
             "allowed_uids": [os.getuid()], "allowed_methods": ["list", "status"]}
    path.write_text(json.dumps(value)); path.chmod(0o600)
    return value


def _admission(policy):
    return {"schema": "tmengine.production-management-admission/v1", "accepted": True,
        "namespace": "tmengine.production", "fixture_only": False,
        "handoff_digest": "sha256:" + "a" * 64,
        "e07_status": "production-trust-accepted", "package_digest": None,
        "production_release_ready": False,
        "admin_policy_digest": "sha256:" + hashlib.sha256(canonical(policy)).hexdigest(),
        "trust_root_version": 1, "service_uid": os.getuid(), "admin_gid": os.getgid()}


def test_missing_components_and_unbootstrapped_trust_fail_closed(tmp_path):
    trust = TrustStore(tmp_path / "trust", "tmengine.production")
    root = (tmp_path / "repository").resolve()
    repository = EngineRepository(root, namespace="tmengine.production", channel="production",
                                  trust_guard=trust, mode="managed",
                                  entrypoint_ids=("python.sdk.provider.v1",))
    policy_path = tmp_path / "admin-policy.json"
    policy = _policy(policy_path)
    wrong_role = dict(policy, role="platform-login-admin")
    policy_path.write_text(json.dumps(wrong_role))
    with pytest.raises(ManagementError, match="management_policy_invalid"):
        load_policy(policy_path, mode="production")
    _policy(policy_path)
    with pytest.raises(ManagementError, match="production_management_admission_missing"):
        LabManagement(root, policy_path=policy_path, mode="production", repository=repository)
    args = {"policy_path": policy_path, "mode": "production", "repository": repository,
            "host_factory": lambda **_: None, "capacity_admitter": lambda *_: {"admitted": True},
            "production_admission": lambda **_: _admission(policy), "admin_gid": os.getgid()}
    with pytest.raises(ManagementError, match="production_management_trust_not_ready"):
        LabManagement(root, **args)
    bad = dict(_admission(policy), fixture_only=True)
    with pytest.raises(ManagementError, match="production_management_admission_rejected"):
        LabManagement(root, **dict(args, production_admission=lambda **_: bad))


def test_synthetic_production_root_stays_fixture_only(tmp_path):
    fixture = Fixture(namespace="tmengine.production")
    trust = TrustStore(tmp_path / "trust", "tmengine.production")
    trust.bootstrap(fixture.root, expected_root_sha256=digest(fixture.root), now=NOW, fixture_only=True)
    trust.update(fixture.bundle(), now=NOW)
    assert trust.status()["fixture_only"] is True
    root = (tmp_path / "repository").resolve()
    repository = EngineRepository(root, namespace="tmengine.production", channel="production",
                                  trust_guard=trust, mode="managed",
                                  entrypoint_ids=("python.sdk.provider.v1",))
    policy_path = tmp_path / "admin-policy.json"
    policy = _policy(policy_path)
    with pytest.raises(ManagementError, match="production_management_trust_not_ready"):
        LabManagement(root, policy_path=policy_path, mode="production", repository=repository,
                      host_factory=lambda **_: None, capacity_admitter=lambda *_: {"admitted": True},
                      production_admission=lambda **_: _admission(policy), admin_gid=os.getgid())
