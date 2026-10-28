from sqlalchemy import inspect, text

from app.models.inspection_template_version import InspectionTemplateVersion


REVISION = "20260908_03_inspection_template_versions"
DESCRIPTION = "Append-only inspection template version history and restore provenance"


def upgrade(connection) -> None:
    table = InspectionTemplateVersion.__table__
    table.create(connection, checkfirst=True)
    # Guard direct SQL as well as ORM mutations. Re-running upgrade is safe.
    if connection.dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            connection.execute(text(
                f"CREATE TRIGGER IF NOT EXISTS inspection_template_version_no_{operation.lower()} "
                f"BEFORE {operation} ON {table.name} BEGIN "
                "SELECT RAISE(ABORT, 'Inspection template versions are immutable'); END"
            ))
    elif connection.dialect.name == "postgresql":
        connection.execute(text(
            "CREATE OR REPLACE FUNCTION reject_inspection_template_version_mutation() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'Inspection template versions are immutable'; END; $$"
        ))
        connection.execute(text(
            "DROP TRIGGER IF EXISTS inspection_template_version_immutable ON inspection_template_version"
        ))
        connection.execute(text(
            "CREATE TRIGGER inspection_template_version_immutable BEFORE UPDATE OR DELETE "
            "ON inspection_template_version FOR EACH ROW "
            "EXECUTE FUNCTION reject_inspection_template_version_mutation()"
        ))
    elif connection.dialect.name in {"mysql", "mariadb"}:
        for operation in ("UPDATE", "DELETE"):
            name = f"inspection_template_version_no_{operation.lower()}"
            exists = connection.execute(text(
                "SELECT 1 FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = DATABASE() AND TRIGGER_NAME = :name"
            ), {"name": name}).first()
            if not exists:
                connection.execute(text(
                    f"CREATE TRIGGER {name} BEFORE {operation} ON {table.name} "
                    "FOR EACH ROW SIGNAL SQLSTATE '45000' "
                    "SET MESSAGE_TEXT = 'Inspection template versions are immutable'"
                ))


def downgrade(connection) -> None:
    if inspect(connection).has_table(InspectionTemplateVersion.__tablename__):
        raise RuntimeError("Inspection template history is retained; destructive downgrade is not supported")
