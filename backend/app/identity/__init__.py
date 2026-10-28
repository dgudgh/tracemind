"""Enterprise identity domain primitives.

The package deliberately has no side effects on the application's central
authentication path.  Wiring is owned by the authentication composition root.
"""

from app.identity.models import ExternalIdentity, GroupMapping, IdentityProvider, LoginAudit

__all__ = ["ExternalIdentity", "GroupMapping", "IdentityProvider", "LoginAudit"]
