from typing import Any, Protocol

from app.identity.contracts import AuthenticatedIdentity, ProviderAuthenticationError


class LdapDirectoryClient(Protocol):
    def authenticate(self, configuration: dict[str, Any], credentials: dict[str, Any]) -> dict[str, Any]: ...


class Ldap3DirectoryClient:
    def authenticate(self, configuration: dict[str, Any], credentials: dict[str, Any]) -> dict[str, Any]:
        try:
            import ldap3
            from ldap3.utils.conv import escape_filter_chars
            from ldap3.utils.dn import escape_rdn
        except ImportError as exc:
            raise ProviderAuthenticationError("LDAP support requires the ldap3 package") from exc

        server_url = str(configuration.get("server_url") or configuration.get("uri") or "").strip()
        if not server_url.startswith(("ldap://", "ldaps://")):
            raise ProviderAuthenticationError("LDAP server_url must use ldap:// or ldaps://")
        username = str(credentials.get("username") or "").strip()
        password = str(credentials.get("password") or "")
        if not username or not password:
            raise ProviderAuthenticationError("LDAP username and password are required")

        timeout = max(2, min(int(configuration.get("timeout_seconds") or 10), 30))
        server = ldap3.Server(server_url, get_info=ldap3.NONE, connect_timeout=timeout)
        search_base = str(configuration.get("user_search_base") or "").strip()
        user_filter = str(configuration.get("user_filter") or "(uid={username})")
        attributes = [
            str(configuration.get("username_attribute") or "uid"),
            str(configuration.get("display_name_attribute") or "displayName"),
            str(configuration.get("group_attribute") or "memberOf"),
        ]
        bind_dn_template = str(configuration.get("bind_dn_template") or "").strip()
        service_bind_dn = str(configuration.get("bind_dn") or "").strip()
        service_bind_password = str(configuration.get("bind_password") or "")

        connection = None
        try:
            if bind_dn_template:
                user_dn = bind_dn_template.format(username=escape_rdn(username))
                connection = ldap3.Connection(server, user=user_dn, password=password, auto_bind=True, receive_timeout=timeout)
            else:
                if not search_base:
                    raise ProviderAuthenticationError("LDAP user_search_base or bind_dn_template is required")
                connection = ldap3.Connection(
                    server,
                    user=service_bind_dn or None,
                    password=service_bind_password or None,
                    auto_bind=True,
                    receive_timeout=timeout,
                )
                filter_text = user_filter.format(username=escape_filter_chars(username))
                if not connection.search(search_base, filter_text, attributes=attributes, size_limit=2) or len(connection.entries) != 1:
                    raise ProviderAuthenticationError("LDAP user was not found or was ambiguous")
                user_dn = connection.entries[0].entry_dn
                connection.unbind()
                connection = ldap3.Connection(server, user=user_dn, password=password, auto_bind=True, receive_timeout=timeout)

            entry = None
            if search_base:
                filter_text = user_filter.format(username=escape_filter_chars(username))
                connection.search(search_base, filter_text, attributes=attributes, size_limit=1)
                entry = connection.entries[0] if connection.entries else None
            values = dict(getattr(entry, "entry_attributes_as_dict", {}) or {})
            username_attr = attributes[0]
            display_attr = attributes[1]
            group_attr = attributes[2]
            resolved_username = _first(values.get(username_attr)) or username
            return {
                "subject": str(getattr(entry, "entry_dn", "") or user_dn),
                "username": str(resolved_username),
                "display_name": str(_first(values.get(display_attr)) or resolved_username),
                "groups": [str(item) for item in _as_list(values.get(group_attr))],
                "claims": {"dn": str(getattr(entry, "entry_dn", "") or user_dn)},
            }
        except ProviderAuthenticationError:
            raise
        except Exception as exc:
            raise ProviderAuthenticationError("LDAP authentication failed") from exc
        finally:
            try:
                if connection is not None:
                    connection.unbind()
            except Exception:
                pass


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _first(value: Any) -> Any:
    values = _as_list(value)
    return values[0] if values else None


class LDAPIdentityProvider:
    """LDAP adapter boundary; deployments supply a directory client implementation."""

    def __init__(self, directory_client: LdapDirectoryClient | None = None):
        self.directory_client = directory_client or Ldap3DirectoryClient()

    def authenticate(self, provider, credentials: dict[str, Any]) -> AuthenticatedIdentity:
        try:
            payload = self.directory_client.authenticate(provider.configuration or {}, credentials)
        except ProviderAuthenticationError:
            raise
        except Exception as exc:
            raise ProviderAuthenticationError("LDAP authentication failed") from exc
        if not isinstance(payload, dict) or not payload.get("subject") or not payload.get("username"):
            raise ProviderAuthenticationError("LDAP response did not include subject and username")
        return AuthenticatedIdentity(
            provider_id=provider.id,
            subject=str(payload["subject"]),
            username=str(payload["username"]),
            display_name=str(payload.get("display_name") or ""),
            groups=[str(group) for group in payload.get("groups", [])],
            claims=dict(payload.get("claims") or {}),
        )
