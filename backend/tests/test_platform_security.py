import time
import unittest

import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.cmdb.models import (
    AssetCluster,
    AssetEnvironment,
    AssetHost,
    AssetInstance,
    AssetRegion,
    AssetSite,
    AssetZone,
    CmdbAsset,
    CmdbRelation,
    CmdbSource,
)
from app.cmdb.service import list_inventory
from app.core.auth import AUTH_TOKENS, get_token_info, issue_user_token, logout
from app.core.database import Base
from app.core.jwt_keys import jwks_document
from app.core.mfa import generate_secret, totp_code, verify_totp
from app.core.security import hash_password
from app.models.access_control import (
    AccessRole,
    RoleActionPermission,
    SecurityAuditEvent,
    UserAccessRole,
    UserAssetScope,
)
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services.access_control import (
    append_security_audit,
    ensure_builtin_roles,
    is_action_allowed,
    resolve_scope,
    scope_allows_path,
    verify_audit_chain,
)


def _request(user: User) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.auth = {"user_id": user.id, "username": user.username, "role": user.role, "source": "db"}
    return request


class PlatformSecurityTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        AUTH_TOKENS.clear()
        self.user = User(
            username="scoped.user",
            password_hash=hash_password("StrongPass1"),
            display_name="Scoped User",
            role="user",
            status="active",
        )
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

    def tearDown(self):
        AUTH_TOKENS.clear()
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _assets(self):
        region = AssetRegion(code="east", name="华东")
        self.db.add(region)
        self.db.flush()
        site = AssetSite(region_id=region.id, code="sh", name="上海")
        self.db.add(site)
        self.db.flush()
        zone = AssetZone(region_id=region.id, site_id=site.id, code="a", name="A 区")
        self.db.add(zone)
        self.db.flush()
        env = AssetEnvironment(region_id=region.id, site_id=site.id, zone_id=zone.id, code="prod", name="生产", env_type="production")
        self.db.add(env)
        self.db.flush()
        host1 = AssetHost(stable_key="host:one", name="one", ip="10.0.0.1", region_id=region.id, site_id=site.id, zone_id=zone.id, env_id=env.id)
        host2 = AssetHost(stable_key="host:two", name="two", ip="10.0.0.2", region_id=region.id, site_id=site.id, zone_id=zone.id, env_id=env.id)
        self.db.add_all([host1, host2])
        self.db.flush()
        cluster1 = AssetCluster(stable_key="cluster:one", name="one", db_type="MySQL", region_id=region.id, site_id=site.id, zone_id=zone.id, env_id=env.id)
        cluster2 = AssetCluster(stable_key="cluster:two", name="two", db_type="MySQL", region_id=region.id, site_id=site.id, zone_id=zone.id, env_id=env.id)
        self.db.add_all([cluster1, cluster2])
        self.db.flush()
        native1 = DBInstance(instance_name="one", db_type="mysql", host="10.0.0.1", port=3306, username="monitor", password_cipher="cipher")
        native2 = DBInstance(instance_name="two", db_type="mysql", host="10.0.0.2", port=3306, username="monitor", password_cipher="cipher")
        self.db.add_all([native1, native2])
        self.db.flush()
        asset1 = AssetInstance(stable_key="instance:one", name="one", db_type="MySQL", ip="10.0.0.1", port=3306, host_id=host1.id, cluster_id=cluster1.id, region_id=region.id, site_id=site.id, zone_id=zone.id, env_id=env.id, native_instance_id=native1.id)
        asset2 = AssetInstance(stable_key="instance:two", name="two", db_type="MySQL", ip="10.0.0.2", port=3306, host_id=host2.id, cluster_id=cluster2.id, region_id=region.id, site_id=site.id, zone_id=zone.id, env_id=env.id, native_instance_id=native2.id)
        self.db.add_all([asset1, asset2])
        self.db.commit()
        return asset1, asset2, native1, native2

    def test_builtin_roles_and_explicit_action_policy(self):
        roles = ensure_builtin_roles(self.db)
        viewer = next(row for row in roles if row.role_key == "viewer")
        self.db.add(UserAccessRole(user_id=self.user.id, role_id=viewer.id))
        self.db.commit()

        self.assertEqual(is_action_allowed(self.db, self.user.id, "asset.read"), (True, True))
        self.assertEqual(is_action_allowed(self.db, self.user.id, "asset.manage"), (False, True))

    def test_users_without_explicit_roles_are_denied(self):
        self.assertEqual(is_action_allowed(self.db, self.user.id, "asset.read"), (False, False))

    def test_asset_scope_maps_normalized_and_native_instances(self):
        asset1, asset2, native1, native2 = self._assets()
        self.db.add(UserAssetScope(user_id=self.user.id, scope_type="instance", scope_id=asset1.id, access_level="read"))
        self.db.commit()

        scope = resolve_scope(self.db, self.user.id)

        self.assertTrue(scope.restricted)
        self.assertEqual(scope.normalized_instance_ids, frozenset({asset1.id}))
        self.assertEqual(scope.native_instance_ids, frozenset({native1.id}))
        self.assertTrue(scope_allows_path(self.db, self.user.id, f"/api/v1/instances/{native1.id}"))
        self.assertFalse(scope_allows_path(self.db, self.user.id, f"/api/v1/instances/{native2.id}"))
        self.assertFalse(scope_allows_path(self.db, self.user.id, f"/api/v1/instances/{native1.id}", method="PUT"))
        self.assertNotIn(asset2.id, scope.normalized_instance_ids)

        self.db.query(UserAssetScope).filter(UserAssetScope.user_id == self.user.id).update({"access_level": "operate"})
        self.db.commit()
        self.assertTrue(scope_allows_path(self.db, self.user.id, f"/api/v1/instances/{native1.id}", method="PUT"))

    def test_signed_session_survives_cache_and_can_be_revoked(self):
        result = issue_user_token(self.user, db=self.db, mfa_verified=True)
        token = result["token"]
        header = jwt.get_unverified_header(token)
        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(header["kid"], jwks_document()["keys"][0]["kid"])

        AUTH_TOKENS.clear()
        info = get_token_info(token, db=self.db)
        self.assertEqual(info["user_id"], self.user.id)
        self.assertTrue(info["mfa_verified"])

        logout(token, db=self.db)
        AUTH_TOKENS.clear()
        self.assertIsNone(get_token_info(token, db=self.db))

    def test_totp_replay_is_rejected(self):
        secret = generate_secret()
        now = 1_800_000_000.0
        counter = int(now // 30)
        code = totp_code(secret, counter)
        accepted_counter = verify_totp(secret, code, now=now)
        self.assertEqual(accepted_counter, counter)
        self.assertIsNone(verify_totp(secret, code, now=now, last_counter=accepted_counter))

    def test_security_audit_is_redacted_and_hash_chained(self):
        append_security_audit(self.db, category="authorization", action="first", actor="admin", details={"token": "plain-token", "note": "password=plain"})
        append_security_audit(self.db, category="authorization", action="second", actor="admin")
        self.db.commit()
        rows = self.db.query(SecurityAuditEvent).order_by(SecurityAuditEvent.id).all()

        self.assertEqual(rows[0].details["token"], "***")
        self.assertNotIn("plain", str(rows[0].details))
        self.assertTrue(verify_audit_chain(rows)["valid"])
        rows[0].action = "tampered"
        self.assertFalse(verify_audit_chain(rows)["valid"])

    def test_legacy_cmdb_inventory_is_filtered_by_native_instance_scope(self):
        _, _, native1, native2 = self._assets()
        source = CmdbSource(source_name="internal", source_type="internal_discovery", enabled=1)
        self.db.add(source)
        self.db.flush()
        db1 = CmdbAsset(source_id=source.id, external_id="db:one", asset_type="database_instance", name="db-one", status="active", attributes={"native_instance_id": native1.id})
        db2 = CmdbAsset(source_id=source.id, external_id="db:two", asset_type="database_instance", name="db-two", status="active", attributes={"native_instance_id": native2.id})
        host1 = CmdbAsset(source_id=source.id, external_id="host:one", asset_type="host", name="host-one", status="active", attributes={"database_instance_ids": [native1.id]})
        host2 = CmdbAsset(source_id=source.id, external_id="host:two", asset_type="host", name="host-two", status="active", attributes={"database_instance_ids": [native2.id]})
        cluster1 = CmdbAsset(source_id=source.id, external_id="cluster:one", asset_type="database_cluster", name="cluster-one", status="active", attributes={})
        cluster2 = CmdbAsset(source_id=source.id, external_id="cluster:two", asset_type="database_cluster", name="cluster-two", status="active", attributes={})
        self.db.add_all([db1, db2, host1, host2, cluster1, cluster2])
        self.db.flush()
        self.db.add_all([
            CmdbRelation(source_id=source.id, source_asset_id=db1.id, target_asset_id=host1.id, relation_type="runs_on", status="active"),
            CmdbRelation(source_id=source.id, source_asset_id=db1.id, target_asset_id=cluster1.id, relation_type="member_of", status="active"),
            CmdbRelation(source_id=source.id, source_asset_id=db2.id, target_asset_id=host2.id, relation_type="runs_on", status="active"),
            CmdbRelation(source_id=source.id, source_asset_id=db2.id, target_asset_id=cluster2.id, relation_type="member_of", status="active"),
        ])
        self.db.commit()

        names = {row["name"] for row in list_inventory(self.db, allowed_native_instance_ids={native1.id})}

        self.assertEqual(names, {"db-one", "host-one", "cluster-one"})


if __name__ == "__main__":
    unittest.main()
