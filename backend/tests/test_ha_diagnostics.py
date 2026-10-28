import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cmdb import ha_diagnostics
from app.cmdb.models import (
    AssetCluster,
    AssetClusterHealthSnapshot,
    AssetEnvironment,
    AssetHost,
    AssetInstance,
    AssetRegion,
    AssetRelation,
    AssetSite,
    AssetZone,
)


MODELS = (
    AssetRegion,
    AssetSite,
    AssetZone,
    AssetEnvironment,
    AssetHost,
    AssetCluster,
    AssetInstance,
    AssetRelation,
    AssetClusterHealthSnapshot,
)


class HADiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in MODELS:
            model.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        region = AssetRegion(code="east", name="华东一区")
        self.db.add(region)
        self.db.flush()
        site = AssetSite(code="sh", name="上海 IDC", region_id=region.id)
        self.db.add(site)
        self.db.flush()
        zone = AssetZone(code="a", name="A 区", region_id=region.id, site_id=site.id)
        self.db.add(zone)
        self.db.flush()
        environment = AssetEnvironment(
            code="prod",
            name="MES 生产环境",
            env_type="production",
            region_id=region.id,
            site_id=site.id,
            zone_id=zone.id,
        )
        self.db.add(environment)
        self.db.commit()
        self.location = {
            "region_id": region.id,
            "site_id": site.id,
            "zone_id": zone.id,
            "env_id": environment.id,
        }

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_cluster(self, name, db_type, architecture, attributes=None):
        cluster = AssetCluster(
            stable_key=f"cluster:{name}",
            name=name,
            db_type=db_type,
            cluster_type="database",
            architecture=architecture,
            status="healthy",
            discovery_source="automatic",
            attributes=attributes or {},
            **self.location,
        )
        self.db.add(cluster)
        self.db.commit()
        return cluster

    def add_member(
        self,
        cluster,
        name,
        ip,
        role,
        *,
        status="healthy",
        attributes=None,
        native_instance_id=None,
    ):
        host = AssetHost(
            stable_key=f"host:{ip}",
            name=name,
            hostname=name,
            ip=ip,
            status=status,
            discovery_source="automatic",
            **self.location,
        )
        self.db.add(host)
        self.db.flush()
        member = AssetInstance(
            stable_key=f"instance:{cluster.id}:{ip}",
            name=name,
            db_type=cluster.db_type,
            ip=ip,
            port={"MySQL": 3306, "PostgreSQL": 5432, "Oracle": 1521, "SQLServer": 1433}[cluster.db_type],
            version="1.0",
            role=role,
            host_id=host.id,
            cluster_id=cluster.id,
            status=status,
            native_instance_id=native_instance_id,
            discovery_source="automatic",
            attributes={"discovery_status": "success", "architecture_verified": True, **(attributes or {})},
            **self.location,
        )
        self.db.add(member)
        self.db.commit()
        return member

    def add_relation(self, source, target, *, discovery_source="automatic", attributes=None):
        relation = AssetRelation(
            relation_key=f"test:{source.id}:replicates_to:{target.id}:{discovery_source}",
            source_type="instance",
            source_id=source.id,
            target_type="instance",
            target_id=target.id,
            relation_type="replicates_to",
            status="active",
            discovery_source=discovery_source,
            attributes=attributes or {
                "discovery_method": "cmdb_relation_projection",
                "provenance": "observed",
                "evidence": {
                    "state": "verified",
                    "upstream": {"host": source.ip, "port": source.port},
                },
            },
        )
        self.db.add(relation)
        self.db.commit()
        return relation

    def test_healthy_postgresql_streaming_cluster(self):
        cluster = self.add_cluster("pg-prod", "PostgreSQL", "streaming_replication")
        primary = self.add_member(cluster, "pg01", "10.0.0.1", "primary", native_instance_id=11)
        replica = self.add_member(
            cluster,
            "pg02",
            "10.0.0.2",
            "replica",
            attributes={"replication_delay_seconds": 2},
            native_instance_id=12,
        )
        self.add_relation(primary, replica)

        result = ha_diagnostics.assess_cluster(self.db, cluster.id)

        self.assertEqual(result["summary"]["health_status"], "healthy")
        self.assertEqual(result["summary"]["primary_count"], 1)
        self.assertEqual(result["summary"]["secondary_count"], 1)
        self.assertEqual(result["summary"]["max_lag_seconds"], 2)
        self.assertEqual(result["summary"]["replication_state"], "verified")
        self.assertTrue(result["action_readiness"]["ready_for_plan"])
        self.assertIn("/operations/postgresql/11/", result["action_readiness"]["plan_endpoint"])

    def test_multiple_mysql_primaries_are_critical(self):
        cluster = self.add_cluster("mysql-prod", "MySQL", "primary_replica")
        self.add_member(cluster, "mysql01", "10.0.1.1", "primary", native_instance_id=21)
        self.add_member(cluster, "mysql02", "10.0.1.2", "primary", native_instance_id=22)

        result = ha_diagnostics.assess_cluster(self.db, cluster.id)

        self.assertEqual(result["summary"]["health_status"], "critical")
        self.assertTrue(result["summary"]["split_brain_risk"])
        self.assertIn("HA_MULTIPLE_PRIMARY", {item["code"] for item in result["findings"]})
        self.assertFalse(result["action_readiness"]["ready_for_plan"])

    def test_mgr_quorum_loss_is_critical(self):
        cluster = self.add_cluster(
            "mysql-mgr",
            "MySQL",
            "mgr",
            {"single_primary_mode": True, "vip": "10.0.2.10"},
        )
        self.add_member(cluster, "mgr01", "10.0.2.1", "primary", native_instance_id=31)
        self.add_member(
            cluster,
            "mgr02",
            "10.0.2.2",
            "replica",
            status="warning",
            attributes={"discovery_status": "failed", "discovery_error": "timeout"},
        )
        self.add_member(
            cluster,
            "mgr03",
            "10.0.2.3",
            "replica",
            status="warning",
            attributes={"discovery_status": "failed", "discovery_error": "timeout"},
        )

        result = ha_diagnostics.assess_cluster(self.db, cluster.id)

        self.assertTrue(result["summary"]["quorum_risk"])
        self.assertEqual(result["summary"]["health_status"], "critical")
        self.assertIn("HA_QUORUM_RISK", {item["code"] for item in result["findings"]})

    def test_adg_lag_and_gap_block_switchover_plan(self):
        cluster = self.add_cluster("oracle-adg", "Oracle", "adg", {"service_name": "mesdb"})
        self.add_member(cluster, "ora01", "10.0.3.1", "adg_primary", native_instance_id=41)
        self.add_member(
            cluster,
            "ora02",
            "10.0.3.2",
            "adg_standby",
            attributes={"apply_lag_seconds": 600, "archive_gap_count": 2},
            native_instance_id=42,
        )

        result = ha_diagnostics.assess_cluster(self.db, cluster.id)

        codes = {item["code"] for item in result["findings"]}
        self.assertEqual(result["summary"]["health_status"], "critical")
        self.assertIn("HA_REPLICATION_LAG_CRITICAL", codes)
        self.assertIn("HA_LOG_GAP", codes)
        self.assertFalse(result["action_readiness"]["ready_for_plan"])
        self.assertIn("/oracle/41/", result["action_readiness"]["plan_endpoint"])

    def test_unverified_metadata_is_not_reported_as_healthy(self):
        cluster = self.add_cluster("sql-ag", "SQLServer", "alwayson", {"listener": "sql-ag.local"})
        member = self.add_member(cluster, "sql01", "10.0.4.1", "alwayson_primary", native_instance_id=51)
        member.attributes = {"discovery_status": "configured", "architecture_verified": False}
        self.db.commit()

        result = ha_diagnostics.assess_cluster(self.db, cluster.id)

        self.assertEqual(result["summary"]["health_status"], "warning")
        self.assertIn("HA_DATA_UNVERIFIED", {item["code"] for item in result["findings"]})
        self.assertFalse(result["action_readiness"]["ready_for_plan"])

    def test_snapshot_history_preserves_assessment_evidence(self):
        cluster = self.add_cluster("pg-history", "PostgreSQL", "streaming_replication")
        self.add_member(cluster, "pg01", "10.0.5.1", "primary", native_instance_id=61)
        self.add_member(cluster, "pg02", "10.0.5.2", "replica", attributes={"replication_delay_seconds": 4})

        captured = ha_diagnostics.capture_snapshot(self.db, cluster.id, source="unit_test")
        history = ha_diagnostics.list_snapshots(self.db, cluster.id)

        self.assertEqual(captured["snapshot"]["source"], "unit_test")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["member_count"], 2)
        self.assertEqual(len(history[0]["member_facts"]), 2)


if __name__ == "__main__":
    unittest.main()
