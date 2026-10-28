from dataclasses import dataclass, field
from typing import Any


class ProviderAuthenticationError(Exception):
    """Raised when a configured external provider rejects an authentication."""


@dataclass(frozen=True)
class AuthenticatedIdentity:
    provider_id: int
    subject: str
    username: str
    display_name: str = ""
    groups: list[str] = field(default_factory=list)
    claims: dict[str, Any] = field(default_factory=dict)
