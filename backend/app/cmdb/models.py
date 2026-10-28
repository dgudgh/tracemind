from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CmdbSource(Base):
    __tablename__ = "cmdb_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class CmdbAsset(Base):
    __tablename__ = "cmdb_asset"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_cmdb_asset_source_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    asset_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    raw_payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    attributes: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class CmdbRelation(Base):
    __tablename__ = "cmdb_relation"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_cmdb_relation_source_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_asset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    target_asset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    relation_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    raw_payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class CmdbSyncJob(Base):
    __tablename__ = "cmdb_sync_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="success", index=True)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_confirmation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CmdbBinding(Base):
    __tablename__ = "cmdb_binding"
    __table_args__ = (UniqueConstraint("instance_id", "asset_id", name="uq_cmdb_binding_instance_asset"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    asset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    binding_status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    match_rule: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class CmdbAssetOverride(Base):
    __tablename__ = "cmdb_asset_override"
    __table_args__ = (UniqueConstraint("asset_id", "field_path", name="uq_cmdb_asset_override_field"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    field_path: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    override_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    locked: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="admin")
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class CmdbChangeEvent(Base):
    __tablename__ = "cmdb_change_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    field_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    old_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="manual", index=True)
    operator: Mapped[str] = mapped_column(String(100), nullable=False, default="admin")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AssetRegion(Base):
    __tablename__ = "asset_region"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="automatic")
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AssetSite(Base):
    __tablename__ = "asset_site"
    __table_args__ = (UniqueConstraint("region_id", "code", name="uq_asset_site_region_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="automatic")
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AssetZone(Base):
    __tablename__ = "asset_zone"
    __table_args__ = (UniqueConstraint("site_id", "code", name="uq_asset_zone_site_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    site_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="automatic")
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AssetEnvironment(Base):
    __tablename__ = "asset_environment"
    __table_args__ = (UniqueConstraint("zone_id", "code", name="uq_asset_environment_zone_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    site_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    zone_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    env_type: Mapped[str] = mapped_column(String(50), nullable=False, default="unknown", index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="automatic")
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AssetHost(Base):
    __tablename__ = "asset_host"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stable_key: Mapped[str] = mapped_column(String(240), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    hostname: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    ip: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    os_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cpu_cores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    region_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    site_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    zone_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    env_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown", index=True)
    source_asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    discovery_source: Mapped[str] = mapped_column(String(50), nullable=False, default="automatic", index=True)
    manual_fields: Mapped[list | None] = mapped_column(JSON, nullable=True)
    attributes: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AssetCluster(Base):
    __tablename__ = "asset_cluster"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stable_key: Mapped[str] = mapped_column(String(240), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    cluster_type: Mapped[str] = mapped_column(String(50), nullable=False, default="database", index=True)
    architecture: Mapped[str] = mapped_column(String(60), nullable=False, default="single", index=True)
    region_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    site_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    zone_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    env_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown", index=True)
    source_asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    discovery_source: Mapped[str] = mapped_column(String(50), nullable=False, default="automatic", index=True)
    manual_fields: Mapped[list | None] = mapped_column(JSON, nullable=True)
    attributes: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AssetInstance(Base):
    __tablename__ = "asset_instance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stable_key: Mapped[str] = mapped_column(String(240), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    ip: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    role: Mapped[str] = mapped_column(String(60), nullable=False, default="unknown", index=True)
    host_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    region_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    site_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    zone_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    env_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown", index=True)
    native_instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    discovery_source: Mapped[str] = mapped_column(String(50), nullable=False, default="automatic", index=True)
    manual_fields: Mapped[list | None] = mapped_column(JSON, nullable=True)
    attributes: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AssetRelation(Base):
    __tablename__ = "asset_relation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    relation_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    relation_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    discovery_source: Mapped[str] = mapped_column(String(50), nullable=False, default="automatic", index=True)
    attributes: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AssetClusterHealthSnapshot(Base):
    __tablename__ = "asset_cluster_health_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    architecture: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    health_status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="current_metadata", index=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reachable_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    primary_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    secondary_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_lag_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    log_gap_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    split_brain_risk: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quorum_risk: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    findings: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    member_facts: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    endpoint_facts: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AssetDiscoveryBatch(Base):
    __tablename__ = "asset_discovery_batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="success", index=True)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suggested_cluster_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_instance_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AssetCorrectionAudit(Base):
    __tablename__ = "asset_correction_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    before_value: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    after_value: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    operator: Mapped[str] = mapped_column(String(100), nullable=False, default="admin", index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
