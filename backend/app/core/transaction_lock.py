"""Transaction-scoped coordination using reserved, non-editable setting keys."""
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models.system_setting import DBSystemSetting


def lock_transaction(db, key: str):
    table = DBSystemSetting.__table__
    values = {"key": "internal.lock." + key, "value": "0", "value_type": "str", "category": "internal"}
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(table).values(**values).on_conflict_do_update(
            index_elements=[table.c.key], set_={"value": table.c.value},
        )
    elif dialect in {"mysql", "mariadb"}:
        statement = mysql_insert(table).values(**values).on_duplicate_key_update(value=table.c.value)
    elif dialect == "postgresql":
        statement = pg_insert(table).values(**values).on_conflict_do_update(
            index_elements=[table.c.key], set_={"value": table.c.value},
        )
    else:
        raise ValueError(f"Unsupported coordination database: {dialect}")
    db.execute(statement)
    return db.query(DBSystemSetting).filter_by(key=values["key"]).populate_existing().with_for_update().one()
