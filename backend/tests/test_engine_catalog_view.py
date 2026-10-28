from contextlib import contextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import engine_catalog
from app.engine_registry import Catalog
from app.services.engine_catalog_publisher import CatalogPublisher


class Store:
    def __init__(self):
        self.active = True
        self.revoked = False
        self.generation = 1
        self.engine = "mysql"
        self.digest = "sha256:package"
        self.state = "active"

    def _connect(self):
        return self

    def close(self):
        pass

    def execute(self, sql, params=()):
        return self

    def row(self):
        return {"engine_id": self.engine, "digest": self.digest, "generation": self.generation,
                "state": self.state, "revoked": self.revoked, "quarantined": False}

    def fetchall(self):
        return [self.row()] if self.active else []

    def fetchone(self):
        return self.row() if self.active else None


class Repository:
    def __init__(self):
        self.trusted = True

    @contextmanager
    def guard_installed(self, digest):
        if not self.trusted:
            raise ValueError("revoked")
        yield {"manifest": {"engine_id": "mysql", "publisher": "tracemind", "version": "1.0",
                            "capabilities": ["sessions"], "runtime_digest": "sha256:runtime", "sdk_version": "1.0"}}


def manager():
    host = SimpleNamespace(identity={"engine_id": "mysql", "package_digest": "sha256:package",
                                     "runtime_digest": "sha256:runtime", "sdk_version": "1.0", "generation": 1},
                           state=SimpleNamespace(value="active"))
    return SimpleNamespace(store=Store(), repo=Repository(), _hosts={"mysql": host}, _retained_hosts={})


def test_publisher_revalidates_trust_route_and_host_and_only_changes_generation_on_content():
    m = manager()
    publisher = CatalogPublisher(m)
    ready = publisher.refresh()
    assert ready.get("mysql").available
    assert ready.get("mysql").capabilities == ("operations.session.read",)
    assert publisher.refresh() is ready
    m._hosts["mysql"].state.value = "stopped"
    stopped = publisher.refresh()
    assert stopped.generation == ready.generation + 1
    assert stopped.get("mysql").reason == "requires_runtime_validation"
    m._hosts["mysql"].state.value = "active"
    m.repo.trusted = False
    untrusted = publisher.refresh()
    assert not untrusted.get("mysql").available
    assert untrusted.get("mysql").capabilities == ()
    m.repo.trusted = True
    m.store.generation = 2
    assert not publisher.refresh().get("mysql").available
    m.store.generation = 1
    m.store.state = "disabled"
    assert publisher.refresh().get("mysql").reason == "disabled"


def test_authenticated_catalog_filters_capabilities_and_has_no_management_api(monkeypatch):
    m = manager()
    app = FastAPI()
    app.include_router(engine_catalog.router)
    app.state.engine_catalog_publisher = CatalogPublisher(m)
    app.dependency_overrides[engine_catalog._actor] = lambda: {"user_id": 1}
    app.dependency_overrides[engine_catalog.get_db] = lambda: None
    monkeypatch.setattr(engine_catalog.capability_registry, "list_capabilities", lambda *a, **k: {"items": [
        {"capability_id": "operations.session.read", "authorized_engines": ["mysql"],
         "availability": "available", "plan_executable": True}]})
    client = TestClient(app)
    response = client.get("/engine-catalog")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    result = response.json()
    mysql = next(e for e in result["engines"] if e["engine_id"] == "mysql")
    assert mysql["available"] and mysql["capabilities"] == ["operations.session.read"]
    assert mysql["connection_schema"] is None
    monkeypatch.setattr(engine_catalog.capability_registry, "list_capabilities", lambda *a, **k: {"items": []})
    assert next(e for e in client.get("/engine-catalog").json()["engines"] if e["engine_id"] == "mysql")["capabilities"] == []
    assert client.post("/engine-catalog", json={"verified": True}).status_code == 405
    assert client.get("/engine-catalog?verified=true").status_code == 400


def test_real_signed_fixture_repository_never_appears_as_customer_ready(tmp_path, monkeypatch):
    from test_engine_repository_trust_integration import ready_repository
    repo, fixture, trust = ready_repository(tmp_path, monkeypatch)
    repo.install("provider.tmengine", "catalog-test")
    m = manager()
    m.repo = repo
    m.store.engine = "dm"
    m.store.digest = fixture.package_digest
    publisher = CatalogPublisher(m)
    fixture_entry = publisher.refresh().get("dm")
    assert not fixture_entry.available and fixture_entry.fixture_only and fixture_entry.reason == "fixture_only"
    trust.update(fixture.bundle(version=2, revoked_digests=[fixture.package_digest]))
    assert not publisher.refresh().get("dm").available


