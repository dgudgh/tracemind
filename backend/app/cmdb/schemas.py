from typing import Any

from pydantic import BaseModel, Field


class CmdbSourceCreate(BaseModel):
    source_name: str = Field(..., min_length=1, max_length=200)
    source_type: str = Field(..., min_length=1, max_length=50)
    endpoint: str | None = Field(None, max_length=500)
    enabled: int = Field(1, ge=0, le=1)
    remark: str | None = None


class CmdbSourceUpdate(BaseModel):
    source_name: str | None = Field(None, min_length=1, max_length=200)
    source_type: str | None = Field(None, min_length=1, max_length=50)
    endpoint: str | None = Field(None, max_length=500)
    enabled: int | None = Field(None, ge=0, le=1)
    remark: str | None = None


class CmdbRelationCreate(BaseModel):
    source_id: int
    source_asset_id: int
    target_asset_id: int
    relation_type: str = Field(..., min_length=1, max_length=100)
    external_id: str | None = Field(None, max_length=200)
    status: str = Field("active", max_length=40)
    raw_payload: dict[str, Any] | list[Any] | None = None


class CmdbBindingCreate(BaseModel):
    instance_id: int
    asset_id: int
    binding_status: str = Field("active", max_length=40)
    match_rule: str | None = Field(None, max_length=100)
    confidence: int = Field(100, ge=0, le=100)


class CmdbSyncRequest(BaseModel):
    source_id: int
    items: list[dict[str, Any]]
    authoritative_snapshot: bool = False


class AssetOverrideRequest(BaseModel):
    values: dict[str, Any]
    reason: str | None = Field(None, max_length=500)
    locked: bool = True


class AssetDiscoveryRequest(BaseModel):
    probe_live: bool = False


class AssetCorrectionRequest(BaseModel):
    action: str = Field("update", max_length=60)
    entity_type: str = Field(..., max_length=50)
    entity_id: int
    changes: dict[str, Any] = Field(default_factory=dict)
    target_id: int | None = None
    target_role: str | None = Field(None, max_length=60)
    entity_ids: list[int] = Field(default_factory=list)
    reason: str = Field(..., min_length=1, max_length=500)
