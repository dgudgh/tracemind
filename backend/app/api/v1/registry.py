from app.api.v1 import (
    ai,
    ai_config,
    assistant,
    alert_events,
    auth,

    capacity,
    connection_security,
    config_governance,

    diagnosis_center,

    diagnosis_rules,
    external_ecosystem,
    engine_catalog,
    generic_operations,
    inspection,
    instances,
    module_config,
    mysql,
    mysql_ops,
    observability,
    operations,
    oracle,
    oracle_awr,
    performance,
    postgresql_ops,

    release_center,
    report,
    report_center,
    scheduler,
    security,
    settings as settings_api,
    setup,
    slow_sql,

    sqlserver_ops,
    ssh,
    system,
    system_init,
    users,
)
from app.cmdb.api import asset_router, router as cmdb_router


API_MODULE_GROUPS = [
]


routers = [router for group in API_MODULE_GROUPS for router in group["routers"]]


def get_api_module_catalog():
    return [
        {
            "key": group["key"],
            "name": group["name"],
            "routers": [
                {
                    "prefix": router.prefix,
                    "tags": router.tags,
                }
                for router in group["routers"]
            ],
        }
        for group in API_MODULE_GROUPS
    ]
