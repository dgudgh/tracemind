import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import AuthMiddleware
from app.models.access_control import AccessRole, RoleActionPermission, UserAccessRole
from app.services.access_control import (
    action_catalog,
    action_for_request,
    ensure_builtin_roles,
    is_action_allowed,
)


class AuthorizationBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        for table in (AccessRole.__table__, RoleActionPermission.__table__, UserAccessRole.__table__):
            table.create(self.engine)
        # Scope resolution needs its own table even when a user is unrestricted.
        from app.models.access_control import UserAssetScope
        UserAssetScope.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.roles = {role.role_key: role for role in ensure_builtin_roles(self.db)}
        self.info = {"source": "db", "user_id": 7, "role": "user", "username": "test-operator"}
        self.executed = []

        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        async def endpoint(request: Request):
            self.executed.append(request.url.path)
            return {"ok": True}

        for path in (
            "/api/v1/users", "/api/v1/oracle/42/sessions", "/api/v1/backup-center/plan",
            "/api/v1/data-governance/jobs/42/status", "/api/v1/data-governance/jobs/42/execute",
            "/api/v1/data-governance/legal-holds/42/release", "/api/v1/slow-sql/collect",
            "/api/v1/performance/refresh", "/api/v1/ai/diagnose", "/api/v1/unmapped-business",
            "/api/v1/auth/new-admin-action", "/api/v1/auth/current-user", "/api/v1/auth/logout",
            "/api/v1/instances/42", "/api/v1/security/audit", "/api/v1/security/audit/verify",
            "/api/v1/automation/approvals", "/api/v1/backup-center/operations",
            "/api/v1/recovery/agent/tasks/pending", "/api/v1/recovery/tasks",
            "/api/v1/incident-snapshots/42/download", "/api/v1/oracle/42/awr/reports/1/download",
        ):
            app.add_api_route(path, endpoint, methods=["GET", "POST", "PUT"])
        self.client = TestClient(app)
        self.auth_patch = patch("app.main.get_token_info", side_effect=lambda token: dict(self.info))
        self.session_patch = patch("app.core.database.SessionLocal", self.Session)
        self.auth_patch.start()
        self.session_patch.start()

    def tearDown(self):
        self.client.close()
        self.auth_patch.stop()
        self.session_patch.stop()
        self.db.close()
        self.engine.dispose()

    def assign(self, role_key):
        self.db.query(UserAccessRole).delete()
        self.db.add(UserAccessRole(user_id=7, role_id=self.roles[role_key].id))
        self.db.commit()

    def request(self, method, path, **kwargs):
        return self.client.request(method, path, headers={"Authorization": "Bearer test-session"}, **kwargs)

    def test_mes_identity_never_inherits_administrator_or_business_access(self):
        for role in ("user", "admin"):
            self.info.update(source="mes", user_id=0, role=role)
            for method, path in (
                ("GET", "/api/v1/users"), ("POST", "/api/v1/backup-center/plan"),
                ("GET", "/api/v1/oracle/42/sessions"), ("POST", "/api/v1/ai/diagnose"),
            ):
                with self.subTest(role=role, path=path):
                    self.assertEqual(self.request(method, path).status_code, 403)
        self.assertEqual(self.executed, [])
        self.assertEqual(self.request("GET", "/api/v1/auth/current-user").status_code, 200)

    def test_missing_role_and_unknown_route_fail_closed(self):
        self.assertEqual(self.request("GET", "/api/v1/oracle/42/sessions").status_code, 403)
        self.assign("platform_admin")
        for method in ("GET", "POST"):
            self.assertEqual(self.request(method, "/api/v1/unmapped-business").status_code, 403)
        self.assertEqual(self.request("POST", "/api/v1/auth/new-admin-action").status_code, 403)
        self.assertEqual(self.executed, [])

    def test_self_service_does_not_require_business_role(self):
        self.assertEqual(self.request("GET", "/api/v1/auth/current-user").status_code, 200)
        self.assertEqual(self.request("POST", "/api/v1/auth/logout").status_code, 200)

    def test_viewer_can_read_but_cannot_execute_unmapped_old_write_routes(self):
        self.assign("viewer")
        self.assertEqual(self.request("GET", "/api/v1/oracle/42/sessions").status_code, 200)
        self.executed.clear()
        for path in (
            "/api/v1/slow-sql/collect", "/api/v1/performance/refresh", "/api/v1/ai/diagnose",
            "/api/v1/data-governance/jobs/42/status", "/api/v1/data-governance/jobs/42/execute",
            "/api/v1/data-governance/legal-holds/42/release",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path).status_code, 403)
        self.assertEqual(self.executed, [])

    def test_approval_execution_and_legal_hold_permissions_are_distinct(self):
        self.assign("dba_operator")
        self.assertEqual(self.request("POST", "/api/v1/data-governance/jobs/42/execute").status_code, 200)
        self.assertEqual(self.request("POST", "/api/v1/data-governance/jobs/42/status").status_code, 403)
        self.assertEqual(self.request("POST", "/api/v1/data-governance/legal-holds/42/release").status_code, 403)
        self.assign("approver")
        self.assertEqual(self.request("POST", "/api/v1/data-governance/jobs/42/status").status_code, 200)
        self.assertEqual(self.request("POST", "/api/v1/data-governance/jobs/42/execute").status_code, 403)

    def test_disabling_assigned_role_removes_its_permissions(self):
        self.assign("viewer")
        self.db.query(AccessRole).filter_by(id=self.roles["viewer"].id).update({"status": "disabled"})
        self.db.commit()
        self.assertEqual(is_action_allowed(self.db, 7, "operations.read"), (False, True))
        self.assertEqual(self.request("GET", "/api/v1/oracle/42/sessions").status_code, 403)

    def test_local_admin_retains_management_access(self):
        self.info["role"] = "admin"
        self.assertEqual(self.request("GET", "/api/v1/users").status_code, 200)

    def test_relisting_builtin_roles_preserves_removed_permissions(self):
        self.assign("dba_operator")
        self.db.query(RoleActionPermission).filter_by(
            role_id=self.roles["dba_operator"].id, action_key="operations.*",
        ).delete()
        self.db.commit()
        ensure_builtin_roles(self.db)
        self.assertEqual(is_action_allowed(self.db, 7, "operations.execute"), (False, True))

    def test_instance_editor_cannot_rotate_credentials_or_retarget_saved_credentials(self):
        role = AccessRole(role_key="instance_editor", name="Editor", status="active")
        self.db.add(role)
        self.db.flush()
        self.db.add(RoleActionPermission(role_id=role.id, action_key="instance.manage"))
        self.db.add(UserAccessRole(user_id=7, role_id=role.id))
        self.db.commit()
        self.assertEqual(self.request("PUT", "/api/v1/instances/42", json={"remark": "changed"}).status_code, 200)
        self.executed.clear()
        for field in ("password", "password_secret_ref", "ssh_password", "host", "username", "certificate_profile_id"):
            with self.subTest(field=field):
                result = self.request("PUT", "/api/v1/instances/42", json={field: "new-value"})
                self.assertEqual(result.status_code, 403)
                self.assertEqual(result.json()["required_action"], "instance.credential")
        self.assertEqual(self.executed, [])
        self.db.add(RoleActionPermission(role_id=role.id, action_key="instance.credential"))
        self.db.commit()
        self.assertEqual(self.request("PUT", "/api/v1/instances/42", json={"password": "new-value"}).status_code, 200)

    def test_viewer_cannot_consume_recovery_queue_or_download_raw_evidence(self):
        self.assign("viewer")
        for path in (
            "/api/v1/recovery/agent/tasks/pending", "/api/v1/incident-snapshots/42/download",
            "/api/v1/oracle/42/awr/reports/1/download",
        ):
            self.assertEqual(self.request("GET", path).status_code, 403)
        self.assertEqual(self.request("POST", "/api/v1/recovery/tasks").status_code, 403)
        self.assign("dba_operator")
        self.assertEqual(self.request("GET", "/api/v1/recovery/agent/tasks/pending").status_code, 403)
        self.assertEqual(self.request("POST", "/api/v1/recovery/tasks").status_code, 200)

    def test_auditor_and_approver_can_read_their_own_work_queues(self):
        self.assign("auditor")
        self.assertEqual(self.request("GET", "/api/v1/security/audit").status_code, 200)
        self.assertEqual(self.request("GET", "/api/v1/security/audit/verify").status_code, 200)
        self.assertEqual(self.request("GET", "/api/v1/users").status_code, 403)
        self.assign("approver")
        self.assertEqual(self.request("GET", "/api/v1/automation/approvals").status_code, 200)
        self.assertEqual(self.request("GET", "/api/v1/backup-center/operations").status_code, 200)

    def test_every_registered_business_route_has_a_known_permission(self):
        from app.api.v1 import routers

        known = {item["action_key"] for item in action_catalog()}
        missing = []
        for router in routers:
            for route in router.routes:
                if route.path.startswith("/auth/"):
                    continue
                for method in route.methods:
                    action = action_for_request(method, "/api/v1" + route.path)
                    if action not in known:
                        missing.append((method, route.path, action))
        self.assertEqual(missing, [])

    def test_permission_prefixes_match_complete_path_segments(self):
        self.assertIsNone(action_for_request("POST", "/api/v1/users-extra"))
        self.assertIsNone(action_for_request("POST", "/api/v1/ai-not-configured"))
        self.assertEqual(action_for_request("POST", "/api/v1/ai-config/models"), "platform.settings")
        self.assertEqual(action_for_request("POST", "/api/v1/backup-center/operations/1/approve"), "automation.approve")


