export const mysqlSchemaName = (row) => row?.table_schema || row?.TABLE_SCHEMA || row?.Table_schema || row?.schema_name || row?.database_name || row?.object_schema || row?.OBJECT_SCHEMA || row?.Object_schema || "-";

export const mysqlTableName = (row) => row?.table_name || row?.TABLE_NAME || row?.Table_name || row?.object_name || row?.OBJECT_NAME || row?.Object_name || "-";
