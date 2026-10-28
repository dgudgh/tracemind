import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cmdb import discovery, normalized_service, service
from app.cmdb.api import asset_router
from app.cmdb.models import (
    AssetCluster,
    AssetClusterHealthSnapshot,
    AssetCorrectionAudit,
    AssetDiscoveryBatch,
    AssetEnvironment,
    AssetHost,
    AssetInstance,
    AssetRegion,
    AssetRelation,
    AssetSite,
    AssetZone,
    CmdbAsset,
    CmdbAssetOverride,
    CmdbBinding,
    CmdbChangeEvent,
    CmdbRelation,
    CmdbSource,
    CmdbSyncJob,
)
from app.core.database import get_db
from app.models.db_instance import DBInstance, DBInstanceCredentialAudit
from app.models.system_setting import DBSystemSetting
from app.services import instances as instance_service


MODELS = (
    DBInstance,
    DBInstanceCredentialAudit,
    DBSystemSetting,
    CmdbSource,
    CmdbAsset,
    CmdbRelation,
    CmdbSyncJob,
    CmdbBinding,
    CmdbAssetOverride,
    CmdbChangeEvent,
    AssetRegion,
    AssetSite,
    AssetZone,
    AssetEnvironment,
    AssetHost,
    AssetCluster,
    AssetInstance,
    AssetRelation,
    AssetDiscoveryBatch,
    AssetCorrectionAudit,
    AssetClusterHealthSnapshot,
)


def add_mysql_pair(db):
    db.add_all([
        DBInstance(
            instance_name="mysql-primary",
            db_type="mysql",
            db_version="8.4.1",
            host="198.51.100.152",
            port=3384,
            username="root",
            password_cipher="database-secret",
            env_type="测试",
            system_name="mysql-test-8.4",
            architecture_type="replication",
            role_type="primary",
            ssh_host="198.51.100.152",
            ssh_port=22,
            ssh_username="ops",
            ssh_password_cipher="ssh-secret",
            enabled=1,
        ),
        DBInstance(
            instance_name="mysql-replica",
            db_type="mysql",
            db_version="8.4.1",
            host="198.51.100.153",
            port=3384,
            username="root",
            password_cipher="another-secret",
            env_type="测试",
            system_name="mysql-test-8.4",
            architecture_type="replication",
            role_type="replica",
            ssh_host="198.51.100.153",
            ssh_port=22,
            ssh_username="ops",
            ssh_password_cipher="another-ssh-secret",
            enabled=1,
        ),
    ])
    db.commit()


class AssetCenterServiceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in MODELS:
            model.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        add_mysql_pair(self.db)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_internal_discovery_creates_assets_relations_and_bindings_idempotently(self):
        first = service.apply_internal_discovery(self.db)
        second = service.apply_internal_discovery(self.db)

        self.assertEqual(first["applied_created_count"], 5)
        self.assertEqual(second["applied_created_count"], 0)
        self.assertEqual(self.db.query(CmdbAsset).count(), 5)
        self.assertEqual(self.db.query(CmdbBinding).count(), 2)
        self.assertEqual(
            self.db.query(CmdbRelation).filter(CmdbRelation.status == "active").count(),
            4,
        )

    def test_internal_discovery_never_persists_database_or_ssh_credentials(self):
        service.apply_internal_discovery(self.db)

        serialized = str([
            {"raw_payload": row.raw_payload, "attributes": row.attributes}
            for row in self.db.query(CmdbAsset).all()
        ])
        self.assertNotIn("database-secret", serialized)
        self.assertNotIn("another-secret", serialized)
        self.assertNotIn("ssh-secret", serialized)
        self.assertNotIn("another-ssh-secret", serialized)

    def test_legacy_cidr_peer_uses_existing_hostname_during_projection(self):
        peer_uuid = "e7651dd6-ddc4-44db-9925-1fe756c295f0"
        normalized_service.sync_from_inventory(
            self.db,
            [
                {
                    "id": 101,
                    "asset_type": "host",
                    "name": "198.51.100.153",
                    "status": "active",
                    "source_kind": "automatic",
                    "attributes": {
                        "host": "198.51.100.153",
                        "hostname": "orch-mysql02",
                    },
                },
                {
                    "id": 102,
                    "asset_type": "host",
                    "name": "198.51.100.153/32",
                    "status": "active",
                    "source_kind": "automatic",
                    "attributes": {
                        "host": "198.51.100.153/32",
                        "hostname": peer_uuid,
                    },
                },
                {
                    "id": 103,
                    "asset_type": "database_instance",
                    "name": peer_uuid,
                    "status": "active",
                    "health_status": "healthy",
                    "source_kind": "automatic",
                    "attributes": {
                        "db_type": "postgresql",
                        "host": "198.51.100.153/32",
                        "port": 5432,
                        "architecture_type": "streaming_replication",
                        "role_type": "replica",
                        "native_type": "discovered_database_instance",
                    },
                },
            ],
            source_id=1,
            source_name="旧版自动发现数据",
            source_type="internal",
        )

        instance = normalized_service.list_instances(self.db)[0]
        host = normalized_service.list_hosts(self.db)[0]
        self.assertEqual(instance["name"], "orch-mysql02")
        self.assertEqual(instance["ip"], "198.51.100.153")
        self.assertEqual(instance["host_name"], "orch-mysql02")
        self.assertEqual(host["name"], "orch-mysql02")
        self.assertEqual(host["ip"], "198.51.100.153")

    def test_endpoint_default_instance_name_uses_discovered_hostname(self):
        normalized_service.sync_from_inventory(
            self.db,
            [
                {
                    "id": 201,
                    "asset_type": "host",
                    "name": "198.51.100.152",
                    "status": "active",
                    "source_kind": "automatic",
                    "attributes": {
                        "host": "198.51.100.152",
                        "hostname": "orch-mysql01",
                    },
                },
                {
                    "id": 202,
                    "asset_type": "database_instance",
                    "name": "198.51.100.152",
                    "status": "active",
                    "health_status": "healthy",
                    "source_kind": "automatic",
                    "attributes": {
                        "db_type": "postgresql",
                        "host": "198.51.100.152",
                        "port": 5432,
                        "architecture_type": "streaming_replication",
                        "role_type": "primary",
                        "native_type": "database_instance",
                        "native_instance_id": 77,
                    },
                },
            ],
            source_id=1,
            source_name="自动发现数据",
            source_type="internal",
        )

        instance = normalized_service.list_instances(self.db)[0]
        self.assertEqual(instance["name"], "orch-mysql01")
        self.assertFalse(instance["name_customized"])

    def test_delete_connection_hides_normalized_asset_without_losing_audit(self):
        service.apply_internal_discovery(self.db)
        native = self.db.query(DBInstance).filter(DBInstance.instance_name == "mysql-primary").one()
        normalized = self.db.query(AssetInstance).filter(
            AssetInstance.native_instance_id == native.id,
        ).one()
        native_id = native.id
        normalized_id = normalized.id

        instance_service.delete(self.db, native, operator="tester")

        self.assertIsNone(self.db.get(DBInstance, native_id))
        self.assertEqual(self.db.get(AssetInstance, normalized_id).status, "inactive")
        self.assertNotIn(normalized_id, {item["id"] for item in normalized_service.list_instances(self.db)})
        source_asset = self.db.query(CmdbAsset).filter(
            CmdbAsset.external_id == f"db-instance:{native_id}",
        ).one()
        self.assertEqual(source_asset.status, "inactive")
        audit = self.db.query(AssetCorrectionAudit).filter(
            AssetCorrectionAudit.entity_id == normalized_id,
            AssetCorrectionAudit.action == "delete_connection",
        ).one()
        self.assertEqual(audit.operator, "tester")

        service.apply_internal_discovery(self.db)
        self.assertEqual(self.db.get(AssetInstance, normalized_id).status, "inactive")
        self.assertEqual(source_asset.status, "inactive")

    def test_topology_groups_roles_do_not_fabricate_directional_edge(self):
        service.apply_internal_discovery(self.db)

        topology = service.topology_groups(self.db)

        self.assertEqual(topology["group_count"], 1)
        group = topology["groups"][0]
        self.assertEqual(group["architecture_type"], "replication")
        self.assertEqual([node["role"] for node in group["nodes"]], ["primary", "replica"])
        self.assertEqual(group["edges"], [])
        self.assertEqual(group["replication_state"], "unknown")

    def test_manual_override_survives_automatic_rediscovery_and_is_audited(self):
        service.apply_internal_discovery(self.db)
        asset = self.db.query(CmdbAsset).filter(CmdbAsset.external_id == "db-instance:2").one()

        service.upsert_asset_overrides(
            self.db,
            asset.id,
            {
                "attributes.role_type": "standby",
                "attributes.vip": "198.51.100.160",
            },
            reason="人工核对复制架构",
            locked=True,
            operator="tester",
        )
        service.apply_internal_discovery(self.db)

        detail = service.get_asset_detail(self.db, asset.id)
        self.assertEqual(detail["attributes"]["role_type"], "standby")
        self.assertEqual(detail["attributes"]["vip"], "198.51.100.160")
        self.assertIn("attributes.role_type", detail["locked_fields"])
        self.assertEqual(self.db.query(CmdbChangeEvent).count(), 2)

    def test_overview_reports_internal_asset_inventory(self):
        service.apply_internal_discovery(self.db)

        overview = service.asset_overview(self.db)

        self.assertEqual(overview["total_assets"], 5)
        self.assertEqual(overview["database_instances"], 2)
        self.assertEqual(overview["hosts"], 2)
        self.assertEqual(overview["clusters"], 1)
        self.assertEqual(overview["automatic_assets"], 5)

    def test_five_layer_projection_separates_hosts_clusters_and_instances(self):
        self.db.add_all([
            DBInstance(
                instance_name="pg-on-primary-host",
                db_type="postgresql",
                db_version="14.3",
                host="198.51.100.152",
                port=5432,
                username="postgres",
                password_cipher="secret",
                env_type="生产",
                architecture_type="standalone",
                role_type="primary",
                enabled=1,
            ),
            DBInstance(
                instance_name="oracle-single",
                db_type="oracle",
                db_version="19c",
                host="198.51.100.200",
                port=1521,
                username="sys",
                password_cipher="secret",
                env_type="生产",
                architecture_type="standalone",
                role_type="primary",
                enabled=1,
            ),
        ])
        self.db.commit()

        service.apply_internal_discovery(self.db)
        hosts = normalized_service.list_hosts(self.db)
        clusters = normalized_service.list_clusters(self.db)
        instances = normalized_service.list_instances(self.db)
        tree = normalized_service.asset_tree(self.db)

        shared_host = next(item for item in hosts if item["ip"] == "198.51.100.152")
        shared_instances = [item for item in instances if item["host_id"] == shared_host["id"]]
        self.assertEqual({item["db_type"] for item in shared_instances}, {"MySQL", "PostgreSQL"})
        self.assertEqual(shared_host["status"], "healthy")
        self.assertEqual(len(hosts), 3)
        self.assertEqual(len(clusters), 3)
        self.assertEqual(len(instances), 4)
        mysql_cluster = next(item for item in clusters if item["db_type"] == "MySQL")
        self.assertEqual(mysql_cluster["architecture"], "primary_replica")
        self.assertEqual(mysql_cluster["instance_count"], 2)
        self.assertEqual(tree["regions"][0]["name"], "未配置")
        self.assertTrue(all(item["host_id"] and item["cluster_id"] for item in instances))

    def test_reprojection_retires_and_hides_empty_automatic_cluster(self):
        base_asset = {
            "id": 301,
            "asset_type": "database_instance",
            "name": "pg-primary",
            "status": "active",
            "health_status": "healthy",
            "source_kind": "automatic",
            "attributes": {
                "native_instance_id": 301,
                "native_type": "database_instance",
                "db_type": "postgresql",
                "host": "198.51.100.152",
                "port": 5432,
                "role_type": "primary",
            },
        }
        normalized_service.sync_from_inventory(
            self.db,
            [{**base_asset, "attributes": {**base_asset["attributes"], "architecture_type": "standalone"}}],
            source_id=None,
            source_name="自动发现数据",
            source_type="internal",
        )
        instance = self.db.query(AssetInstance).filter(AssetInstance.native_instance_id == 301).one()
        old_cluster_id = instance.cluster_id

        normalized_service.sync_from_inventory(
            self.db,
            [{
                **base_asset,
                "attributes": {
                    **base_asset["attributes"],
                    "architecture_type": "streaming_replication",
                    "topology_group": "postgresql:streaming:198.51.100.152:5432",
                    "cluster_name": "postgres 流复制",
                },
            }],
            source_id=None,
            source_name="自动发现数据",
            source_type="internal",
        )

        self.db.refresh(instance)
        self.assertNotEqual(instance.cluster_id, old_cluster_id)
        self.assertEqual(self.db.get(AssetCluster, old_cluster_id).status, "inactive")
        self.assertNotIn(old_cluster_id, {item["id"] for item in normalized_service.list_clusters(self.db)})

        # Existing installations can already contain a pending empty cluster.
        old_cluster = self.db.get(AssetCluster, old_cluster_id)
        old_cluster.status = "pending_confirmation"
        self.db.commit()
        self.assertNotIn(old_cluster_id, {item["id"] for item in normalized_service.list_clusters(self.db)})
        self.assertIn(old_cluster_id, {item["id"] for item in normalized_service.list_clusters(self.db, include_inactive=True)})

    def test_postgresql_discovery_uses_shared_hostname_instead_of_uuid(self):
        pg = DBInstance(
            instance_name="pg-primary",
            db_type="postgresql",
            host="198.51.100.152",
            port=5432,
            username="postgres",
            password_cipher="secret",
            database_name="postgres",
            enabled=1,
        )
        self.db.add(pg)
        self.db.commit()
        mysql_replica = self.db.query(DBInstance).filter(DBInstance.host == "198.51.100.153").one()
        generated_application_name = "e7651dd6-ddc4-44db-9925-1fe756c295f0"

        bundle = discovery.build_internal_discovery(self.db, detected_profiles={
            mysql_replica.id: {
                "architecture_type": "replication",
                "role_type": "replica",
                "hostname": "orch-mysql02",
            },
            pg.id: {
                "architecture_type": "streaming_replication",
                "role_type": "primary",
                "topology_group": "postgresql:streaming:test",
                "cluster_name": "postgres 流复制",
                "members": [{
                    "member_key": "198.51.100.153/32:5432",
                    "name": generated_application_name,
                    "host": "198.51.100.153/32",
                    "reported_host": generated_application_name,
                    "port": 5432,
                    "role_type": "replica",
                    "health_status": "healthy",
                }],
            },
        })

        peer = next(
            item for item in bundle.assets
            if item["asset_type"] == "database_instance"
            and item["attributes"].get("native_type") == "discovered_database_instance"
            and item["attributes"].get("db_type") == "postgresql"
        )
        matching_hosts = [
            item for item in bundle.assets
            if item["asset_type"] == "host" and item["attributes"].get("host") == "198.51.100.153"
        ]

        self.assertEqual(peer["name"], "orch-mysql02")
        self.assertEqual(peer["attributes"]["host"], "198.51.100.153")
        self.assertEqual(peer["attributes"]["reported_host"], "orch-mysql02")
        self.assertEqual(len(matching_hosts), 1)
        self.assertEqual(matching_hosts[0]["attributes"]["hostname"], "orch-mysql02")

    def test_manual_role_and_cluster_split_survive_rediscovery(self):
        service.apply_internal_discovery(self.db)
        replica = next(item for item in normalized_service.list_instances(self.db) if item["role"] == "replica")
        original_cluster_id = replica["cluster_id"]

        normalized_service.apply_correction(self.db, {
            "action": "update",
            "entity_type": "instance",
            "entity_id": replica["id"],
            "changes": {"role": "read_only"},
            "reason": "人工确认只读角色",
        }, "tester")
        normalized_service.apply_correction(self.db, {
            "action": "split_cluster",
            "entity_type": "cluster",
            "entity_id": original_cluster_id,
            "entity_ids": [replica["id"]],
            "changes": {"name": "mysql-read-cluster", "architecture": "primary_replica"},
            "reason": "复制组识别错误",
        }, "tester")
        service.apply_internal_discovery(self.db)

        corrected = normalized_service.get_instance(self.db, replica["id"])
        self.assertEqual(corrected["role"], "read_only")
        self.assertNotEqual(corrected["cluster_id"], original_cluster_id)
        self.assertEqual(self.db.query(AssetCorrectionAudit).count(), 2)

    def test_cluster_merge_assignment_survives_rediscovery(self):
        service.apply_internal_discovery(self.db)
        target = self.db.query(AssetCluster).one()
        replica = self.db.query(AssetInstance).filter(AssetInstance.role == "replica").one()
        duplicate = AssetCluster(
            stable_key="manual-cluster:duplicate",
            name="重复识别集群",
            db_type=target.db_type,
            cluster_type=target.cluster_type,
            architecture=target.architecture,
            region_id=target.region_id,
            site_id=target.site_id,
            zone_id=target.zone_id,
            env_id=target.env_id,
            status="healthy",
            discovery_source="manual",
        )
        self.db.add(duplicate)
        self.db.flush()
        replica.cluster_id = duplicate.id
        self.db.commit()

        normalized_service.apply_correction(self.db, {
            "action": "merge",
            "entity_type": "cluster",
            "entity_id": target.id,
            "entity_ids": [duplicate.id],
            "reason": "合并重复集群",
        }, "tester")
        service.apply_internal_discovery(self.db)

        self.assertEqual(self.db.get(AssetInstance, replica.id).cluster_id, target.id)
        self.assertIn("cluster_id", self.db.get(AssetInstance, replica.id).manual_fields)
        self.assertEqual(self.db.get(AssetCluster, duplicate.id).status, "merged")

    def test_projection_deduplicates_same_instance_and_replication_relation_in_one_batch(self):
        now = self.db.query(DBInstance).first().create_time
        primary = {
            "id": 101,
            "asset_type": "database_instance",
            "name": "mysql-primary",
            "status": "active",
            "health_status": "healthy",
            "source_kind": "automatic",
            "last_seen_at": now,
            "attributes": {
                "db_type": "mysql",
                "host": "10.0.0.1",
                "port": 3306,
                "architecture_type": "replication",
                "role_type": "primary",
                "topology_group": "mysql:replication:test",
            },
        }
        replica = {
            **primary,
            "id": 102,
            "name": "mysql-replica",
            "attributes": {
                **primary["attributes"],
                "host": "10.0.0.2",
                "role_type": "replica",
                "replication": {"state": "verified", "upstream": {"host": "10.0.0.1", "port": 3306}},
            },
        }

        normalized_service.sync_from_inventory(
            self.db,
            [primary, replica, dict(replica)],
            source_id=None,
            source_name="test",
            source_type="internal",
        )

        self.assertEqual(self.db.query(AssetInstance).count(), 2)
        self.assertEqual(
            self.db.query(AssetRelation).filter(AssetRelation.relation_type == "replicates_to").count(),
            1,
        )

    def test_cluster_list_preserves_saved_cascade_and_filters_relation_scope(self):
        inventory = [{
            "id": 900 + index, "asset_type": "database_instance", "name": f"node-{index}",
            "status": "active", "attributes": {
                "db_type": "mysql", "host": f"198.51.100.{index}", "port": 3306,
                "architecture_type": "replication", "role_type": "primary" if index == 1 else "replica",
                "topology_group": "cascade" if index < 4 else "other-cluster",
            },
        } for index in range(1, 5)]
        normalized_service.sync_from_inventory(self.db, inventory, None, "test", "internal")
        nodes = {item.name: item for item in self.db.query(AssetInstance).all()}
        a, b, c, external = [nodes[f"node-{index}"] for index in range(1, 5)]
        self.db.query(AssetRelation).delete(synchronize_session=False)
        specs = [
            ("a-b", "instance", a.id, "instance", b.id, "active"),
            ("b-c", "instance", b.id, "instance", c.id, "active"),
            ("inactive", "instance", a.id, "instance", c.id, "inactive"),
            ("external-target", "instance", a.id, "instance", external.id, "active"),
            ("external-source", "instance", external.id, "instance", a.id, "active"),
            ("missing", "instance", a.id, "instance", 99999, "active"),
            ("wrong-source-type", "host", a.id, "instance", c.id, "active"),
            ("wrong-target-type", "instance", a.id, "host", c.id, "active"),
            ("self", "instance", a.id, "instance", a.id, "active"),
        ]
        for key, source_type, source_id, target_type, target_id, status in specs:
            self.db.add(AssetRelation(
                relation_key=key, source_type=source_type, source_id=source_id,
                target_type=target_type, target_id=target_id, relation_type="replicates_to", status=status,
            ))
        self.db.commit()

        def edges(allowed=None):
            clusters = normalized_service.list_clusters(self.db, allowed_instance_ids=allowed)
            cluster = next(item for item in clusters if item["id"] == a.cluster_id)
            return [(item["source_id"], item["target_id"]) for item in cluster["relations"]]

        self.assertEqual(edges(), [(a.id, b.id), (b.id, c.id)])
        self.assertEqual(edges({a.id, b.id}), [(a.id, b.id)])
        self.assertEqual(edges({b.id, c.id}), [(b.id, c.id)])
        self.assertEqual(edges({a.id, c.id}), [])
        self.assertEqual(normalized_service.list_clusters(self.db, allowed_instance_ids=set()), [])
        c.status = "inactive"
        self.db.commit()
        self.assertEqual(edges(), [(a.id, b.id)])
        self.db.query(AssetRelation).delete(synchronize_session=False)
        self.db.commit()
        self.assertEqual(edges(), [])

    def test_cluster_list_exposes_rac_membership_without_replication_edges(self):
        service.apply_internal_discovery(self.db)
        cluster = self.db.query(AssetCluster).one()
        cluster.architecture = "rac"
        nodes = self.db.query(AssetInstance).all()
        for node in nodes:
            node.role = "rac_node"
        self.db.query(AssetRelation).filter(AssetRelation.relation_type == "replicates_to").delete(synchronize_session=False)
        self.db.add(AssetRelation(
            relation_key="wrong-cluster", source_type="instance", source_id=nodes[0].id,
            target_type="cluster", target_id=99999, relation_type="member_of", status="active",
        ))
        self.db.commit()
        relations = normalized_service.list_clusters(self.db)[0]["relations"]
        self.assertEqual(len(relations), 2)
        self.assertEqual({item["relation_type"] for item in relations}, {"member_of"})
        self.assertEqual({item["target_id"] for item in relations}, {cluster.id})
        visible = normalized_service.list_clusters(self.db, allowed_instance_ids={nodes[0].id})[0]["relations"]
        self.assertEqual([item["source_id"] for item in visible], [nodes[0].id])

    @patch("app.cmdb.service.host_overview.get_instance_host_overview")
    @patch("app.cmdb.service.instance_service.test_connection")
    def test_live_probe_updates_architecture_metadata_and_evidence(self, test_connection, get_host_overview):
        test_connection.side_effect = [
            {"architecture": {"architecture_type": "replication", "role_type": "primary", "server_id": "1"}},
            {"architecture": {"architecture_type": "replication", "role_type": "replica", "server_id": "2"}},
        ]
        get_host_overview.return_value = {
            "host_basic": {
                "hostname": "db-linux",
                "os_name": "Linux",
                "os_version": "9.4",
                "cpu_count": 8,
                "cpu_model": "Test CPU",
                "memory_total_mb": 16384,
            },
        }

        result = service.apply_internal_discovery(self.db, probe_live=True)

        self.assertEqual(result["probed_count"], 2)
        self.assertEqual(result["detected_architecture_count"], 2)
        self.assertEqual(result["host_profile_count"], 2)
        assets = service.list_inventory(self.db, asset_type="database_instance")
        self.assertTrue(all(item["attributes"]["discovery_method"] == "live_probe" for item in assets))
        self.assertEqual({item["attributes"]["architecture_evidence"]["server_id"] for item in assets}, {"1", "2"})
        self.assertTrue(all(item["os_type"] == "Linux" for item in normalized_service.list_hosts(self.db)))

    @patch("app.cmdb.service.host_overview.get_instance_host_overview")
    @patch("app.cmdb.service.instance_service.test_connection")
    def test_live_probe_failure_is_pending_instead_of_confirmed_healthy(self, test_connection, get_host_overview):
        test_connection.side_effect = RuntimeError(
            'no pg_hba.conf entry for host "192.168.101.165", user "pgsql"'
        )
        get_host_overview.return_value = {"error": "SSH 未配置", "host_basic": None}

        result = service.apply_internal_discovery(self.db, probe_live=True)

        self.assertEqual(result["detected_architecture_count"], 0)
        self.assertEqual(result["failed_probe_count"], 2)
        self.assertGreaterEqual(result["warning_count"], 2)
        instances = normalized_service.list_instances(self.db)
        self.assertTrue(all(item["status"] == "warning" for item in instances))
        self.assertTrue(all(item["attributes"]["discovery_status"] == "failed" for item in instances))
        self.assertTrue(all(item["attributes"]["architecture_verified"] is False for item in instances))
        batch = self.db.query(AssetDiscoveryBatch).order_by(AssetDiscoveryBatch.id.desc()).first()
        self.assertEqual(batch.status, "warning")
        self.assertEqual(batch.pending_instance_count, 2)

    @patch("app.cmdb.service.host_overview.get_instance_host_overview")
    @patch("app.cmdb.service.instance_service.test_connection")
    def test_failed_reprobe_preserves_last_successful_architecture(self, test_connection, get_host_overview):
        get_host_overview.return_value = {"error": "SSH 未配置", "host_basic": None}
        test_connection.side_effect = [
            {
                "version": "8.4.1",
                "architecture": {
                    "architecture_type": "replication",
                    "role_type": "primary",
                    "topology_group": "mysql:replication:test-cluster",
                    "cluster_name": "test-cluster",
                },
            },
            {
                "version": "8.4.1",
                "architecture": {
                    "architecture_type": "replication",
                    "role_type": "replica",
                    "topology_group": "mysql:replication:test-cluster",
                    "cluster_name": "test-cluster",
                },
            },
        ]
        service.apply_internal_discovery(self.db, probe_live=True)

        test_connection.side_effect = RuntimeError("password authentication failed")
        result = service.apply_internal_discovery(self.db, probe_live=True)

        self.assertEqual(result["failed_probe_count"], 2)
        instances = normalized_service.list_instances(self.db)
        self.assertEqual({item["architecture"] for item in instances}, {"primary_replica"})
        self.assertEqual({item["role"] for item in instances}, {"primary", "replica"})
        self.assertTrue(all(item["attributes"]["discovery_status"] == "failed" for item in instances))
        self.assertTrue(all(item["attributes"]["has_verified_history"] is True for item in instances))
        self.assertTrue(all(item["attributes"]["architecture_source"] == "last_successful_discovery" for item in instances))

    def test_cluster_discovery_status_is_aggregated_across_all_members(self):
        cluster = AssetCluster(
            stable_key="cluster:oracle:data-guard:test",
            name="mesdb",
            db_type="Oracle",
            cluster_type="database_cluster",
            architecture="adg",
            region_id=1,
            site_id=1,
            zone_id=1,
            env_id=1,
            status="healthy",
            discovery_source="automatic",
            attributes={"discovery_status": "failed", "discovery_error": "stale error"},
        )
        self.db.add(cluster)
        self.db.flush()
        primary = AssetInstance(
            stable_key="native-instance:101",
            name="mesdb",
            db_type="Oracle",
            ip="198.51.100.200",
            port=1521,
            role="adg_primary",
            host_id=1,
            cluster_id=cluster.id,
            region_id=1,
            site_id=1,
            zone_id=1,
            env_id=1,
            status="healthy",
            discovery_source="automatic",
            attributes={
                "architecture_type": "data_guard",
                "topology_group": "oracle:data_guard:mesdb:reportdb",
                "cluster_name": "mesdb",
                "discovery_status": "success",
            },
        )
        standby = AssetInstance(
            stable_key="native-instance:102",
            name="reportdb",
            db_type="Oracle",
            ip="198.51.100.211",
            port=1521,
            role="adg_standby",
            host_id=2,
            cluster_id=cluster.id,
            region_id=1,
            site_id=1,
            zone_id=1,
            env_id=1,
            status="healthy",
            discovery_source="automatic",
            attributes={
                "architecture_type": "data_guard",
                "discovery_status": "failed",
                "discovery_error": "ORA-01033",
            },
        )
        self.db.add_all([primary, standby])
        self.db.flush()

        normalized_service._apply_cluster_discovery_summary(cluster, [primary, standby])

        self.assertEqual(cluster.attributes["discovery_status"], "partial")
        self.assertEqual(cluster.attributes["verified_member_count"], 1)
        self.assertEqual(cluster.attributes["failed_member_count"], 1)
        self.assertTrue(cluster.attributes["architecture_verified"])
        self.assertEqual(cluster.attributes["discovery_errors"][0]["name"], "reportdb")
        self.assertEqual(cluster.attributes["topology_group"], "oracle:data_guard:mesdb:reportdb")

    def test_cluster_projection_keeps_only_cluster_level_attributes(self):
        normalized_service.sync_from_inventory(
            self.db,
            [{
                "id": 901,
                "asset_type": "database_instance",
                "name": "mesdb",
                "status": "active",
                "health_status": "healthy",
                "source_kind": "automatic",
                "attributes": {
                    "native_instance_id": 901,
                    "native_type": "db_instance",
                    "db_type": "oracle",
                    "db_version": "19.0.0.0.0",
                    "host": "198.51.100.200",
                    "port": 1521,
                    "service_name": "mesdb",
                    "architecture_type": "data_guard",
                    "role_type": "primary",
                    "topology_group": "oracle:data_guard:mesdb:reportdb",
                    "cluster_name": "mesdb",
                    "discovery_status": "success",
                    "architecture_verified": True,
                    "replication_delay_seconds": 2,
                    "transport_lag_seconds": 1,
                    "apply_lag_seconds": 2,
                    "transport_lag": "+00 00:00:01",
                    "apply_lag": "+00 00:00:02",
                    "lag_time_computed": "07/24/2026 10:36:52",
                },
            }],
            source_id=None,
            source_name="test",
            source_type="internal",
        )

        cluster = self.db.query(AssetCluster).filter(
            AssetCluster.stable_key.like("cluster:oracle:%"),
        ).one()
        self.assertEqual(cluster.attributes["topology_group"], "oracle:data_guard:mesdb:reportdb")
        self.assertEqual(cluster.attributes["discovery_status"], "success")
        instance_payload = normalized_service.list_instances(self.db)[0]
        self.assertEqual(instance_payload["replication_delay_seconds"], 2)
        self.assertEqual(instance_payload["transport_lag_seconds"], 1)
        self.assertEqual(instance_payload["apply_lag_seconds"], 2)
        self.assertEqual(instance_payload["lag_time_computed"], "07/24/2026 10:36:52")
        for member_only_field in (
            "native_instance_id",
            "native_type",
            "db_version",
            "host",
            "port",
            "service_name",
            "role_type",
        ):
            self.assertNotIn(member_only_field, cluster.attributes)

    def test_startup_projection_does_not_create_discovery_batch(self):
        service.apply_internal_discovery(self.db)
        before = self.db.query(AssetDiscoveryBatch).count()

        result = service.ensure_internal_assets(self.db)

        self.assertFalse(result["initialized"])
        self.assertEqual(self.db.query(AssetDiscoveryBatch).count(), before)
        self.assertIsNone(result["normalized"]["batch_id"])

    @patch("app.cmdb.service.instance_service.test_connection")
    def test_configured_oracle_dataguard_peer_is_not_probed_twice(self, test_connection):
        self.db.query(DBInstance).delete()
        self.db.add_all([
            DBInstance(
                instance_name="mesdb",
                db_type="oracle",
                host="198.51.100.200",
                port=1521,
                username="system",
                password_cipher="secret",
                service_name="mesdb",
                enabled=1,
            ),
            DBInstance(
                instance_name="reportdb",
                db_type="oracle",
                host="198.51.100.211",
                port=1521,
                username="system",
                password_cipher="secret",
                service_name="reportdb",
                enabled=1,
            ),
        ])
        self.db.commit()

        def probe(instance, **_kwargs):
            if instance.instance_name == "mesdb":
                return {
                    "version": "19.0.0.0.0",
                    "architecture": {
                        "architecture_type": "data_guard",
                        "role_type": "primary",
                        "topology_group": "oracle:data_guard:mesdb:reportdb",
                        "cluster_name": "mesdb",
                        "member_key": "mesdb",
                        "db_unique_name": "mesdb",
                        "db_version": "19.0.0.0.0",
                        "members": [{
                            "member_key": "reportdb",
                            "name": "reportdb",
                            "host": "",
                            "reported_host": "reportdb",
                            "port": 1521,
                            "role_type": "standby",
                            "health_status": "healthy",
                        }],
                    },
                }
            if instance.instance_name == "reportdb":
                return {
                    "version": "19.0.0.0.0",
                    "architecture": {
                        "architecture_type": "data_guard",
                        "role_type": "standby",
                        "topology_group": "oracle:data_guard:mesdb:reportdb",
                        "cluster_name": "mesdb",
                        "member_key": "reportdb",
                        "db_unique_name": "reportdb",
                        "db_version": "19.0.0.0.0",
                        "members": [{
                            "member_key": "mesdb",
                            "name": "mesdb",
                            "host": "",
                            "reported_host": "mesdb",
                            "port": 1521,
                            "role_type": "primary",
                            "health_status": "healthy",
                        }],
                    },
                }
            raise AssertionError(f"unexpected peer probe: {instance.instance_name}")

        test_connection.side_effect = probe

        result = service.apply_internal_discovery(self.db, probe_live=True)

        self.assertEqual(test_connection.call_count, 2)
        self.assertEqual(result["warning_count"], 0)
        oracle_clusters = [
            item for item in normalized_service.list_clusters(self.db)
            if item["db_type"] == "Oracle"
        ]
        self.assertEqual(len(oracle_clusters), 1)
        self.assertEqual(oracle_clusters[0]["instance_count"], 2)

    @patch("app.cmdb.service.host_overview.get_instance_host_overview")
    @patch("app.cmdb.service.instance_service.test_connection")
    def test_live_probe_adds_unmanaged_replica_assets_to_topology(self, test_connection, get_host_overview):
        self.db.query(DBInstance).filter(DBInstance.instance_name == "mysql-replica").delete()
        self.db.commit()
        primary_profile = {
            "architecture": {
                "architecture_type": "replication",
                "role_type": "primary",
                "topology_group": "mysql:replication:primary-uuid",
                "cluster_name": "mysql-test-8.4",
                "server_uuid": "primary-uuid",
                "member_key": "primary-uuid",
                "members": [
                    {
                        "member_key": "replica-2",
                        "server_uuid": "replica-2",
                        "name": "orch-mysql02",
                        "host": "198.51.100.153",
                        "reported_host": "orch-mysql02",
                        "port": 3384,
                        "role_type": "replica",
                        "health_status": "healthy",
                    },
                    {
                        "member_key": "replica-3",
                        "server_uuid": "replica-3",
                        "name": "orch-mysql03",
                        "host": "198.51.100.154",
                        "reported_host": "orch-mysql03",
                        "port": 3384,
                        "role_type": "replica",
                        "health_status": "healthy",
                    },
                ],
            }
        }
        replica_profile = {
            "version": "8.4.1",
            "architecture": {
                "architecture_type": "replication",
                "role_type": "replica",
                "replication_delay_seconds": 0,
            },
        }
        def probe(item):
            if item.host == "198.51.100.152":
                return primary_profile
            return {**replica_profile, "architecture": {**replica_profile["architecture"],
                "server_uuid": "replica-2" if item.host == "198.51.100.153" else "replica-3"}}
        test_connection.side_effect = probe
        get_host_overview.return_value = {"error": "测试环境不采集主机", "host_basic": None}

        result = service.apply_internal_discovery(self.db, probe_live=True)
        topology = service.topology_groups(self.db)
        inventory = service.list_inventory(self.db, asset_type="database_instance")

        self.assertEqual(topology["group_count"], 1)
        self.assertEqual(topology["groups"][0]["node_count"], 3)
        self.assertEqual(topology["groups"][0]["edges"], [])
        self.assertEqual({node["name"] for node in topology["groups"][0]["nodes"]}, {
            "mysql-primary",
            "orch-mysql02",
            "orch-mysql03",
        })
        discovered = [item for item in inventory if item["attributes"].get("discovery_method") == "topology_probe"]
        self.assertEqual(len(discovered), 2)
        self.assertTrue(all(item["attributes"]["connection_managed"] is False for item in discovered))
        self.assertTrue(all(item["attributes"]["db_version"] == "8.4.1" for item in discovered))
        self.assertTrue(all(item["health_status"] == "healthy" for item in discovered))
        normalized_peers = [
            item for item in normalized_service.list_instances(self.db)
            if item["attributes"].get("native_type") == "discovered_database_instance"
        ]
        self.assertEqual({item["name"] for item in normalized_peers}, {"orch-mysql02", "orch-mysql03"})

        renamed = normalized_peers[0]
        normalized_service.apply_correction(self.db, {
            "action": "update",
            "entity_type": "instance",
            "entity_id": renamed["id"],
            "changes": {"name": "人工命名的只读节点"},
            "reason": "使用业务实例别名",
        }, "tester")
        service.sync_normalized_asset_model(self.db, result["source_id"])
        self.assertEqual(normalized_service.get_instance(self.db, renamed["id"])["name"], "人工命名的只读节点")
        self.assertTrue(normalized_service.get_instance(self.db, renamed["id"])["name_customized"])

    @patch("app.cmdb.service.host_overview.get_instance_host_overview")
    @patch("app.cmdb.service.instance_service.test_connection")
    def test_replica_entrypoint_discovers_primary_and_sibling_replica(self, test_connection, get_host_overview):
        self.db.query(DBInstance).delete()
        self.db.add(DBInstance(
            instance_name="orch-mysql01",
            db_type="mysql",
            host="198.51.100.152",
            port=3306,
            username="root",
            password_cipher="database-secret",
            env_type="测试",
            system_name="orch-mysql-cluster",
            enabled=1,
        ))
        self.db.commit()

        profiles_by_host = {
            "198.51.100.152": {
                "version": "8.0.44",
                "architecture": {
                    "architecture_type": "replication",
                    "role_type": "replica",
                    "topology_group": "mysql:replication:primary-uuid",
                    "cluster_name": "orch-mysql-cluster",
                    "hostname": "orch-mysql01",
                    "server_uuid": "replica-1-uuid",
                    "member_key": "replica-1-uuid",
                    "members": [{
                        "member_key": "primary-uuid",
                        "server_uuid": "primary-uuid",
                        "name": "orch-mysql03",
                        "host": "198.51.100.154",
                        "port": 3306,
                        "role_type": "primary",
                    }],
                },
            },
            "198.51.100.154": {
                "version": "8.0.44",
                "architecture": {
                    "architecture_type": "replication",
                    "role_type": "primary",
                    "topology_group": "mysql:replication:primary-uuid",
                    "cluster_name": "orch-mysql-cluster",
                    "hostname": "orch-mysql03",
                    "server_uuid": "primary-uuid",
                    "member_key": "primary-uuid",
                    "members": [
                        {
                            "member_key": "replica-1-uuid",
                            "server_uuid": "replica-1-uuid",
                            "name": "orch-mysql01",
                            "host": "198.51.100.152",
                            "port": 3306,
                            "role_type": "replica",
                        },
                        {
                            "member_key": "replica-2-uuid",
                            "server_uuid": "replica-2-uuid",
                            "name": "orch-mysql02",
                            "host": "198.51.100.153",
                            "port": 3306,
                            "role_type": "replica",
                        },
                    ],
                },
            },
            "198.51.100.153": {
                "version": "8.0.44",
                "architecture": {
                    "architecture_type": "replication",
                    "role_type": "replica",
                    "topology_group": "mysql:replication:primary-uuid",
                    "cluster_name": "orch-mysql-cluster",
                    "hostname": "orch-mysql02",
                    "server_uuid": "replica-2-uuid",
                    "member_key": "replica-2-uuid",
                    "replication_delay_seconds": 0,
                    "members": [{
                        "member_key": "primary-uuid",
                        "server_uuid": "primary-uuid",
                        "name": "orch-mysql03",
                        "host": "198.51.100.154",
                        "port": 3306,
                        "role_type": "primary",
                    }],
                },
            },
        }
        test_connection.side_effect = lambda instance: profiles_by_host[instance.host]
        get_host_overview.return_value = {"error": "测试环境不采集主机", "host_basic": None}

        service.apply_internal_discovery(self.db, probe_live=True)
        topology = service.topology_groups(self.db)

        self.assertEqual(test_connection.call_count, 3)
        self.assertEqual(topology["group_count"], 1)
        group = topology["groups"][0]
        self.assertEqual(group["node_count"], 3)
        self.assertEqual(group["edges"], [])
        roles_by_host = {
            node["host"]: node["role"]
            for node in group["nodes"]
        }
        self.assertEqual(roles_by_host, {
            "198.51.100.152": "replica",
            "198.51.100.153": "replica",
            "198.51.100.154": "primary",
        })
        # Roles alone are insufficient; a later probe supplies directional evidence.
        for host in ("198.51.100.152", "198.51.100.153"):
            profiles_by_host[host]["architecture"].update(
                source_host="198.51.100.154", source_port=3306, source_uuid="primary-uuid",
            )
        service.apply_internal_discovery(self.db, probe_live=True)
        group = service.topology_groups(self.db)["groups"][0]
        hosts_by_node_id = {
            node["id"]: node["host"]
            for node in group["nodes"]
        }
        self.assertEqual(
            {
                (
                    hosts_by_node_id[edge["source"]],
                    hosts_by_node_id[edge["target"]],
                )
                for edge in group["edges"]
            },
            {
                ("198.51.100.154", "198.51.100.152"),
                ("198.51.100.154", "198.51.100.153"),
            },
        )

    @patch("app.cmdb.service.instance_service.test_connection")
    def test_oracle_primary_matches_discovered_standby_to_configured_asset(self, test_connection):
        self.db.query(DBInstance).delete()
        self.db.add_all([
            DBInstance(
                instance_name="mesdb",
                db_type="oracle",
                host="198.51.100.200",
                port=1521,
                username="sys",
                password_cipher="secret",
                service_name="mesdb",
                enabled=1,
            ),
            DBInstance(
                instance_name="reportdb",
                db_type="oracle",
                host="198.51.100.211",
                port=1521,
                username="sys",
                password_cipher="secret",
                service_name="reportdb",
                enabled=1,
            ),
        ])
        self.db.commit()

        def probe(instance):
            if instance.instance_name == "mesdb":
                return {
                    "architecture": {
                        "architecture_type": "data_guard",
                        "role_type": "primary",
                        "topology_group": "oracle:data_guard:mesdb:reportdb",
                        "cluster_name": "MES Data Guard",
                        "member_key": "mesdb",
                        "members": [
                            {
                                "member_key": "reportdb",
                                "name": "reportdb",
                                "host": "",
                                "port": 1521,
                                "role_type": "standby",
                                "health_status": "warning",
                            }
                        ],
                    }
                }
            raise RuntimeError("standby unavailable")

        test_connection.side_effect = probe
        service.apply_internal_discovery(self.db, probe_live=True)
        topology = service.topology_groups(self.db)

        self.assertEqual(topology["group_count"], 1)
        group = topology["groups"][0]
        self.assertEqual(group["architecture_type"], "data_guard")
        self.assertEqual(group["node_count"], 2)
        self.assertEqual(group["edges"], [])
        standby = next(node for node in group["nodes"] if node["role"] == "standby")
        self.assertEqual(standby["host"], "198.51.100.211")


class AssetCenterApiTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        for model in MODELS:
            model.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            add_mysql_pair(db)

        app = FastAPI()
        app.include_router(asset_router, prefix="/api/v1")

        def override_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_discovery_inventory_topology_and_override_flow(self):
        preview = self.client.post("/api/v1/assets/discovery/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["created_count"], 5)

        applied = self.client.post("/api/v1/assets/discovery/apply")
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.json()["applied_created_count"], 5)

        inventory = self.client.get("/api/v1/assets/inventory").json()["items"]
        replica = next(item for item in inventory if item["name"] == "mysql-replica")
        corrected = self.client.post(
            f"/api/v1/assets/{replica['id']}/overrides",
            json={
                "values": {"attributes.role_type": "standby"},
                "reason": "人工核验",
                "locked": True,
            },
        )
        self.assertEqual(corrected.status_code, 200)
        self.assertEqual(corrected.json()["attributes"]["role_type"], "standby")

        topology = self.client.get("/api/v1/assets/topology")
        self.assertEqual(topology.status_code, 200)
        self.assertEqual(topology.json()["groups"][0]["nodes"][1]["role"], "standby")
        self.assertEqual(len(self.client.get("/api/v1/assets/audit").json()["items"]), 1)

    def test_five_layer_api_tree_details_and_correction(self):
        self.assertEqual(self.client.post("/api/v1/assets/discovery/apply").status_code, 200)

        tree = self.client.get("/api/v1/assets/tree")
        clusters = self.client.get("/api/v1/assets/clusters")
        instances = self.client.get("/api/v1/assets/instances")
        hosts = self.client.get("/api/v1/assets/hosts")

        self.assertEqual(tree.status_code, 200)
        self.assertEqual(len(clusters.json()["items"]), 1)
        relations = clusters.json()["items"][0]["relations"]
        self.assertEqual(len(relations), 2)
        self.assertEqual({item["relation_type"] for item in relations}, {"member_of"})
        self.assertTrue(all(item["status"] == "active" and item["source_type"] == "instance" for item in relations))
        self.assertEqual(len(instances.json()["items"]), 2)
        self.assertEqual(len(hosts.json()["items"]), 2)
        instance = instances.json()["items"][1]
        self.assertEqual(self.client.get(f"/api/v1/assets/instances/{instance['id']}").status_code, 200)
        cluster_id = clusters.json()["items"][0]["id"]
        self.assertEqual(self.client.get(f"/api/v1/assets/clusters/{cluster_id}").status_code, 200)
        host_id = hosts.json()["items"][0]["id"]
        self.assertEqual(self.client.get(f"/api/v1/assets/hosts/{host_id}").status_code, 200)

        corrected = self.client.post("/api/v1/assets/corrections", json={
            "action": "update",
            "entity_type": "instance",
            "entity_id": instance["id"],
            "changes": {"role": "read_only"},
            "reason": "人工核验角色",
        })
        self.assertEqual(corrected.status_code, 200)
        self.assertEqual(corrected.json()["role"], "read_only")
        audit = self.client.get("/api/v1/assets/corrections").json()["items"]
        self.assertEqual(audit[0]["operator"], "admin")


if __name__ == "__main__":
    unittest.main()