class ModuleCatalogAccessTest(unittest.TestCase):
    """`GET /api/v1/modules` 必须对任何已认证用户可读，否则权限会 fail-open。

    背景：该路径原本归到 `platform.settings`。受限用户拿不到 → 前端菜单清单请求 403 →
    `AppRootState` 的 catch 分支把 disabledModules 置空 → **显示全部菜单**。
    也就是说"按模块授权"在菜单层根本不生效。修法只放开 GET，写操作仍要求 platform.settings。
    """

    PATHS = (
        "/api/v1/modules",
        "/api/v1/modules/system.params",
        "/api/v1/reports/catalog",
        "/api/v1/settings/snapshot",
    )

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        from app.models.access_control import UserAssetScope
        for table in (AccessRole.__table__, RoleActionPermission.__table__,
                      UserAccessRole.__table__, UserAssetScope.__table__):
            table.create(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        ensure_builtin_roles(self.db)
        self.info = {"source": "db", "user_id": 7, "role": "user", "username": "no-role-user"}
        self.executed = []

        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        async def endpoint(request: Request):
            self.executed.append((request.method, request.url.path))
            return {"ok": True}

        for path in self.PATHS:
            app.add_api_route(path, endpoint, methods=["GET", "PUT", "POST"])
        self.client = TestClient(app)
        self.auth_patch = patch("app.main.get_token_info", side_effect=lambda token: dict(self.info))
        self.session_patch = patch("app.core.database.SessionLocal", self.Session)
        self.auth_patch.start()
        self.session_patch.start()

    def tearDown(self):
        self.client.close()
        self.auth_patch.stop()
        self.session_patch.stop()
        self.db.close()
        self.engine.dispose()

    def request(self, method, path):
        return self.client.request(method, path, headers={"Authorization": "Bearer test-session"})

    def test_module_catalog_is_readable_without_any_business_role(self):
        """没有任何角色的普通用户也要能读自己的模块清单。"""
        self.assertEqual(self.request("GET", "/api/v1/modules").status_code, 200)
        self.assertIn(("GET", "/api/v1/modules"), self.executed)

    def test_writing_module_catalog_still_requires_platform_settings(self):
        """只放开读；改模块开关仍需 platform.settings。"""
        self.assertEqual(self.request("PUT", "/api/v1/modules").status_code, 403)
        self.assertEqual(self.request("PUT", "/api/v1/modules/system.params").status_code, 403)
        self.assertNotIn(("PUT", "/api/v1/modules"), self.executed)

    def test_other_settings_paths_still_fail_closed(self):
        """放开范围必须精确：设置域的其他路径不能被顺带放开。"""
        for path in ("/api/v1/settings/snapshot", "/api/v1/reports/catalog"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path).status_code, 403)
        self.assertEqual(self.executed, [])


if __name__ == "__main__":
    unittest.main()
