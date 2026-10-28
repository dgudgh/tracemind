# Inspection script sources

The ten files in `source-manifest.json` are byte-identical copies of the
user-provided inspection script library. `user-inspection-scripts` is a logical
origin, not a filesystem location or runtime dependency. Each entry records its
filename, source identity, byte size and SHA-256. Preserve original bytes on
future syncs and update the manifest and regression expectations together.
The existing packaging specs include this directory as bundled `scripts`.
The pre-existing `dbcheck12c.sql` is not part of this sync or the active catalog.
No installer or `MySQLShellInstall` is included in this source set.

## Oracle API semantics

`dbcheck10g.sql`, `dbcheck_noncdb.sql` and `dbcheck_cdb.sql` supply report-body
SELECT/WITH sections to the Oracle API collector. They are not executed as
SQL*Plus scripts. Profile matching still runs before section extraction when
an expected profile is provided. The parser currently extracts 53, 56 and 56
sections respectively, with multiple sections sharing template metric keys.

SQL*Plus SET/COLUMN/NEW_VALUE, DEFINE, SPOOL, HOST, formatting commands and
session-altering statements are not interpreted. PL/SQL blocks, chart SQL and
the trailing SQL*Plus error-log section are excluded. Standalone DML/DDL are
not selected for execution. Unresolved substitutions and guarded SYS internal
tables remain skipped; this extractor is not an arbitrary SQL security sandbox.
API results are structured evidence with a 200-row limit per section, not the
original HTML report. SQL-embedded HTML values can remain in that evidence.

API substitutions are explicit and do not execute script initialization SQL.
Non-CDB DG uses the detected version: 11.1 selects `synchronization_status`,
`SYNC`, `OK`; 11.2+ selects `gap_status`, `GAP`, `NO GAP`. These are fixed
allowlisted replacements, including an identifier and already-quoted string
contents. An unknown or unsupported version leaves the DG variables unresolved
and the section skipped. AWR/CPU substitutions retain existing API semantics;
the original SQL*Plus snapshot/window initialization is not reproduced fully.
Legacy database driver/client compatibility and catalog permissions still need
deployment-specific validation; parser tests are not live database acceptance.

## Reference-only files

`mysql_check.sh`, `mysql_check.ps1`, `pg_check.sh`, `pg_check.ps1`,
`mssql_check.sh`, `oscheck.sh` and `oscheck.ps1` are packaged references only.
The API continues using its existing Python database/host collectors, without
launching these shell or PowerShell files. Shell and PowerShell section layouts
are not necessarily equivalent. Their presence does not add OS access, backup
verification, or complete script behavior to a database-only connection.
