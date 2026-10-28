EXPORTER_TYPES = [
    "node_exporter",
    "mysqld_exporter",
    "postgres_exporter",
    "oracle_exporter",
    "sqlserver_exporter",
    "nginx_exporter",
    "jmx_exporter",
    "elasticsearch_exporter",
]


def list_exporter_types() -> list[dict]:
    return [{"name": item, "status": "supported"} for item in EXPORTER_TYPES]
