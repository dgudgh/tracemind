from typing import Any

from pydantic import BaseModel, Field, model_validator


_SECRET_MARKERS = ("password", "passwd", "secret", "token", "api_key", "private_key", "client_secret")


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            any(marker in str(key).lower() for marker in _SECRET_MARKERS) or _contains_secret_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


class IdentityProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    provider_type: str = Field(..., pattern="^(ldap|oidc)$")
    priority: int = Field(100, ge=1, le=10000)
    enabled: bool = True
    configuration: dict[str, Any] = Field(default_factory=dict)
    secret_config: dict[str, Any] | None = None

    @model_validator(mode="after")
    def reject_public_secrets(self):
        if _contains_secret_key(self.configuration):
            raise ValueError("敏感字段必须放入 secret_config")
        return self


class IdentityProviderRead(BaseModel):
    id: int
    name: str
    provider_type: str
    priority: int
    enabled: bool
    configuration: dict[str, Any]
    has_secret: bool = False


class GroupMappingCreate(BaseModel):
    external_group: str = Field(..., min_length=1, max_length=200)
    role: str | None = Field(None, pattern="^(user|operator|admin)$")
    module_keys: list[str] = Field(default_factory=list)
    enabled: bool = True


class ExternalLoginRequest(BaseModel):
    credentials: dict[str, Any] = Field(default_factory=dict)
