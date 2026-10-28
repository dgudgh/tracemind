"""Startup policy; backups and service quiescence belong to the release manager."""

import os


class SchemaUpgradeRequired(RuntimeError):
    pass


def auto_upgrade_enabled(bind, allow_upgrade: bool | None = None) -> bool:
    if allow_upgrade is not None:
        if not isinstance(allow_upgrade, bool):
            raise ValueError("allow_upgrade must be a boolean")
        return allow_upgrade
    value = os.environ.get("TRACEMIND_AUTO_MIGRATE")
    if value is not None:
        value = value.strip().lower()
        if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise ValueError("TRACEMIND_AUTO_MIGRATE must be a boolean")
        return value in {"1", "true", "yes", "on"}
    from app.core.config import settings

    return bind.dialect.name == "sqlite" and settings.APP_MODE in {
        "dev",
        "test",
        "exe_sqlite",
        "schema_check",
    }


def require_upgrade_permission(pending, allowed: bool) -> None:
    if pending and not allowed:
        raise SchemaUpgradeRequired(
            "Platform schema upgrade required: "
            + ", ".join(pending)
            + ". Automatic schema upgrades are disabled. Quiesce the service and use the "
            "release migration workflow with a verified backup; explicitly authorize the "
            "migration step with allow_upgrade=True or TRACEMIND_AUTO_MIGRATE=1. "
            "Startup does not create external database backups."
        )