def test_http_reads_real_supervisor_signed_repository_and_sqlite_from_request_thread(tmp_path, monkeypatch):
    import base64
    import shutil
    from datetime import datetime, timedelta, timezone
    import test_engine_trust_metadata as vectors
    from test_engine_provider_rpc import _setup
    from app.runtime.engine_host.provider_packages import build_extension_package, ENTRYPOINT
    from app.runtime.engine_host.store import FixtureStore, Package
    from app.runtime.engine_packs.archive import build_archive
    from app.runtime.engine_packs.repository import EngineRepository
    from app.runtime.engine_packs.trust import TrustedEngineKey
    from app.runtime.engine_packs.trust_metadata import TrustStore

    monkeypatch.setattr(vectors, "NOW", datetime.now(timezone.utc) - timedelta(seconds=5))
    root, _, broker, host = _setup()
    fixture = vectors.Fixture()
    key = TrustedEngineKey.create(base64.b64decode(fixture.release[2]), namespace="tmengine.test",
        channel="test", publisher="acme", engine_scopes=("dm",), fixture_only=True)
    adapter = b'''ENGINE_ID = "dm"\nDRIVER = "demo_driver"\nCAPABILITIES = ("sessions",)\nPROVIDER_VERSION = "0.3.0"\nMARKER = "catalog-fixture"\nclass Provider:\n    def invoke(self, operation, config, params=None, *, expected_identity=None):\n        return {}\n'''
    package = build_extension_package(root, engine_id="dm", adapter_source=adapter,
        driver="demo_driver", operations=("sessions",), private_key=fixture.release[0],
        trusted_key=key, synthetic_driver_source=root / "pymysql.py")
    fixture.manifest, fixture.package_digest = package.manifest, package.digest
    trust = TrustStore(tmp_path / "trust", "tmengine.test")
    trust.bootstrap(fixture.root, expected_root_sha256=vectors.digest(fixture.root), fixture_only=True)
    trust.update(fixture.bundle())
    archive = tmp_path / "provider.tmengine"
    build_archive(package.source, package.manifest, package.signature, archive, trusted_keys=(key,),
                  expected_namespace="tmengine.test", expected_channel="test")
    repo = EngineRepository(tmp_path / "repo", namespace="tmengine.test", channel="test",
                            trust_guard=trust, entrypoint_ids=(ENTRYPOINT,))
    shutil.copyfile(archive, repo.root / "incoming" / "provider.tmengine")
    repo.install("provider.tmengine", "catalog-live-host")
    store = FixtureStore(tmp_path / "store.sqlite")
    store.bootstrap_active(Package(engine_id="dm", digest=package.digest, version="0.3.0"), generation=1)
    try:
        host.start(package.source, package.manifest, package.signature, package.trusted_keys,
                   expected_namespace="tmengine.test", expected_channel="test", generation=1)
        mgr = SimpleNamespace(store=store, repo=repo, _hosts={"dm": host}, _retained_hosts={})
        publisher = CatalogPublisher(mgr)
        app = FastAPI(); app.include_router(engine_catalog.router)
        app.state.engine_catalog_publisher = publisher
        app.dependency_overrides[engine_catalog._actor] = lambda: {"user_id": 1}
        app.dependency_overrides[engine_catalog.get_db] = lambda: None
        monkeypatch.setattr(engine_catalog.capability_registry, "list_capabilities", lambda *a, **k: {"items": []})
        client = TestClient(app)
        response = client.get("/engine-catalog")
        assert response.status_code == 200
        dm = next(e for e in response.json()["engines"] if e["engine_id"] == "dm")
        assert dm["fixture_only"] and not dm["available"] and dm["reason"] == "fixture_only"
        assert dm["capabilities"] == []
        store.set_package_state("dm", package.digest, "disabled")
        assert client.get("/engine-catalog").status_code == 200
        trust.update(fixture.bundle(version=2, revoked_digests=[package.digest]))
        dm = next(e for e in client.get("/engine-catalog").json()["engines"] if e["engine_id"] == "dm")
        assert not dm["available"]
    finally:
        host.stop(); broker.close(); store.close()
