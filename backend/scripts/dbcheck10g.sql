-- |------------------------------------------------------------------------------------|
-- |               Copyright (c) 2015-2100 lucifer. All rights reserved.                |
-- |------------------------------------------------------------------------------------|
-- | DATABASE : Oracle                                                                  |
-- | FILE     : dbcheck10g.sql                                                          |
-- | CLASS    : Database Administration                                                 |
-- | PURPOSE  : This SQL script provides a detailed report (in HTML format) on          |
-- |            all database metrics including installed options, storage,              |
-- |            performance data, AND  security.                                        |
-- | VERSION  : This script was designed for Oracle Database 11g.                       |
-- | USAGE    :                                                                         |
-- | sqlplus / as sysdba @dbcheck10g.sql                                                |
-- |                                                                                    |
-- | NOTE     : AS with any code, ensure to test this script in a development           |
-- |            environment before attempting to run it in production.                  |
-- +------------------------------------------------------------------------------------+

prompt Note1: Information about Instance
set line 9999 timing off
col NAME for a10
col CREATED format a20
col DATABASE_ROLE format a20
col LOG_MODE format a13
col OPEN_MODE format a20
col VERSION format a10
col sessionid format a20

BREAK ON REPORT ON INST_ID ON OWNER ON INSTANCE_NUMBER ON INSTANCE_NAME ON ts_name ON bs_key ON ROLE ON SNAP_ID ON snap_date on group_name on profile

SELECT d.INST_ID,
       d.DBID,
       d.NAME,
       d.DATABASE_ROLE,
       TO_CHAR(d.CREATED, 'yyyy-mm-dd HH24:mi:ss') CREATED,
       d.LOG_MODE,
       d.OPEN_MODE,
       (SELECT b.VERSION FROM v$instance b WHERE ROWNUM = 1) VERSION,
       (SELECT a.SID || ',' || b.SERIAL# || ',' || c.SPID
          FROM v$mystat a, v$session b, v$process c
         WHERE a.SID = b.SID
           AND  b.PADDR = c.ADDR
           AND  ROWNUM = 1) sessionid
  FROM gv$database d;

prompt
prompt Note2: Information about Recyclebin
col owner format a15
set pagesize 1000
set feedback off
SELECT nvl(a.owner, 'SUM') owner,
       round(SUM(a.space *
                 (SELECT value FROM v$parameter WHERE name = 'db_block_size')) / 1024 / 1024,
             2) recyb_size_M,
       count(1) recyb_cnt
  FROM dba_recyclebin a
 GROUP BY ROLLUP(a.owner);

prompt 
prompt +------------------------------------------------------------------------------------------------------------+    
prompt |                                    Oracle Database health Check script                                     |    
prompt |------------------------------------------------------------------------------------------------------------+    
prompt |                              Copyright (c) 2022-2100 lpc. All rights reserved.                             |    
prompt +------------------------------------------------------------------------------------------------------------+  
prompt

prompt DBHealthCheck  Author: Lucifer
prompt 
prompt +----------------------------------------------------------------------------+
prompt Now DBCheck staring, the time cost depending on size of database.
prompt Begining ......
prompt +----------------------------------------------------------------------------+
prompt

-- +----------------------------------------------------------------------------+
-- |                           SCRIPT SETTINGS                                  |
-- +----------------------------------------------------------------------------+
-- set sqlplus format
set termout       off
set echo          off
set feedback      off
set heading       off
set verify        off
set wrap          on
set trimspool     on
set serveroutput  on size unlimited
set escape        on
set sqlblanklines on
set ARRAYSIZE  500

set pagesize 50000
set linesize 32767
set numwidth 50
set long     2000000000 LONGCHUNKSIZE 100000

clear buffer computes columns
alter session set nls_timestamp_format='YYYY-MM-DD HH24:MI:SS';
alter session set NLS_DATE_FORMAT='YYYY-MM-DD HH24:mi:ss';

prompt

host echo '-----Oracle Database  Check STRAT, Starting Collect Data Dictionary Information----'

prompt Please Waiting......
host echo start...Set Environment Variables, Configure html headers.....

--------------------------------------------------------------------------------------------------------------------------------------------------------
--------------------------------------------------------------------------------------------------------------------------------------------------------

-- +----------------------------------------------------------------------------+
-- |                   GATHER DATABASE REPORT INFORMATION                       |
-- +----------------------------------------------------------------------------+
-- Configure Enviroment
-- Date Information
COLUMN tdate NEW_VALUE _date NOPRINT
COLUMN sdate NEW_VALUE _sdate NOPRINT
COLUMN time NEW_VALUE _time NOPRINT
COLUMN date_time NEW_VALUE _date_time NOPRINT
COLUMN spool_time NEW_VALUE _spool_time NOPRINT
COLUMN date_time_timezone NEW_VALUE _date_time_timezone NOPRINT
COLUMN v_current_user NEW_VALUE _v_current_user NOPRINT
SELECT TO_CHAR(SYSDATE, 'YYYY-MM-DD') tdate,
       TO_CHAR(SYSDATE, 'YYYYMMDD') sdate,
       TO_CHAR(SYSDATE, 'HH24:MI:SS') time,
       TO_CHAR(SYSDATE, 'YYYY-MM-DD HH24:MI:SS') date_time,
       TO_CHAR(systimestamp, 'YYYY-MM-DD  (') ||
       TRIM(TO_CHAR(systimestamp, 'Day')) ||
       TO_CHAR(systimestamp, ') HH24:MI:SS AM') ||
       trim(TO_CHAR(systimestamp, ' "timezone" TZR')) date_time_timezone,
       TO_CHAR(SYSDATE, 'YYYYMMDDHH24MISS') spool_time,
       user v_current_user
  FROM dual;

-- Database Information
COLUMN dbVERSION NEW_VALUE _dbVERSION NOPRINT
COLUMN dbVERSION1 NEW_VALUE _dbVERSION1 NOPRINT
COLUMN instance_number NEW_VALUE _instance_number NOPRINT
SELECT b.VERSION       dbVERSION,
       instance_number instance_number,
       substr(b.VERSION,1,instr(b.VERSION,'.')-1) dbVERSION1
  FROM v$instance b;

-- Hostname
COLUMN hostname NEW_VALUE _hostname NOPRINT
SELECT host_name hostname FROM v$instance;

-- Hostnames
COLUMN host_name_all NEW_VALUE _host_name_all NOPRINT
SELECT 'Hosts: [' || WMSYS.WM_CONCAT(host_name) || '] ' host_name_all FROM gv$instance g;

-- Instance names
COLUMN instance_name_all NEW_VALUE _instance_name_all NOPRINT
SELECT 'Instances: [' || WMSYS.WM_CONCAT(instance_name) || '] ' instance_name_all FROM gv$instance g;

-- Database Startup Time
COLUMN startup_time NEW_VALUE _startup_time NOPRINT
SELECT CASE np.value
         WHEN 'TRUE' then
          WMSYS.WM_CONCAT('[INST_ID ' || d.INST_ID || ': ' ||
                    TO_CHAR(startup_time, 'YYYY-MM-DD HH24:MI:SS') || '] ')
         else
          WMSYS.WM_CONCAT(TO_CHAR(startup_time, 'YYYY-MM-DD HH24:MI:SS')) || '  '
       end AS startup_time
  FROM gv$instance d, v$parameter np
 WHERE np.NAME = 'cluster_database'
 GROUP BY np.value;

-- Instance Information
COLUMN dbname1 NEW_VALUE _dbname1 NOPRINT
COLUMN dbid NEW_VALUE _dbid NOPRINT
COLUMN dbname NEW_VALUE _dbname NOPRINT
COLUMN reporttitle NEW_VALUE _reporttitle NOPRINT
COLUMN platform_name NEW_VALUE _platform_name NOPRINT
COLUMN FORCE_LOGGING NEW_VALUE _FORCE_LOGGING NOPRINT
COLUMN FLASHBACK_ON NEW_VALUE _FLASHBACK_ON NOPRINT
COLUMN platform_id NEW_VALUE _platform_id NOPRINT
COLUMN creation_date NEW_VALUE _creation_date NOPRINT
COLUMN log_mode NEW_VALUE _log_mode NOPRINT
COLUMN DB_ROLE NEW_VALUE _DB_ROLE NOPRINT
COLUMN OPEN_MODE NEW_VALUE _OPEN_MODE NOPRINT
SELECT DECODE((SELECT b.parallel FROM v$instance b), 'YES', (d.NAME || '_' ||  (SELECT b.INSTANCE_NUMBER FROM v$instance b)), 'NO', d.NAME)   dbname1,  
       name dbname,
       trim(dbid) dbid,
       trim('dbcheck_' || dbid || '_' || name || '_'  || (SELECT b.VERSION FROM v$instance b) || '_' ||TO_CHAR(SYSDATE, 'YYYYMMDD')) reporttitle,
       trim(platform_name) platform_name,
       d.FORCE_LOGGING,
       d.FLASHBACK_ON,
       trim(platform_id) platform_id ,
       TO_CHAR(CREATED, 'YYYY-MM-DD HH24:MI:SS') creation_date ,
       (case when log_mode ='NOARCHIVELOG' then log_mode else log_mode||','||(SELECT a.DESTINATION FROM v$archive_dest a where a.DESTINATION IS NOT NULL  and rownum<=1) end) log_mode,
       D.DATABASE_ROLE   DB_ROLE,
       D.OPEN_MODE       OPEN_MODE
  FROM  v$database d;

-- CPU NUM
COLUMN cpus NEW_VALUE _cpus NOPRINT
SELECT TO_NUMBER(value) cpus FROM v$parameter WHERE name = 'cpu_count';
select distinct DECODE(o.stat_name, 'NUM_CPUS', o.value) cpus from dba_hist_osstat o WHERE o.stat_name = 'NUM_CPUS';

-- AWR SNAP MIN
COLUMN snap_int_min NEW_VALUE _snap_int_min NOPRINT
SELECT 60 snap_int_min FROM dual;
select extract(hour FROM snap_interval) * 60 + extract(minute FROM snap_interval) snap_int_min from dba_hist_wr_control WHERE dbid = &_dbid;

-- Host Information
COLUMN hostinfo NEW_VALUE _hostinfo NOPRINT
SELECT WMSYS.WM_CONCAT(hostinfo) hostinfo
  FROM (SELECT CASE (SELECT b.parallel FROM v$instance b)
                 WHEN 'NO' then
                  '  CPUs:' || SUM(CPUs) || '  Cores:' || SUM(Cores) ||
                  '  Sockets:' || SUM(Sockets) || '  Memory:' || SUM(Memory) || 'G'
                 WHEN 'YES' then
                  '[' || 'INST_ID ' || instance_number || ':  CPUs:' ||
                  SUM(CPUs) || '  Cores:' || SUM(Cores) || '  Sockets:' ||
                  SUM(Sockets) || '  Memory:' || SUM(Memory) || 'G]'
               end hostinfo
          FROM (SELECT o.snap_id,
                       o.dbid,
                       o.instance_number,
                       DECODE(o.stat_name, 'NUM_CPUS', o.value) CPUs,
                       DECODE(o.stat_name, 'NUM_CPU_CORES', o.value) Cores,
                       DECODE(o.stat_name, 'NUM_CPU_SOCKETS', o.value) Sockets,
                       DECODE(o.stat_name,
                              'PHYSICAL_MEMORY_BYTES',
                              trunc(o.value / 1024 / 1024 / 1024, 2)) Memory
                  FROM dba_hist_osstat o
                 WHERE o.stat_name IN
                       ('NUM_CPUS',
                        'NUM_CPU_CORES',
                        'NUM_CPU_SOCKETS',
                        'PHYSICAL_MEMORY_BYTES'))
         WHERE (instance_number, snap_id) in
               (SELECT t.instance_number, max(t.snap_id) snap_id
                  FROM DBA_HIST_SNAPSHOT t
                 GROUP BY t.instance_number)
         GROUP BY instance_number);

-- Global name
COLUMN global_name NEW_VALUE _global_name NOPRINT
SELECT global_name global_name FROM global_name;

-- block size
COLUMN blocksize NEW_VALUE _blocksize NOPRINT
SELECT value blocksize FROM v$parameter WHERE name='db_block_size';

-- timezone
COLUMN timezone NEW_VALUE _timezone NOPRINT
SELECT trim(d.version) timezone FROM v$timezone_file d;

-- characterset
COLUMN characterset NEW_VALUE _characterset NOPRINT
SELECT (SELECT value$ FROM sys.props$ WHERE name='NLS_CHARACTERSET') || '.' || (SELECT value$ characterset FROM sys.props$ WHERE name='NLS_NCHAR_CHARACTERSET') characterset FROM DUAL;

-- language
COLUMN  nls_language NEW_VALUE _nls_language NOPRINT 
SELECT d.VALUE nls_language FROM v$parameter d WHERE d.NAME='nls_language';

-- archive log mode
COLUMN ARCH NEW_VALUE _ARCH NOPRINT
SELECT log_mode ARCH FROM v$database;

-- rac or not
COLUMN RAC NEW_VALUE _RAC NOPRINT
COLUMN cluster_database NEW_VALUE _cluster_database NOPRINT
SELECT 'NO' RAC, 'FALSE' cluster_database FROM dual;
SELECT decode(value,'TRUE','YES','NO') RAC FROM v$parameter WHERE name='cluster_database';
SELECT value cluster_database FROM v$parameter WHERE name='cluster_database';

-- RAC nodes
COLUMN cluster_database_instances NEW_VALUE _cluster_database_instances NOPRINT
SELECT value cluster_database_instances FROM v$parameter WHERE name='cluster_database_instances';

COLUMN rac_database NEW_VALUE _rac_database NOPRINT 
SELECT (SELECT value cluster_database
          FROM v$parameter
         WHERE name = 'cluster_database') || ' : ' ||
       (SELECT value cluster_database_instances
          FROM v$parameter
         WHERE name = 'cluster_database_instances') rac_database
  FROM DUAL;

-- snap id, last hour
COLUMN snap_beg NEW_VALUE _snap_beg NOPRINT 
COLUMN snap_end NEW_VALUE _snap_end NOPRINT
SELECT 1 snap_beg, 2 snap_end FROM dual;
SELECT snap_beg,snap_end
  FROM (SELECT d.snap_id snap_beg, 
               lead(d.snap_id) over(partition by d.startup_time ORDER BY snap_id) snap_end
          FROM dba_hist_snapshot d,v$instance nd
         WHERE d.instance_number = nd.INSTANCE_NUMBER  
         ORDER BY d.snap_id desc) t 
 WHERE snap_end IS NOT NULL
   AND  ROWNUM = 1;

-- snap id, last 7 days
COLUMN crt_snap_beg NEW_VALUE _crt_snap_beg NOPRINT 
COLUMN crt_snap_end NEW_VALUE _crt_snap_end NOPRINT
SELECT 0 crt_snap_beg, 0 crt_snap_end FROM dual;
SELECT NVL(MIN(b.snap_id), 0) crt_snap_beg,
       NVL(MAX(b.snap_id), 0) crt_snap_end
  FROM dba_hist_snapshot b
 WHERE b.begin_interval_time >= TRUNC(SYSDATE) - 7
   AND b.dbid = &_dbid
   AND b.instance_number IN (SELECT instance_number FROM gv$instance)
   AND b.startup_time >= (SELECT MAX(i.startup_time)
                            FROM gv$instance i
                           WHERE i.instance_number = b.instance_number);

-- dbcheck session information
COLUMN v_SID NEW_VALUE _v_SID NOPRINT
COLUMN v_SERIAL# NEW_VALUE _v_SERIAL NOPRINT
COLUMN v_SPID NEW_VALUE _v_SPID NOPRINT
COLUMN v_sessionid NEW_VALUE _v_sessionid NOPRINT
SELECT a.SID v_SID,
       b.SERIAL# v_SERIAL#,
       c.SPID v_SPID,
       'INST_ID:'||b.INST_ID||',['||a.SID||','||b.SERIAL# ||','||c.SPID||']' v_sessionid  
FROM   v$mystat  a,
       gv$session b,
       v$process c
WHERE a.SID = b.SID
and b.PADDR=c.ADDR
AND ROWNUM = 1;

-- DataGuard or not
COLUMN DG NEW_VALUE _DG NOPRINT
COLUMN DGINFO NEW_VALUE _DGINFO NOPRINT
SELECT case
         WHEN d.VALUE is null then
          'NO'
         else
          decode(d.value,'NODG_CONFIG','NO','YES')
       end DG
  FROM v$parameter d
 WHERE d.NAME = 'log_archive_config';
SELECT case
         WHEN d.VALUE is null then
          'NO'
         else
          decode(d.value,'NODG_CONFIG','NO','YES,'|| d.value)
       end DGINFO
  FROM v$parameter d
 WHERE d.NAME = 'log_archive_config';

-- GoldenGate or not
COLUMN GGS_GGSUSER_ROLE NEW_VALUE _GGS_GGSUSER_ROLE NOPRINT
SELECT 'NULL' GGS_GGSUSER_ROLE FROM dual;
SELECT case
         WHEN SUM(count_gg) > 0 then
          'YES'
         ELSE
          'NO'
       END AS GGS_GGSUSER_ROLE
  FROM (SELECT count(D.ROLE) count_gg
          FROM dba_roles d
         WHERE d.ROLE = 'GGS_GGSUSER_ROLE'
        UNION ALL 
        SELECT count(*)
          FROM dba_users d
         WHERE d.username = 'GOLDENGATE');

-- SharePlex or not 
COLUMN SPLEXUSER_ROLE NEW_VALUE _SPLEXUSER_ROLE NOPRINT
SELECT 'NULL' SPLEXUSER_ROLE FROM dual;
SELECT case
         WHEN SUM(count_splex) > 0 then
          'YES'
         ELSE
          'NO'
       END AS SPLEXUSER_ROLE
  FROM (SELECT count(D.ROLE) count_splex
          FROM dba_roles d
         WHERE upper(d.ROLE) like 'SPLEX%'
        UNION ALL 
        SELECT count(*)
          FROM dba_users d
         WHERE upper(d.username) like 'SPLEX%');

-- Maxgauge or not
COLUMN MXGUSER_ROLE NEW_VALUE _MXGUSER_ROLE NOPRINT
SELECT 'NULL' MXGUSER_ROLE FROM dual;
SELECT case
         WHEN SUM(count_mxg) > 0 then
          'YES'
         ELSE
          'NO'
       END AS MXGUSER_ROLE
  FROM (SELECT count(D.ROLE) count_mxg
          FROM dba_roles d
         WHERE upper(d.ROLE) like 'MAXGAUGE%'
        UNION ALL 
        SELECT count(*)
          FROM dba_users d
         WHERE upper(d.username) like 'MAXGAUGE%');

-- Recyclebin Information
COLUMN recyclebin NEW_VALUE _recyclebin NOPRINT
SELECT '''NULL''' recyclebin FROM dual;
SELECT 'Status: ' || a.VALUE || ', Used_Size: ' ||
       (SELECT round(SUM(a.space * (SELECT value
                                      FROM v$parameter
                                     WHERE name = 'db_block_size')) / 1024 / 1024,
                     2) || 'M, Total_Count: ' || count(1) || ''
          FROM dba_recyclebin a) recyclebin
  FROM v$parameter a
 WHERE a.NAME = 'recyclebin';

-- iv for awrcrt
COLUMN IV NEW_VALUE _IV NOPRINT
SELECT 3600 IV FROM dual;
SELECT trim(trunc(3600*24*(sysdate+snaP_interval-sysdate))) IV
   FROM dba_hist_wr_control 
  WHERE dbid = &_dbid;

-- Database file Information
COLUMN TBS_CNT NEW_VALUE _TBS_CNT NOPRINT
COLUMN CTRL_CNT NEW_VALUE _CTRL_CNT NOPRINT
COLUMN REDO_CNT NEW_VALUE _REDO_CNT NOPRINT
COLUMN STD_CNT NEW_VALUE _STD_CNT NOPRINT
COLUMN USER_CNT NEW_VALUE _USER_CNT NOPRINT
COLUMN REDO_SIZE NEW_VALUE _REDO_SIZE NOPRINT
COLUMN REDO_GENRATE NEW_VALUE _REDO_GENRATE NOPRINT
SELECT trim(count(*)) TBS_CNT FROM v$tablespace;
SELECT trim(count(*)) CTRL_CNT FROM v$controlfile;
SELECT trim(count(*)) REDO_CNT FROM v$logfile WHERE type = 'ONLINE';
SELECT trim(count(*)) STD_CNT FROM v$logfile WHERE type = 'STANDBY';
SELECT '[Max Redo Size: ' || max(bytes/1024/1024) || ' M], [Min Redo Size: ' || min(bytes/1024/1024) || ' M]' REDO_SIZE FROM v$log;
SELECT round(sum(blocks*block_size)/1024/1024/168,2) || ' M/h' REDO_GENRATE FROM v$archived_log WHERE first_time > sysdate -7 AND dest_id=1;
SELECT trim(count(*)) USER_CNT from dba_users where default_tablespace not in ('SYSTEM','SYSAUX') and account_status = 'OPEN';

-- Database Parameters
COLUMN SPFILE NEW_VALUE _SPFILE NOPRINT
COLUMN ISSPFILE NEW_VALUE _ISSPFILE NOPRINT
COLUMN OMF NEW_VALUE _OMF NOPRINT
COLUMN DBFILES NEW_VALUE _DBFILES NOPRINT
COLUMN SGA_MAX NEW_VALUE _SGA_MAX NOPRINT
COLUMN SGA_TARGET NEW_VALUE _SGA_TARGET NOPRINT
COLUMN PGA_TARGET NEW_VALUE _PGA_TARGET NOPRINT
SELECT case
         WHEN d.VALUE IS NOT NULL then
          'This database IS using an SPFILE'
         else
          'This database IS NOT using an SPFILE'
       end AS ISSPFILE
  FROM v$parameter d
 WHERE d.NAME = 'spfile';
SELECT DECODE(value,null,'NO','YES, ' || value) SPFILE FROM v$parameter WHERE name = 'spfile';
SELECT round(value/1024/1024/1024,2) || 'G' SGA_MAX FROM v$parameter WHERE name = 'sga_max_size';
SELECT round(value/1024/1024/1024,2) || 'G' SGA_TARGET FROM v$parameter WHERE name = 'sga_target';
SELECT round(value/1024/1024/1024,2) || 'G' PGA_TARGET FROM v$parameter WHERE name = 'pga_aggregate_target';
SELECT DECODE(value,null,'NO','YES, ' || value) OMF FROM v$parameter WHERE name = 'db_create_file_dest';
SELECT 'DB Files Count: ' || ((SELECT count(*) FROM dba_data_files) + (SELECT count(*) FROM dba_temp_files)) || ', DB Files Limit: ' || value DBFILES FROM v$parameter WHERE name = 'db_files';

--------------------------------------------------------------------------------------------------------------------------------------------------------
--------------------------------------------------------------------------------------------------------------------------------------------------------

-- +----------------------------------------------------------------------------+
-- |                   GATHER DATABASE REPORT INFORMATION                       |
-- +----------------------------------------------------------------------------+

set heading on
set markup html on spool on preformat off entmap on -
head ' -
  <title>&_dbname DBCheck Report</title> -
  <style type="text/css"> -
    body p            {font:11px Consolas; color:black; background:White;} -
    table             {font:11px Consolas; color:Black; background:#FFFFCC; padding:1px; margin:0px 0px 0px 0px;} -
    tr:nth-child(odd) {background:White;} -
    tr:hover          {background-color: yellow;} -
    th                {font:bold 11px Consolas; color:White; background:#0066cc; padding:5px;white-space: nowrap;} -
    a a.link          {font:11px Consolas; color:#663300; margin-top:0pt; margin-bottom:0pt; vertical-align:middle;padding:4;} -
    a.noLink          {font:11px Consolas; color:#663300; text-decoration: underline; margin-top:0pt; margin-bottom:0pt; vertical-align:middle;padding:4;} -
    a.info:hover      {background:#eee;color:#000000; position:relative;} -
    a.info span       {display: none; } -
    a.info:hover span {font-size:11px!important; color:#000000; display:block;position:absolute;top:30px;left:40px;width:150px;border:1px solid red; background:#FFFF00; padding:1px 1px;text-align:left;word-wrap: break-word; white-space: pre-wrap} -
  </style> -
  <script src="crt21.js"></script>'

SET MARKUP html TABLE  'border="1" summary="Script output" cellspacing="0px" style="border-collapse:collapse;" '
spool &_reporttitle..html;
set markup html on ENTMAP OFF

 
-- +----------------------------------------------------------------------------+
-- +----------------------------------------------------------------------------+
-- |                             - REPORT HEADER -                              |
-- +----------------------------------------------------------------------------+

define reportHeader="<center><font size=+3 color=darkgreen><b>&_dbname DBCheck Report</b></font></center>"

prompt <a name=top></a>
prompt &reportHeader
prompt <hr>
prompt <a style="font-weight:lighter">Check Date: <span id="checkdate">&_sdate</span></a>
prompt <a style="font-weight:lighter">Database NAME: <span id="dbname">&_dbname</span></a>
prompt <span id="dbid" hidden>&_dbid</span><span id="rac" hidden>&_RAC</span><span id="dg" hidden>&_DG</span><span id="arch" hidden>&_ARCH</span><span id="isspfile" hidden>&_ISSPFILE</span><span id="openmode" hidden>&_OPEN_MODE</span>
prompt [<a class="noLink" href="#html_bottom_link"><b>Switch to Bottom</b></a>]<hr>

prompt <a name="directory"><font size=+2 face="Consolas" color="#336699"><b>Directory</b></font></a><hr>
prompt <table width="100%" border="1" width="90%" align="center" summary="Script output" > -
<tr><th colspan="5"><a class="info" href="#10"><font size=+0.5 face="Consolas" color="#ffffff"><b>Database Information</b></font></a></th></tr> -
<tr style="background:#FFFFCC;"> -
<td nowrap align="center" width="18%"><a class="info" href="#instance_info">       <font size=+0.5 face="Consolas" color="#336699">  Instance Info       </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#database_info">       <font size=+0.5 face="Consolas" color="#336699">  Database Info       </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#database_version">    <font size=+0.5 face="Consolas" color="#336699">  Database Version    </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#database_component">  <font size=+0.5 face="Consolas" color="#336699">  Database Component  </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#database_patch">      <font size=+0.5 face="Consolas" color="#336699">  Database Patch      </font></a></td> -
</tr> -
<tr style="background:#FFFFCC;"> -      
<td nowrap align="center" width="18%"><a class="info" href="#database_parameter">  <font size=+0.5 face="Consolas" color="#336699">  Database Parameter                </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#resource_limit">      <font size=+0.5 face="Consolas" color="#336699">  Resource Limit                    </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#control_file">        <font size=+0.5 face="Consolas" color="#336699">  Control File                      </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#redolog_file">        <font size=+0.5 face="Consolas" color="#336699">  Log File                          </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#archivelog_size">     <font size=+0.5 face="Consolas" color="#336699">  Archive Log Size in last 10 Days  </font></a></td> -
</tr>
prompt <tr style="background:#FFFFCC;"> -         
<td nowrap align="center" width="18%"><a class="info" href="#invalid_object">      <font size=+0.5 face="Consolas" color="#336699">  Invalid Object              </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#tablespace_usage">    <font size=+0.5 face="Consolas" color="#336699">  Tablespace Usage            </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#top10_table">         <font size=+0.5 face="Consolas" color="#336699">  Top 10 Table                </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#top10_index">         <font size=+0.5 face="Consolas" color="#336699">  Top 10 Index                </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#foreigno_index">      <font size=+0.5 face="Consolas" color="#336699">  Foreign keys without index  </font></a></td> -
</tr> -
<tr style="background:#FFFFCC;"> -         
<td nowrap align="center" width="18%"><a class="info" href="#object_insystem">     <font size=+0.5 face="Consolas" color="#336699">  Object in System TableSpace </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#bitcoin_check">       <font size=+0.5 face="Consolas" color="#336699">  BitCoin Attack Check        </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#sysaux_obj">          <font size=+0.5 face="Consolas" color="#336699">  SYSAUX Object Check         </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#hwmstat_info">        <font size=+0.5 face="Consolas" color="#336699">  High Water Mark Statistics  </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#flashback_parameters">        <font size=+0.5 face="Consolas" color="#336699">  Flashback Database Parameters  </font></a></td> -
</tr> -
<tr style="background:#FFFFCC;"> -         
<td nowrap align="center" width="18%"><a class="info" href="#flashback_status">     <font size=+0.5 face="Consolas" color="#336699">  Flashback Database Status </font></a></td> -
</tr> -
</table>

prompt <table width="100%" border="1" width="90%" align="center" summary="Script output" > -
<tr><th colspan="5"><a class="info" href="#20"><font size=+0.5 face="Consolas" color="#ffffff"><b>Schema Information</b></font></a></th></tr> -
<tr style="background:#FFFFCC;"> -
<td nowrap align="center" width="18%"><a class="info" href="#userdefault_passwd">  <font size=+0.5 face="Consolas" color="#336699">  User with Default Password  </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#system_role">         <font size=+0.5 face="Consolas" color="#336699">  System Manager Role         </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#schema_info">         <font size=+0.5 face="Consolas" color="#336699">  Schema Info                 </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#profile">             <font size=+0.5 face="Consolas" color="#336699">  Profile                     </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#directory">           <font size=+0.5 face="Consolas" color="#336699">  Directory                   </font></a></td> -
</tr> -
<tr style="background:#FFFFCC;"> -      
<td nowrap align="center" width="18%"><a class="info" href="#job">                 <font size=+0.5 face="Consolas" color="#336699">  Job                    </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#scheduler_jobs">      <font size=+0.5 face="Consolas" color="#336699">  DBA Scheduler Jobs     </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#jobs_info_errors">    <font size=+0.5 face="Consolas" color="#336699">  Job Error Information  </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#dblink">              <font size=+0.5 face="Consolas" color="#336699">  Database Link          </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#autotask">            <font size=+0.5 face="Consolas" color="#336699">  Autotask               </font></a></td> -
</tr> -
</table>

prompt <table width="100%" border="1" width="90%" align="center" summary="Script output" > -
<tr><th colspan="5"><a class="info" href="#30"><font size=+0.5 face="Consolas" color="#ffffff"><b>Backup and DataGuard</b></font></a></th></tr> -
<tr style="background:#FFFFCC;"> -
<td nowrap align="center" width="18%"><a class="info" href="#dg_para">          <font size=+0.5 face="Consolas" color="#336699">  DG Parameter            </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#dgapply_info">     <font size=+0.5 face="Consolas" color="#336699">  DG Apllied Status       </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#dg_stats">         <font size=+0.5 face="Consolas" color="#336699">  DG Status               </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#rmanbackup_info">  <font size=+0.5 face="Consolas" color="#336699">  RMAN Backup Info        </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#datapump_info">    <font size=+0.5 face="Consolas" color="#336699">  Orphaned DataPump Jobs  </font></a></td> -
</tr> -
</table>

prompt <table width="100%" border="1" width="90%" align="center" summary="Script output" > -
<tr><th colspan="5"><a class="info" href="#40"><font size=+0.5 face="Consolas" color="#ffffff"><b>ASM Information</b></font></a></th></tr> -
<tr style="background:#FFFFCC;"> -
<td nowrap align="center" width="18%"><a class="info" href="#asmdiskgroup_usage">  <font size=+0.5 face="Consolas" color="#336699">  ASM Disk Group                </font></a></td> -
</tr> -
</table>

prompt <table width="100%" border="1" width="90%" align="center" summary="Script output" > -
<tr><th colspan="5"><a class="info" href="#50"><font size=+0.5 face="Consolas" color="#ffffff"><b>Performance</b></font></a></th></tr> -
<tr style="background:#FFFFCC;"> -
<td nowrap align="center" width="18%"><a class="info" href="#awraux_check">  <font size=+0.5 face="Consolas" color="#336699">  Awrsnap Info                                   </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#load_profile">  <font size=+0.5 face="Consolas" color="#336699">  Load Profile Per Sec                           </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#insteffi">      <font size=+0.5 face="Consolas" color="#336699">  Instance Efficiency Percentages (Target 100%)  </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#top10_event">   <font size=+0.5 face="Consolas" color="#336699">  TOP 10 Wait Event                              </font></a></td> -
</tr> -
<tr style="background:#FFFFCC;"> -      
<td nowrap align="center" width="18%"><a class="info" href="#timemodel">  <font size=+0.5 face="Consolas" color="#336699">  System Time Model                 </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#top10_sql">  <font size=+0.5 face="Consolas" color="#336699">  TOP 10 SQL Order by Elapsed Time  </font></a></td> -
<td nowrap align="center" width="18%"><a class="info" href="#awrcrt">     <font size=+0.5 face="Consolas" color="#336699">  Awrcrt Info                       </font></a></td> -
</tr> -
</table>

prompt <hr>

-- +====================================================================================================================+
-- |
-- | <<<<<     Overview of Database Informaion     >>>>>                                         |
-- |                                                                                                                    |
-- +====================================================================================================================+

host echo start collect...Database Informaion... 

prompt <a name="10"></a>
prompt <font size="+2" face="Consolas" color="#336699"><b>Database Information</b></font><hr>

-- +----------------------------------------------------------------------------+
-- |                           - DATABASE OVERVIEW -                            |
-- +----------------------------------------------------------------------------+

host echo start collect......Overview of Instance Informaion... 

prompt <a name="instance_info"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Instance Info</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="instinfo" border="1" width="90%" align="center" summary="Script output" '

COLUMN inst_id        HEADING  'ID'
COLUMN host_name      HEADING  'Host Name'
COLUMN instance_name  HEADING  'Instance Name'
COLUMN version        HEADING  'Version'
COLUMN startup_time   HEADING  'Startup Time'
COLUMN updays         HEADING  'UP Time(Days)'
COLUMN rac            HEADING  'RAC?'
COLUMN status         HEADING  'Status'
COLUMN archiver       HEADING  'Archive Mode'

SELECT '<b>' || instance_number || '</b>' inst_id,
       '<font color="#843900"><b>' || host_name || '</b></font>' host_name,
       '<font color="#336699"><b>' || instance_name || '</b></font>' instance_name,
       version,
       to_char(startup_time,
               'yyyy-mm-dd HH24:MI:SS') startup_time,
       round(to_char(SYSDATE - startup_time),
             2) updays,
       parallel rac,
       status,
       decode(archiver,
              'FAILED',
              '<font color="#990000"><b>' || archiver || '</b></font>',
              '<font color="darkgreen"><b>' || archiver || '</b></font>') archiver
  FROM gv$instance
 ORDER BY instance_number;

host echo start collect......Overview of Database Informaion... 

prompt <a name="database_info"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Base Info</b></font><br>

CLEAR COLUMNS COMPUTES
SET DEFINE ON

prompt <TABLE id="baseinfo" border="1" width="90%" align="center" summary="Script output" > -
<tr><td style="color:White; background:#0066cc;" width="200"><b>DBCheck Date</b></td><td>&_date_time_timezone</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>DBCheck User</b></td><td>&_v_current_user</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>DBCheck Session</b></td><td>&_v_sessionid</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Host Name</b></td><td>&_host_name_all</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>CPU/Memory Info</b></td><td>&_hostinfo</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>OS Version</b></td><td>&_platform_name / &_platform_id</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>DB Name</b></td><td>&_dbname</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Global Name</b></td><td>&_global_name</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Instance Name</b></td><td>&_instance_name_all</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Database Role</b></td><td>&_DB_ROLE</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Open Mode</b></td><td>&_OPEN_MODE</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Database Version</b></td><td>&_dbversion</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Database ID</b></td><td>&_dbid</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>RAC Nodes</b></td><td>&_rac_database</td></tr> - 
<tr><td style="color:White; background:#0066cc;" width="200"><b>Database Create Time</b></td><td>&_creation_date</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Database Start Time</b></td><td>&_startup_time</td></tr> -
</table>
prompt <table id="fileinfo" border="1" width="90%" align="center" summary="Script output" > -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Users</b></td><td>&_USER_CNT</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Tablespace Count</b></td><td>&_TBS_CNT</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Data Files Count</b></td><td>&_DBFILES</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Control Files Count</b></td><td>&_CTRL_CNT</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Redo Files Count</b></td><td>&_REDO_CNT</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Standby Files Count</b></td><td>&_STD_CNT</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Redo Files Size</b></td><td>&_REDO_SIZE</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Redo Generation Rate</b></td><td>&_REDO_GENRATE</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>RECYCLEBIN</b></td><td>&_recyclebin</td></tr> -
</table>
prompt <table id="dbinfo" border="1" width="90%" align="center" summary="Script output" > -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Block Size</b></td><td>&_blocksize</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>SGA Max Size</b></td><td>&_SGA_MAX</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>SGA Target</b></td><td>&_SGA_TARGET</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>PGA Target</b></td><td>&_PGA_TARGET</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Database Characterset</b></td><td>&_characterset</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Time Zone</b></td><td>&_timezone</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Using SPFILE?</b></td><td>&_SPFILE</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Using OMF?</b></td><td>&_OMF</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Archivelog?</b></td><td>&_log_mode</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Flashback?</b></td><td>&_FLASHBACK_ON</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Force Logging?</b></td><td>&_FORCE_LOGGING</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>DataGuard?</b></td><td>&_DGINFO</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>GoldenGate?</b></td><td>&_GGS_GGSUSER_ROLE</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>SharePlex?</b></td><td>&_SPLEXUSER_ROLE</td></tr> -
<tr><td style="color:White; background:#0066cc;" width="200"><b>Maxgauge?</b></td><td>&_MXGUSER_ROLE</td></tr> -
</table>

host echo start collect......Database Version Informaion... 

prompt <a name="database_version"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Database Version</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="dbversion" border="1" width="90%" align="center" summary="Script output" '

COLUMN  banner  FORMAT  A100  HEADING  'BANNER'

SELECT banner
  FROM v$version;

host echo start collect......Database Component and Patch Informaion...
 
prompt <a name="database_component"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Database Component</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="component" border="1" width="90%" align="center" summary="Script output" '

COLUMN  comp_id    HEADING  'Comp ID'
COLUMN  comp_name  HEADING  'Comp Name'
COLUMN  version    HEADING  'Version'
COLUMN  status     HEADING  'Status'
COLUMN  modified   HEADING  'Modified'

SELECT comp_id,
       comp_name,
       version,
       decode(status,
              'VALID',
              '<font color="green"><b>' || status || '</b></font>',
              '<font color="red"><b>' || status || '</b></font>') status,
       to_char(to_date(modified,
                       'DD-Mon-YYYY HH24:MI:SS'),
               'YYYY-MM-DD HH24:MI:SS') modified
  FROM dba_registry
 ORDER BY 5 DESC;

prompt <a name="database_patch"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Database Patch</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="patchinfo" border="1" width="90%" align="center" summary="Script output" '

COLUMN  id            HEADING  'ID'
COLUMN  comments      HEADING  'Comments'
COLUMN  version       HEADING  'Version'
COLUMN  action        HEADING  'Action'
COLUMN  action_time   HEADING  'Action Time'

SELECT id,
       comments,
       version,
       action,
       action_time
  FROM dba_registry_history
 ORDER BY action_time;

host echo start collect......Database Parameter Informaion... 

prompt <a name="database_parameter"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Database Parameters</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="dbpara" border="1" width="90%" align="center" summary="Script output" '

COLUMN  id             HEADING  'Para Name'
COLUMN  current_value  HEADING  'Current Value'
COLUMN  advise         HEADING  'Advise'

SELECT v.sid || '.' || v.name name,
       CASE WHEN v.name = 'processes' AND v.value < 500 THEN '<font color="red"><b>' || v.value || '</b></font>' 
        WHEN v.name = 'sessions' AND v.value < 500 THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'db_files' AND v.value < 500 THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'spfile' AND v.value IS NULL THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'optimizer_adaptive_features' AND decode(VALUE,'TRUE','NO','YES') = 'NO' THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'optimizer_adaptive_plans' AND decode(VALUE,'TRUE','NO','YES') = 'NO' THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'optimizer_adaptive_statistics' AND decode(VALUE,'TRUE','NO','YES') = 'NO' THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = '_optimizer_use_feedback' AND decode(VALUE,'TRUE','NO','YES') = 'NO' THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'max_string_size' AND decode(upper(VALUE),'STANDARD','NO','YES') = 'NO' THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'statistics_level' AND decode(upper(VALUE),'TYPICAL','YES','NO') = 'NO' THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'control_file_record_keep_time' AND v.value < 10 THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = '_use_adaptive_log_file_sync' AND decode(VALUE,'FALSE','YES','NO') = 'NO' THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'fast_start_parallel_rollback' AND decode(VALUE,'HIGH','NO','YES') = 'NO' THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = '_datafile_write_errors_crash_instance' AND decode(VALUE,'FALSE','YES','NO') = 'NO' THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'max_dump_file_size' AND decode(VALUE,'unlimited','NO','YES') = 'NO' THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'parallel_max_servers' AND v.value > (SELECT VALUE * 4 FROM v$parameter WHERE NAME = 'cpu_count') THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'deferred_segment_creation' AND decode(upper(VALUE),'TRUE','NO','YES') = 'NO' THEN '<font color="red"><b>' || v.value || '</b></font>' 
      WHEN v.name = 'open_cursors' AND v.value * 0.8 < (SELECT MAX(SUM(s.value)) FROM v$statname n, v$sesstat s WHERE n.name IN ('opened cursors current','session cursor cache count') AND s.statistic# = n.statistic# GROUP BY s.sid) THEN '<font color="red"><b>' || v.value || '</b></font>' 
        WHEN v.name = 'session_cached_cursors' AND v.value * 0.8 < (SELECT MAX(SUM(s.value)) FROM v$statname n,v$sesstat s WHERE n.name = 'session cursor cache count' AND s.statistic# = n.statistic# GROUP BY s.sid) THEN '<font color="red"><b>' || v.value || '</b></font>'
       ELSE
          VALUE
       END current_value,
       CASE WHEN v.name = 'processes' AND v.value < 500 THEN 'Processes is less than 500.' 
      WHEN v.name = 'sessions' AND v.value < 500 THEN 'Sessions is less than 500.' 
      WHEN v.name = 'db_files' AND v.value < 500 THEN 'Db_files is less than 500.' 
      WHEN v.name = 'spfile' AND v.value IS NULL THEN 'Instance Not Starup from SPFILE.' 
      WHEN v.name = 'optimizer_adaptive_features' AND decode(VALUE,'TRUE','NO','YES') = 'NO' THEN 'Optimizer_adaptive is Enabled,Plan may be Change.' 
      WHEN v.name = 'optimizer_adaptive_plans' AND decode(VALUE,'TRUE','NO','YES') = 'NO' THEN 'optimizer_adaptive_plans is Enabled,Plan may be Change.' 
      WHEN v.name = 'optimizer_adaptive_statistics' AND decode(VALUE,'TRUE','NO','YES') = 'NO' THEN 'optimizer_adaptive_statistics is Enabled,Plan may be Change.' 
      WHEN v.name = '_optimizer_use_feedback' AND decode(VALUE,'TRUE','NO','YES') = 'NO' THEN 'Optimizer Feedback is Enabled,Plan may be Change.' 
      WHEN v.name = 'max_string_size' AND decode(upper(VALUE),'STANDARD','NO','YES') = 'NO' THEN 'VARCHAR2 Maximum Not Support 32k.' 
      WHEN v.name = 'statistics_level' AND decode(upper(VALUE),'TYPICAL','YES','NO') = 'NO' THEN 'Statistics Level is not Typical.' 
      WHEN v.name = 'control_file_record_keep_time' AND v.value < 10 THEN 'Controlfile Record Maybe to Small.' 
      WHEN v.name = '_use_adaptive_log_file_sync' AND decode(VALUE,'FALSE','YES','NO') = 'NO' THEN 'OLTP Should not used Adaptive Log File Sync.' 
      WHEN v.name = 'fast_start_parallel_rollback' AND decode(VALUE,'HIGH','NO','YES') = 'NO' THEN 'Parallel Rollback is HIGH.' 
      WHEN v.name = '_datafile_write_errors_crash_instance' AND decode(VALUE,'FALSE','YES','NO') = 'NO' THEN 'Instance May Crash when Datafile Write Failed.' 
      WHEN v.name = 'max_dump_file_size' AND decode(VALUE,'unlimited','NO','YES') = 'NO' THEN 'Max Size of Tracefile is Unlimited.' 
      WHEN v.name = 'parallel_max_servers' AND v.value > (SELECT VALUE * 4 FROM v$parameter WHERE NAME = 'cpu_count') THEN 'Max Parallel Processed is seting too High.' 
      WHEN v.name = 'deferred_segment_creation' AND decode(upper(VALUE),'TRUE','NO','YES') = 'NO' THEN 'Deferred Segment Creation Not Disable, EXPDP may report Error.' 
      WHEN v.name = 'open_cursors' AND v.value * 0.8 < (SELECT MAX(SUM(s.value)) FROM v$statname n,v$sesstat s WHERE n.name IN ('opened cursors current','session cursor cache count') AND s.statistic# = n.statistic# GROUP BY s.sid) THEN 'Open Cursor is over 80% Full.' 
      WHEN v.name = 'session_cached_cursors' AND v.value * 0.8 < (SELECT MAX(SUM(s.value)) FROM v$statname n,v$sesstat s WHERE n.name = 'session cursor cache count' AND s.statistic# = n.statistic# GROUP BY s.sid) THEN 'session_cached_cursors Sholud Be inc.'
       END advise
  FROM (SELECT DISTINCT s.name,
                        s.sid,
                        s.value spvalue,
                        p.value VALUE
          FROM v$spparameter s,
               gv$parameter  p
         WHERE s.name = p.name
           AND (s.value IS NOT NULL OR (p.name IN ('statistics_level',
                                                   'processes',
                                                   'sessions',
                                                   'db_files',
                                                   'spfile',
                                                   'optimizer_adaptive_features',
                                                   'optimizer_adaptive_plans',
                                                   'optimizer_adaptive_statistics',
                                                   'max_string_size',
                                                   'control_file_record_keep_time',
                                                   '_use_adaptive_log_file_sync',
                                                   'fast_start_parallel_rollback',
                                                   '_datafile_write_errors_crash_instance',
                                                   'max_dump_file_size',
                                                   'parallel_max_servers',
                                                   'deferred_segment_creation',
                                                   '_optimizer_use_feedback',
                                                   'open_cursors',
                                                   'session_cached_cursors',
                                                   'OPTIMIZER_INDEX_COST_ADJ',
                                                   'optimizer_index_caching',
                                                   'audit_trail',
                                                   'SEC_CASE_SENSITIVE_LOGON',
                                                   'parallel_force_local',
                                                   'db_file_multiblock_read_count',
                                                   'event',
                                                   'dispatchers',
                                                   'db_writer_processes',
                                                   'optimizer_mode')))
           AND p.name NOT IN ('thread',
                              'instance_name',
                              'instance_number',
                              'undo_tablespace',
                              'local_listener',
                              'remote_listener',
                              'lisneter_network',
                              'control_files')
         ORDER BY s.name) v;

host echo start collect......Database Resource Informaion... 

prompt <a name="resource_limit"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Resource Limit</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="resourcelimit" border="1" width="90%" align="center" summary="Script output" '

COLUMN  inst_id              HEADING  'ID'
COLUMN  resource_name        HEADING  'Resource Name'
COLUMN  current_utilization  HEADING  'Current Value'
COLUMN  max_utilization      HEADING  'Max Value'
COLUMN  initial_allocation   HEADING  'Initial Value'
COLUMN  limit_value          HEADING  'Limit Value'

SELECT inst_id,
       resource_name,
       CASE
         WHEN current_utilization / decode(to_number(translate(limit_value,
                                                               'UNLIMITED',
                                                               '10000000000')),
                                           '0',
                                           '10000000000') > 0.8 THEN
          '<font color="red"><b>' || current_utilization || '</b></font>'
         ELSE
          to_char(current_utilization)
       END current_utilization,
       CASE
         WHEN max_utilization / decode(to_number(translate(limit_value,
                                                           'UNLIMITED',
                                                           '10000000000')),
                                       '0',
                                       '10000000000') > 0.5 THEN
          '<font color="red"><b>' || max_utilization || '</b></font>'
         ELSE
          to_char(max_utilization)
       END max_utilization,
       CASE
         WHEN resource_name IN ('processes',
                                'sessions')
              AND initial_allocation < 500 THEN
          '<font color="red"><b>' || initial_allocation || '</b></font>'
         ELSE
          to_char(initial_allocation)
       END initial_allocation,
       limit_value
  FROM gv$resource_limit
 WHERE resource_name IN ('processes',
                         'sessions',
                         'transactions',
                         'parallel_max_servers',
                         'dml_locks',
                         'max_rollback_segments',
                         'max_shared_servers')
 ORDER BY 1,
          2;

host echo start collect......Database ControlFile Informaion... 

prompt <a name="control_file"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Control File</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="controlfiles" border="1" width="90%" align="center" summary="Script output" '

COLUMN  name    HEADING  'ControlFile Name'
COLUMN  sizemb  HEADING  'Size(MB)'
COLUMN  cnt     HEADING  'Recode Count'

SELECT v.name,
       CASE
         WHEN v.sizemb > 512 THEN
          '<font color="red"><b>' || to_char(v.sizemb,
                                             '999,990.99') || '</b></font>'
         ELSE
          to_char(v.sizemb,
                  '999,990.99')
       END sizemb,
       CASE
         WHEN v.cnt > 5000 THEN
          '<font color="red"><b>' || v.cnt || '</b></font>'
         ELSE
          to_char(v.cnt)
       END cnt
  FROM (WITH a AS (SELECT COUNT(*) cnt
                     FROM v$controlfile_record_section)
         SELECT rownum rn,
                NAME,
                block_size * file_size_blks / 1024 / 1024 sizemb,
                a.cnt
           FROM v$controlfile,
                a) v;

host echo start collect......Database LogFile Informaion... 

prompt <a name="redolog_file"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● RedoLog File Check</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="redologfile" border="1" width="90%" align="center" summary="Script output" '

COLUMN  inst_id    HEADING  'ID'
COLUMN  group#     HEADING  'Group#'
COLUMN  member     HEADING  'Member'
COLUMN  status     HEADING  'Status'
COLUMN  redo_size  HEADING  'Size(MB)'

SELECT inst_id,
       v.group#,
       v.member,
       decode(v.status,
              'ACTIVE',
              '<div align="right"><font color="red"><b>' || v.status || '</b></font></div>',
              'CURRENT',
              '<div align="right"><font color="red"><b>' || v.status || '</b></font></div>',
              'INACTIVE',
              '<div align="right"><font color="green"><b>' || v.status || '</b></font></div>',
              '<div align="right">' || v.status || '</div>') status,
       CASE
         WHEN v.redo_size <= 100 THEN
          '<div align="right"><font color="red"><b>' || v.redo_size || '</b></font></div>'
         ELSE
          '<div align="right">' || v.redo_size || '</div>'
       END redo_size
  FROM (SELECT (select inst_id from gv$instance where thread# = a.thread#) inst_id,
               a.group#,
               b.member,
               a.status,
               trunc(a.bytes / 1024 / 1024) redo_size
          FROM v$log     a,
               v$logfile b
         WHERE a.group# = b.group#
           AND b.type = 'ONLINE'
           AND a.status <> 'UNUSED'
         ORDER BY 1,
                  2) v;

prompt <a name="redo_switch"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Redolog Switch Check</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="redoswitch" border="1" width="90%" align="center" summary="Script output" '

COLUMN  inst_id  HEADING  'ID'
COLUMN  day      HEADING  'Date'
COLUMN  h00      HEADING  'H00'
COLUMN  h01      HEADING  'H01'
COLUMN  h02      HEADING  'H02'
COLUMN  h03      HEADING  'H03'
COLUMN  h04      HEADING  'H04'
COLUMN  h05      HEADING  'H05'
COLUMN  h06      HEADING  'H06'
COLUMN  h07      HEADING  'H07'
COLUMN  h08      HEADING  'H08'
COLUMN  h09      HEADING  'H09'
COLUMN  h10      HEADING  'H10'
COLUMN  h11      HEADING  'H11'
COLUMN  h12      HEADING  'H12'
COLUMN  h13      HEADING  'H13'
COLUMN  h14      HEADING  'H14'
COLUMN  h15      HEADING  'H15'
COLUMN  h16      HEADING  'H16'
COLUMN  h17      HEADING  'H17'
COLUMN  h18      HEADING  'H18'
COLUMN  h19      HEADING  'H19'
COLUMN  h20      HEADING  'H20'
COLUMN  h21      HEADING  'H21'
COLUMN  h22      HEADING  'H22'
COLUMN  h23      HEADING  'H23'
COLUMN  total    HEADING  'Total'

SELECT inst_id,
       v.day,
       CASE WHEN v.h00 > 100 THEN '<div align="right"><font color="red"><b>' || v.h00 || '</b></font></div>' ELSE '<div align="right">' || v.h00 || '</div>' END h00,
       CASE WHEN v.h01 > 100 THEN '<div align="right"><font color="red"><b>' || v.h01 || '</b></font></div>' ELSE '<div align="right">' || v.h01 || '</div>' END h01,
       CASE WHEN v.h02 > 100 THEN '<div align="right"><font color="red"><b>' || v.h02 || '</b></font></div>' ELSE '<div align="right">' || v.h02 || '</div>' END h02,
       CASE WHEN v.h03 > 100 THEN '<div align="right"><font color="red"><b>' || v.h03 || '</b></font></div>' ELSE '<div align="right">' || v.h03 || '</div>' END h03,
       CASE WHEN v.h04 > 100 THEN '<div align="right"><font color="red"><b>' || v.h04 || '</b></font></div>' ELSE '<div align="right">' || v.h04 || '</div>' END h04,
       CASE WHEN v.h05 > 100 THEN '<div align="right"><font color="red"><b>' || v.h05 || '</b></font></div>' ELSE '<div align="right">' || v.h05 || '</div>' END h05,
       CASE WHEN v.h06 > 100 THEN '<div align="right"><font color="red"><b>' || v.h06 || '</b></font></div>' ELSE '<div align="right">' || v.h06 || '</div>' END h06,
       CASE WHEN v.h07 > 100 THEN '<div align="right"><font color="red"><b>' || v.h07 || '</b></font></div>' ELSE '<div align="right">' || v.h07 || '</div>' END h07,
       CASE WHEN v.h08 > 100 THEN '<div align="right"><font color="red"><b>' || v.h08 || '</b></font></div>' ELSE '<div align="right">' || v.h08 || '</div>' END h08,
       CASE WHEN v.h09 > 100 THEN '<div align="right"><font color="red"><b>' || v.h09 || '</b></font></div>' ELSE '<div align="right">' || v.h09 || '</div>' END h09,
       CASE WHEN v.h10 > 100 THEN '<div align="right"><font color="red"><b>' || v.h10 || '</b></font></div>' ELSE '<div align="right">' || v.h10 || '</div>' END h10,
       CASE WHEN v.h11 > 100 THEN '<div align="right"><font color="red"><b>' || v.h11 || '</b></font></div>' ELSE '<div align="right">' || v.h11 || '</div>' END h11,
       CASE WHEN v.h12 > 100 THEN '<div align="right"><font color="red"><b>' || v.h12 || '</b></font></div>' ELSE '<div align="right">' || v.h12 || '</div>' END h12,
       CASE WHEN v.h13 > 100 THEN '<div align="right"><font color="red"><b>' || v.h13 || '</b></font></div>' ELSE '<div align="right">' || v.h13 || '</div>' END h13,
       CASE WHEN v.h14 > 100 THEN '<div align="right"><font color="red"><b>' || v.h14 || '</b></font></div>' ELSE '<div align="right">' || v.h14 || '</div>' END h14,
       CASE WHEN v.h15 > 100 THEN '<div align="right"><font color="red"><b>' || v.h15 || '</b></font></div>' ELSE '<div align="right">' || v.h15 || '</div>' END h15,
       CASE WHEN v.h16 > 100 THEN '<div align="right"><font color="red"><b>' || v.h16 || '</b></font></div>' ELSE '<div align="right">' || v.h16 || '</div>' END h16,
       CASE WHEN v.h17 > 100 THEN '<div align="right"><font color="red"><b>' || v.h17 || '</b></font></div>' ELSE '<div align="right">' || v.h17 || '</div>' END h17,
       CASE WHEN v.h18 > 100 THEN '<div align="right"><font color="red"><b>' || v.h18 || '</b></font></div>' ELSE '<div align="right">' || v.h18 || '</div>' END h18,
       CASE WHEN v.h19 > 100 THEN '<div align="right"><font color="red"><b>' || v.h19 || '</b></font></div>' ELSE '<div align="right">' || v.h19 || '</div>' END h19,
       CASE WHEN v.h20 > 100 THEN '<div align="right"><font color="red"><b>' || v.h20 || '</b></font></div>' ELSE '<div align="right">' || v.h20 || '</div>' END h20,
       CASE WHEN v.h21 > 100 THEN '<div align="right"><font color="red"><b>' || v.h21 || '</b></font></div>' ELSE '<div align="right">' || v.h21 || '</div>' END h21,
       CASE WHEN v.h22 > 100 THEN '<div align="right"><font color="red"><b>' || v.h22 || '</b></font></div>' ELSE '<div align="right">' || v.h22 || '</div>' END h22,
       CASE WHEN v.h23 > 100 THEN '<div align="right"><font color="red"><b>' || v.h23 || '</b></font></div>' ELSE '<div align="right">' || v.h23 || '</div>' END h23,
       CASE WHEN v.total > 1000 THEN '<div align="right"><font color="red"><b>' || v.total || '</b></font></div>' ELSE '<div align="right">' || v.total || '</div>' END total
  FROM (SELECT (select b.inst_id from gv$instance b where b.thread# = a.thread#) inst_id,
               substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),1,10) DAY,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'00',1,0)) h00,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'01',1,0)) h01,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'02',1,0)) h02,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'03',1,0)) h03,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'04',1,0)) h04,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'05',1,0)) h05,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'06',1,0)) h06,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'07',1,0)) h07,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'08',1,0)) h08,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'09',1,0)) h09,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'10',1,0)) h10,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'11',1,0)) h11,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'12',1,0)) h12,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'13',1,0)) h13,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'14',1,0)) h14,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'15',1,0)) h15,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'16',1,0)) h16,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'17',1,0)) h17,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'18',1,0)) h18,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'19',1,0)) h19,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'20',1,0)) h20,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'21',1,0)) h21,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'22',1,0)) h22,
               SUM(decode(substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),12,2),'23',1,0)) h23,
               COUNT(*) total
          FROM v$log_history a
         WHERE first_time > SYSDATE - 7
         GROUP BY thread#,
                  substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),1,10)
         ORDER BY thread#,
                  substr(to_char(first_time,'YYYY-MM-DD HH24:MI:SS'),1,10)) v;

host echo start collect......Archive Log Size in last 10 Days... 

prompt <a name="archivelog_size"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Archived Log Size In Last 10 Days</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="archperday" border="1" width="90%" align="center" summary="Script output" '

COLUMN  inst_id  HEADING  'ID'
COLUMN  time     HEADING  'Date'
COLUMN  sizegb   HEADING  'Size(GB)'

SELECT (select inst_id from gv$instance where thread# = a.thread#) inst_id,
       trunc(first_time) time,
       '<div align="left">' || round(SUM(blocks * block_size) / 1024 / 1024 / 1024) || '</div>' sizegb
  FROM v$archived_log a
 WHERE dest_id = 1
   AND first_time > SYSDATE - 10
 GROUP BY thread#,
          trunc(first_time)
 ORDER BY 1,
          2 DESC;

host echo start collect......Invalid Object Informaion... 

prompt <a name="invalid_object"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Invalid Object</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="dbinvalid" border="1" width="90%" align="center" summary="Script output" '

COLUMN  owner        HEADING  'Owner'
COLUMN  object_type  HEADING  'Object Type'
COLUMN  status       HEADING  'Status'
COLUMN  counts       HEADING  'Count'

SELECT owner,
       object_type,
       '<font color="red"><b>' || status || '</b></font>' status,
       CASE
         WHEN COUNT(object_name) > 50 THEN
          '<div align="left"><font color="red"><b>' || COUNT(object_name) || '</b></font></div>'
         ELSE
          '<div align="left">' || COUNT(object_name) || '</div>'
       END counts
  FROM dba_objects
 WHERE status = 'INVALID'
   AND owner NOT IN ('ADM_PARALLEL_EXECUTE_TASK',
                     'ANONYMOUS',
                     'APEX_030200',
                     'APEX_ADMINISTRATOR_ROLE',
                     'APEX_PUBLIC_USER',
                     'APPQOSSYS',
                     'AQ_ADMINISTRATOR_ROLE',
                     'AQ_USER_ROLE',
                     'CONNECT',
                     'CSW_USR_ROLE',
                     'CTXAPP',
                     'CTXSYS',
                     'CWM_USER',
                     'DATAPUMP_EXP_FULL_DATABASE',
                     'DATAPUMP_IMP_FULL_DATABASE',
                     'DBA',
                     'DBFS_ROLE',
                     'DBSNMP',
                     'DELETE_CATALOG_ROLE',
                     'DIP',
                     'EXECUTE_CATALOG_ROLE',
                     'EXFSYS',
                     'EXP_FULL_DATABASE',
                     'FLOWS_FILES',
                     'GATHER_SYSTEM_STATISTICS',
                     'HS_ADMIN_EXECUTE_ROLE',
                     'HS_ADMIN_ROLE',
                     'HS_ADMIN_SELECT_ROLE',
                     'IMP_FULL_DATABASE',
                     'JAVADEBUGPRIV',
                     'JAVASYSPRIV',
                     'LOGSTDBY_ADMINISTRATOR',
                     'MDDATA',
                     'MDSYS',
                     'MGMT_USER',
                     'MGMT_VIEW',
                     'OEM_ADVISOR',
                     'OEM_MONITOR',
                     'OLAPSYS',
                     'OLAP_DBA',
                     'OLAP_USER',
                     'OLAP_XS_ADMIN',
                     'ORACLE_OCM',
                     'ORDADMIN',
                     'ORDDATA',
                     'ORDPLUGINS',
                     'ORDSYS',
                     'OUTLN',
                     'OWB$CLIENT',
                     'OWBSYS',
                     'OWBSYS_AUDIT',
                     'PUBLIC',
                     'RECOVERY_CATALOG_OWNER',
                     'RESOURCE',
                     'SCHEDULER_ADMIN',
                     'SCOTT',
                     'SELECT_CATALOG_ROLE',
                     'SI_INFORMTN_SCHEMA',
                     'SPATIAL_CSW_ADMIN',
                     'SPATIAL_CSW_ADMIN_USR',
                     'SPATIAL_WFS_ADMIN',
                     'SPATIAL_WFS_ADMIN_USR',
                     'SQLTXADMIN',
                     'SQLTXPLAIN',
                     'SQLT_USER_ROLE',
                     'SYS',
                     'SYSMAN',
                     'SYSTEM',
                     'WFS_USR_ROLE',
                     'WM_ADMIN_ROLE',
                     'XDB',
                     'XDBADMIN',
                     'AUDSYS',
                     'OJVMSYS',
                     'GSMADMIN_INTERNAL',
                     'PERFSTAT',
                     'LBACSYS',
                     'WMSYS',
                     'MDSYS')
 GROUP BY owner,
          object_type,
          status
 ORDER BY owner,
          4 DESC;

host echo start collect......Tablespace Usage Informaion... 

prompt <a name="tablespace_usage"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Tablespace Usage</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="tablespaces" border="1" width="90%" align="center" summary="Script output" '

COLUMN  tbsname                         HEADING  'Name'
COLUMN  total_gb      FORMAT 99,990.99  HEADING  'Total(GB)'
COLUMN  used_gb       FORMAT 99,990.99  HEADING  'Used(GB)'
COLUMN  left_gb       FORMAT 99,990.99  HEADING  'Left(GB)'
COLUMN  used_percent  FORMAT 990.99     HEADING  'Used(%)'
COLUMN  count_file    FORMAT 9999       HEADING  'File Count'

WITH file_counts AS
 (SELECT tablespace_name,
         SUM(count_file) count_file
    FROM (SELECT tablespace_name,
                 COUNT(*) count_file
            FROM dba_data_files
           GROUP BY tablespace_name
          UNION ALL
          SELECT tablespace_name,
                 COUNT(*) count_file
            FROM dba_temp_files
           GROUP BY tablespace_name)
   GROUP BY tablespace_name),
metric_usage AS
 (SELECT m.tablespace_name tbsname,
         m.tablespace_size * t.block_size total_bytes,
         m.used_space * t.block_size used_bytes,
         GREATEST(m.tablespace_size - m.used_space, 0) * t.block_size free_bytes,
         NVL(fc.count_file, 0) count_file
    FROM dba_tablespace_usage_metrics m
    JOIN dba_tablespaces t
      ON t.tablespace_name = m.tablespace_name
    LEFT JOIN file_counts fc
      ON fc.tablespace_name = m.tablespace_name
   WHERE m.tablespace_size > 0
     AND m.used_space IS NOT NULL),
fallback_usage AS
 (SELECT df.tablespace_name tbsname,
         df.total_bytes,
         df.total_bytes - NVL(fs.free_bytes, 0) used_bytes,
         NVL(fs.free_bytes, 0) free_bytes,
         df.count_file
    FROM (SELECT tablespace_name,
                 SUM(bytes) total_bytes,
                 COUNT(*) count_file
            FROM dba_data_files
           GROUP BY tablespace_name) df
    LEFT JOIN (SELECT tablespace_name,
                      SUM(bytes) free_bytes
                 FROM dba_free_space
                GROUP BY tablespace_name) fs
      ON fs.tablespace_name = df.tablespace_name
  UNION ALL
  SELECT tf.tablespace_name tbsname,
         tf.total_bytes,
         NVL(th.used_bytes, 0) used_bytes,
         GREATEST(tf.total_bytes - NVL(th.used_bytes, 0), 0) free_bytes,
         tf.count_file
    FROM (SELECT tablespace_name,
                 SUM(bytes) total_bytes,
                 COUNT(*) count_file
            FROM dba_temp_files
           GROUP BY tablespace_name) tf
    -- Use the local fixed view so RAC capacity is not multiplied per instance.
    LEFT JOIN (SELECT tablespace_name,
                      SUM(bytes_used) used_bytes
                 FROM v$temp_space_header
                GROUP BY tablespace_name) th
      ON th.tablespace_name = tf.tablespace_name),
space_usage AS
 (SELECT m.tbsname,
         m.total_bytes,
         m.used_bytes,
         m.free_bytes,
         m.count_file
    FROM metric_usage m
  UNION ALL
  SELECT f.tbsname,
         f.total_bytes,
         f.used_bytes,
         f.free_bytes,
         f.count_file
    FROM fallback_usage f
   WHERE NOT EXISTS (SELECT 1
                       FROM metric_usage m
                      WHERE m.tbsname = f.tbsname))
SELECT v.tbsname,
       ROUND(v.total_bytes / 1024 / 1024 / 1024, 2) total_gb,
       ROUND(v.used_bytes / 1024 / 1024 / 1024, 2) used_gb,
       ROUND(v.free_bytes / 1024 / 1024 / 1024, 2) left_gb,
       CASE
         WHEN v.total_bytes > 0
              AND v.used_bytes / v.total_bytes * 100 >= 90
              AND v.tbsname NOT LIKE 'UNDO%' THEN
          '<div align="right"><font color="red"><b>' ||
          ROUND(v.used_bytes / v.total_bytes * 100, 2) ||
          '</b></font></div>'
         ELSE
          '<div align="right"><font color="green"><b>' ||
          NVL(ROUND(v.used_bytes / NULLIF(v.total_bytes, 0) * 100, 2), 0) ||
          '</b></font></div>'
       END used_percent,
       v.count_file
  FROM space_usage v
 ORDER BY v.total_bytes DESC;

prompt <a name="tablespace_total"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Tablespace Total</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="tbstotal" border="1" width="90%" align="center" summary="Script output" '

COLUMN  ts_datafile_physical_size_G  FORMAT 999,999,990.99  HEADING  'Datafile Physical Size(GB)'
COLUMN  ts_tempfile_physical_size_G  FORMAT 999,999,990.99  HEADING  'Tempfile Physical Size(GB)'
COLUMN  ts_datafile_used_size_G      FORMAT 999,999,990.99  HEADING  'Datafile Userd Size(GB)'

SELECT (SELECT round(SUM(bytes)/1024/1024/1024,2) FROM dba_data_files) ts_datafile_physical_size_G,
       (SELECT round(SUM(bytes)/1024/1024/1024,2) FROM dba_temp_files) ts_tempfile_physical_size_G,
       (SELECT round(SUM(bytes)/1024/1024/1024,2) FROM dba_segments) ts_datafile_used_size_G
  FROM dual;

prompt <a name="tablespace_perday"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Tablespace Per Day</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="tbsperday" border="1" width="90%" align="center" summary="Script output" '

COLUMN  rtime                                    HEADING  'Date'
COLUMN  ts_size_gb        FORMAT 999,999,990.99  HEADING  'Total Size(GB)'
COLUMN  ts_used_gb        FORMAT 999,999,990.99  HEADING  'Used Size(GB)'
COLUMN  ts_free_gb        FORMAT 999,999,990.99  HEADING  'Free Size(GB)'
COLUMN  increase_size_gb  FORMAT 999,999,990.99  HEADING  'Increase Size(GB)'
COLUMN  pct_used                                 HEADING  'Percent(%)'

SELECT v.rtime,
       v.ts_size_gb,
       v.ts_used_gb,
       v.ts_free_gb,
       v.ts_used_gb - lead(v.ts_used_gb,
                           1,
                           NULL) over(ORDER BY v.rtime DESC) increase_size_gb,
       CASE
         WHEN v.pct_used >= 90 THEN
          '<div align="right"><font color="red"><b>' || to_char(v.pct_used,'990.99') || '</b></font></div>'
         ELSE
          '<div align="right"><font color="green"><b>' || to_char(v.pct_used,'990.99') || '</b></font></div>'
       END pct_used
  FROM (SELECT to_char(to_date(a.rtime,
                               'mm/dd/yyyy hh24:mi:ss'),
                       'yyyy-mm-dd hh24:mi') rtime,
               SUM(a.tablespace_size * c.block_size / 1024 / 1024 / 1024) ts_size_gb,
               SUM(a.tablespace_usedsize * c.block_size / 1024 / 1024 / 1024) ts_used_gb,
               SUM((a.tablespace_size - a.tablespace_usedsize) * c.block_size / 1024 / 1024 / 1024) ts_free_gb,
               ROUND(SUM(a.tablespace_usedsize) / SUM(a.tablespace_size) * 100,2) pct_used
          FROM dba_hist_tbspc_space_usage a,
               (SELECT tablespace_id,
                       substr(rtime,
                              1,
                              10) rtime,
                       MAX(snap_id) snap_id
                  FROM dba_hist_tbspc_space_usage nb
                 GROUP BY tablespace_id,
                          substr(rtime,
                                 1,
                                 10)) b,
               dba_tablespaces c,
               v$tablespace d
         WHERE a.snap_id = b.snap_id
           AND a.tablespace_id = b.tablespace_id
           AND a.tablespace_id = d.ts#
           AND d.name = c.tablespace_name
           AND d.name not like 'UNDO%'
           AND to_date(a.rtime,
                       'mm/dd/yyyy hh24:mi:ss') > (SELECT MAX(startup_time)
                                                     FROM gv$instance)
           AND to_date(a.rtime,
                       'mm/dd/yyyy hh24:mi:ss') >= SYSDATE - 30
         GROUP BY a.rtime
         ORDER BY to_date(a.rtime,
                          'mm/dd/yyyy hh24:mi:ss') DESC) v;

prompt <a name="recyclebin_info"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Recyclebin Information</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="recyleobj" border="1" width="90%" align="center" summary="Script output" '

COLUMN  recyb_cnt                          HEADING  'Recyclebin Count'
COLUMN  recyb_size  FORMAT 999,999,990.99  HEADING  'Recyclebin Size(GB)'

SELECT CASE
         WHEN COUNT(1) > 1000 THEN
          '<div align="right"><font color="red"><b>' || COUNT(1) || '</b></font></div>'
         ELSE
          '<div align="right">' || COUNT(1) || '</div>'
       END recyb_cnt,
       round(SUM(a.space * (SELECT VALUE
                              FROM v$parameter
                             WHERE NAME = 'db_block_size')) / 1024 / 1024 / 1024,
             2) recyb_size
  FROM dba_recyclebin a;

prompt <a name="top10_table"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Top 10 Table</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="top10tab" border="1" width="90%" align="center" summary="Script output" '

COLUMN  owner                               HEADING  'Owner'
COLUMN  table_name                          HEADING  'Table Name'
COLUMN  partitioned                         HEADING  'Partitioned'
COLUMN  size_gb      FORMAT 999,999,990.99  HEADING  'Size(GB)'

SELECT v.owner,
       v.table_name,
       decode(v.partitioned,
              'YES',
              '<font color="green"><b>' || v.partitioned || '</b></font>',
              '<font color="red"><b>' || v.partitioned || '</b></font>') partitioned,
       CASE
         WHEN v.size_gb > 100 THEN
          '<font color="red"><b>' || v.size_gb || '</b></font>'
         ELSE
          to_char(v.size_gb)
       END sizegb
  FROM (SELECT t.owner,
               t.table_name,
               t.partitioned,
               round(SUM(s.bytes) / 1024 / 1024 / 1024,
                     2) size_gb
          FROM dba_tables   t,
               dba_segments s
         WHERE t.owner = s.owner
           AND s.segment_name = t.table_name
           AND s.segment_type LIKE 'TABLE%'
           AND s.tablespace_name NOT IN ('SYSTEM',
                                         'SYSAUX')
           AND s.owner NOT IN ('ADM_PARALLEL_EXECUTE_TASK',
                               'ANONYMOUS',
                               'APEX_030200',
                               'APEX_ADMINISTRATOR_ROLE',
                               'APEX_PUBLIC_USER',
                               'APPQOSSYS',
                               'AQ_ADMINISTRATOR_ROLE',
                               'AQ_USER_ROLE',
                               'CONNECT',
                               'CSW_USR_ROLE',
                               'CTXAPP',
                               'CTXSYS',
                               'CWM_USER',
                               'DATAPUMP_EXP_FULL_DATABASE',
                               'DATAPUMP_IMP_FULL_DATABASE',
                               'DBA',
                               'DBFS_ROLE',
                               'DBSNMP',
                               'DELETE_CATALOG_ROLE',
                               'DIP',
                               'EXECUTE_CATALOG_ROLE',
                               'EXFSYS',
                               'EXP_FULL_DATABASE',
                               'FLOWS_FILES',
                               'GATHER_SYSTEM_STATISTICS',
                               'HS_ADMIN_EXECUTE_ROLE',
                               'HS_ADMIN_ROLE',
                               'HS_ADMIN_SELECT_ROLE',
                               'IMP_FULL_DATABASE',
                               'JAVADEBUGPRIV',
                               'JAVASYSPRIV',
                               'LOGSTDBY_ADMINISTRATOR',
                               'MDDATA',
                               'MDSYS',
                               'MGMT_USER',
                               'MGMT_VIEW',
                               'OEM_ADVISOR',
                               'OEM_MONITOR',
                               'OLAPSYS',
                               'OLAP_DBA',
                               'OLAP_USER',
                               'OLAP_XS_ADMIN',
                               'ORACLE_OCM',
                               'ORDADMIN',
                               'ORDDATA',
                               'ORDPLUGINS',
                               'ORDSYS',
                               'OUTLN',
                               'OWB$CLIENT',
                               'OWBSYS',
                               'OWBSYS_AUDIT',
                               'PUBLIC',
                               'RECOVERY_CATALOG_OWNER',
                               'RESOURCE',
                               'SCHEDULER_ADMIN',
                               'SCOTT',
                               'SELECT_CATALOG_ROLE',
                               'SI_INFORMTN_SCHEMA',
                               'SPATIAL_CSW_ADMIN',
                               'SPATIAL_CSW_ADMIN_USR',
                               'SPATIAL_WFS_ADMIN',
                               'SPATIAL_WFS_ADMIN_USR',
                               'SQLTXADMIN',
                               'SQLTXPLAIN',
                               'SQLT_USER_ROLE',
                               'SYS',
                               'SYSMAN',
                               'SYSTEM',
                               'WFS_USR_ROLE',
                               'WMSYS',
                               'WM_ADMIN_ROLE',
                               'XDB',
                               'XDBADMIN',
                               'AUDSYS',
                               'OJVMSYS',
                               'GSMADMIN_INTERNAL',
                               'PERFSTAT')
         GROUP BY t.owner,
                  t.table_name,
                  t.partitioned,
                  t.table_name
        HAVING round(SUM(bytes / 1024 / 1024)) > 10240
         ORDER BY size_gb DESC) v
 WHERE rownum <= 10;

host echo start collect......Top10 Index Informaion... 

prompt <a name="top10_index"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Top 10 Index</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="top10idx" border="1" width="90%" align="center" summary="Script output" '

COLUMN  owner                               HEADING  'Owner'
COLUMN  table_name                          HEADING  'Table Name'
COLUMN  index_name                          HEADING  'Index Name'
COLUMN  partitioned                         HEADING  'Partitioned'
COLUMN  size_gb      FORMAT 999,999,990.99  HEADING  'Size(GB)'

SELECT v.owner,
       v.table_name,
       v.index_name,
       decode(v.partitioned,
              'YES',
              '<font color="green"><b>' || v.partitioned || '</b></font>',
              '<font color="red"><b>' || v.partitioned || '</b></font>') partitioned,
       CASE
         WHEN v.size_gb > 100 THEN
          '<font color="red"><b>' || v.size_gb || '</b></font>'
         ELSE
          to_char(v.size_gb)
       END size_gb
  FROM (SELECT i.owner,
               i.table_name,
               i.index_name,
               i.partitioned,
               round(SUM(s.bytes) / 1024 / 1024 / 1024,
                     2) size_gb
          FROM dba_indexes  i,
               dba_segments s
         WHERE s.owner = i.owner
           AND s.segment_name = i.index_name
           AND s.segment_type LIKE 'INDEX%'
           AND s.segment_name NOT LIKE 'BIN$%'
           AND s.tablespace_name NOT IN ('SYSTEM',
                                         'SYSAUX')
           AND s.owner NOT IN ('ADM_PARALLEL_EXECUTE_TASK',
                               'ANONYMOUS',
                               'APEX_030200',
                               'APEX_ADMINISTRATOR_ROLE',
                               'APEX_PUBLIC_USER',
                               'APPQOSSYS',
                               'AQ_ADMINISTRATOR_ROLE',
                               'AQ_USER_ROLE',
                               'CONNECT',
                               'CSW_USR_ROLE',
                               'CTXAPP',
                               'CTXSYS',
                               'CWM_USER',
                               'DATAPUMP_EXP_FULL_DATABASE',
                               'DATAPUMP_IMP_FULL_DATABASE',
                               'DBA',
                               'DBFS_ROLE',
                               'DBSNMP',
                               'DELETE_CATALOG_ROLE',
                               'DIP',
                               'EXECUTE_CATALOG_ROLE',
                               'EXFSYS',
                               'EXP_FULL_DATABASE',
                               'FLOWS_FILES',
                               'GATHER_SYSTEM_STATISTICS',
                               'HS_ADMIN_EXECUTE_ROLE',
                               'HS_ADMIN_ROLE',
                               'HS_ADMIN_SELECT_ROLE',
                               'IMP_FULL_DATABASE',
                               'JAVADEBUGPRIV',
                               'JAVASYSPRIV',
                               'LOGSTDBY_ADMINISTRATOR',
                               'MDDATA',
                               'MDSYS',
                               'MGMT_USER',
                               'MGMT_VIEW',
                               'OEM_ADVISOR',
                               'OEM_MONITOR',
                               'OLAPSYS',
                               'OLAP_DBA',
                               'OLAP_USER',
                               'OLAP_XS_ADMIN',
                               'ORACLE_OCM',
                               'ORDADMIN',
                               'ORDDATA',
                               'ORDPLUGINS',
                               'ORDSYS',
                               'OUTLN',
                               'OWB$CLIENT',
                               'OWBSYS',
                               'OWBSYS_AUDIT',
                               'PUBLIC',
                               'RECOVERY_CATALOG_OWNER',
                               'RESOURCE',
                               'SCHEDULER_ADMIN',
                               'SCOTT',
                               'SELECT_CATALOG_ROLE',
                               'SI_INFORMTN_SCHEMA',
                               'SPATIAL_CSW_ADMIN',
                               'SPATIAL_CSW_ADMIN_USR',
                               'SPATIAL_WFS_ADMIN',
                               'SPATIAL_WFS_ADMIN_USR',
                               'SQLTXADMIN',
                               'SQLTXPLAIN',
                               'SQLT_USER_ROLE',
                               'SYS',
                               'SYSMAN',
                               'SYSTEM',
                               'WFS_USR_ROLE',
                               'WMSYS',
                               'WM_ADMIN_ROLE',
                               'XDB',
                               'XDBADMIN',
                               'AUDSYS',
                               'OJVMSYS',
                               'GSMADMIN_INTERNAL',
                               'PERFSTAT')
         GROUP BY i.owner,
                  i.table_name,
                  i.partitioned,
                  i.index_name
        HAVING round(SUM(bytes / 1024 / 1024)) > 10240
         ORDER BY size_gb DESC) v
 WHERE rownum <= 10;

host echo start collect......Range Partition Extend Check Informaion... 

prompt <a name="range_par_extend"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Range Partition Extend Check</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="par_extend" border="1" width="90%" align="center" summary="Script output" '

COLUMN  username                            HEADING  'Owner'
COLUMN  tablename                           HEADING  'Table Name'
COLUMN  partname                            HEADING  'Partition Name'
COLUMN  max_range                           HEADING  'Max Range Date'

SELECT p.table_owner username,
       p.table_name tablename,
       p.partition_name partname,
       p.high_value max_range
  FROM dba_tab_partitions p
  JOIN (SELECT tp.table_owner,
               tp.table_name,
               MAX(tp.partition_position) - 1 partition_position
          FROM dba_tab_partitions tp
          JOIN dba_users du
            ON du.username = tp.table_owner
         WHERE du.account_status = 'OPEN'
         GROUP BY tp.table_owner,
                  tp.table_name) penultimate
    ON penultimate.table_owner = p.table_owner
   AND penultimate.table_name = p.table_name
   AND penultimate.partition_position = p.partition_position
 WHERE p.table_owner NOT IN ('SYS',
                             'SYSTEM',
                             'DBSNMP',
                             'MGMT_VIEW',
                             'SYSMAN',
                             'SH',
                             'MDSYS',
                             'AUDSYS')
   AND NOT EXISTS (SELECT 1
                     FROM dba_recyclebin r
                    WHERE r.owner = p.table_owner
                      AND r.object_name = p.table_name)
   AND EXISTS (SELECT 1
                 FROM dba_part_key_columns pk
                 JOIN dba_tab_columns tc
                   ON tc.owner = pk.owner
                  AND tc.table_name = pk.name
                  AND tc.column_name = pk.column_name
                WHERE pk.owner = p.table_owner
                  AND pk.name = p.table_name
                  AND (tc.data_type = 'DATE' OR tc.data_type LIKE 'TIMESTAMP%'))
 ORDER BY p.table_owner,
          p.table_name;


host echo start collect......Foreign keys without Index Informaion... 

prompt <a name="foreigno_index"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Foreign Keys Without Index</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="fknoidx" border="1" width="90%" align="center" summary="Script output" '

COLUMN  owner            HEADING  'Owner'
COLUMN  table_name       HEADING  'Table Name'
COLUMN  constraint_name  HEADING  'Constraint Name'
COLUMN  column_name      HEADING  'Column Name'

SELECT v.owner,
       v.table_name,
       v.constraint_name,
       v.column_name
  FROM (SELECT c.owner,
               c.constraint_name,
               c.table_name,
               cc.column_name,
               c.status
          FROM dba_constraints  c,
               dba_cons_columns cc
         WHERE c.constraint_type = 'R'
           AND c.status = 'ENABLED'
           AND c.owner NOT IN ('ADM_PARALLEL_EXECUTE_TASK',
                               'ANONYMOUS',
                               'APEX_030200',
                               'APEX_ADMINISTRATOR_ROLE',
                               'APEX_PUBLIC_USER',
                               'APPQOSSYS',
                               'AQ_ADMINISTRATOR_ROLE',
                               'AQ_USER_ROLE',
                               'CONNECT',
                               'CSW_USR_ROLE',
                               'CTXAPP',
                               'CTXSYS',
                               'CWM_USER',
                               'DATAPUMP_EXP_FULL_DATABASE',
                               'DATAPUMP_IMP_FULL_DATABASE',
                               'DBA',
                               'DBFS_ROLE',
                               'DBSNMP',
                               'DELETE_CATALOG_ROLE',
                               'DIP',
                               'EXECUTE_CATALOG_ROLE',
                               'EXFSYS',
                               'EXP_FULL_DATABASE',
                               'FLOWS_FILES',
                               'GATHER_SYSTEM_STATISTICS',
                               'HS_ADMIN_EXECUTE_ROLE',
                               'HS_ADMIN_ROLE',
                               'HS_ADMIN_SELECT_ROLE',
                               'IMP_FULL_DATABASE',
                               'JAVADEBUGPRIV',
                               'JAVASYSPRIV',
                               'LOGSTDBY_ADMINISTRATOR',
                               'MDDATA',
                               'MDSYS',
                               'MGMT_USER',
                               'MGMT_VIEW',
                               'OEM_ADVISOR',
                               'OEM_MONITOR',
                               'OLAPSYS',
                               'OLAP_DBA',
                               'OLAP_USER',
                               'OLAP_XS_ADMIN',
                               'ORACLE_OCM',
                               'ORDADMIN',
                               'ORDDATA',
                               'ORDPLUGINS',
                               'ORDSYS',
                               'OUTLN',
                               'OWB$CLIENT',
                               'OWBSYS',
                               'OWBSYS_AUDIT',
                               'PUBLIC',
                               'RECOVERY_CATALOG_OWNER',
                               'RESOURCE',
                               'SCHEDULER_ADMIN',
                               'SCOTT',
                               'SELECT_CATALOG_ROLE',
                               'SI_INFORMTN_SCHEMA',
                               'SPATIAL_CSW_ADMIN',
                               'SPATIAL_CSW_ADMIN_USR',
                               'SPATIAL_WFS_ADMIN',
                               'SPATIAL_WFS_ADMIN_USR',
                               'SQLTXADMIN',
                               'SQLTXPLAIN',
                               'SQLT_USER_ROLE',
                               'SYS',
                               'SYSMAN',
                               'SYSTEM',
                               'WFS_USR_ROLE',
                               'WMSYS',
                               'WM_ADMIN_ROLE',
                               'XDB',
                               'XDBADMIN',
                               'PERFSTAT',
                               'GSMADMIN_INTERNAL',
                               'LBACSYS')
           AND c.owner = cc.owner
           AND c.constraint_name = cc.constraint_name
           AND NOT EXISTS (SELECT 1
                  FROM dba_ind_columns ic
                 WHERE cc.owner = ic.table_owner
                   AND cc.table_name = ic.table_name
                   AND cc.column_name = ic.column_name
                   AND cc.position = ic.column_position
                   AND NOT EXISTS (SELECT owner,
                               index_name
                          FROM dba_indexes i
                         WHERE i.table_owner = c.owner
                           AND i.index_name = ic.index_name
                           AND i.owner = ic.index_owner
                           AND (i.status = 'UNUSABLE' OR i.partitioned = 'YES' AND EXISTS
                                (SELECT 1
                                   FROM dba_ind_partitions ip
                                  WHERE status = 'UNUSABLE'
                                    AND ip. index_owner = i. owner
                                    AND ip. index_name = i. index_name
                                 UNION ALL
                                 SELECT 1
                                   FROM dba_ind_subpartitions isp
                                  WHERE status = 'UNUSABLE'
                                    AND isp. index_owner = i. owner
                                    AND isp. index_name = i. index_name))))
         ORDER BY 1,
                  2) v;

host echo start collect......Object in System TableSpace Informaion... 

prompt <a name="object_insystem"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Object In System TableSpace</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="objsystem" border="1" width="90%" align="center" summary="Script output" '

COLUMN  owner            HEADING  'Owner'
COLUMN  object_name      HEADING  'Object Name'
COLUMN  object_type      HEADING  'Object Type'
COLUMN  tablespace_name  HEADING  'TableSpace Name'

SELECT owner,
       object_name,
       object_type,
       tablespace_name
  FROM (SELECT 'Table' object_type,
               owner,
               tablespace_name,
               table_name object_name
          FROM dba_tables
         WHERE temporary = 'N'
        UNION ALL
        SELECT 'Index' object_type,
               owner,
               tablespace_name,
               index_name object_name
          FROM dba_indexes
         WHERE temporary = 'N')
 WHERE tablespace_name = 'SYSTEM'
   AND owner IN (SELECT username
                   FROM dba_users
                  WHERE default_tablespace NOT IN ('SYSTEM',
                                                   'SYSAUX'));

host echo start collect......BitCoin Attack Check... 

prompt <a name="bitcoin_check"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● BitCoin Attack Check</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="bitcoincheck" border="1" width="90%" align="center" summary="Script output" '

COLUMN  owner        HEADING  'Owner'
COLUMN  object_name  HEADING  'Object Name'
COLUMN  object_type  HEADING  'Object Type'
COLUMN  created      HEADING  'Creation Time'

SELECT owner,
       '"' || object_name || '"' object_name,
       object_type,
       to_char(created,
               'yyyy-mm-dd hh24:mi:ss') created
  FROM dba_objects
 WHERE object_name LIKE 'DBMS_CORE_INTERNA%'
    OR object_name LIKE 'DBMS_SYSTEM_INTERNA%'
    OR object_name LIKE 'DBMS_SUPPORT_INTERNA%';

host echo start collect......SYSAUX Objects Informaion... 

prompt <a name="sysaux_obj"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● SYSAUX Object Check</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="auxobj" border="1" width="90%" align="center" summary="Script output" '

COLUMN  segment_name  HEADING  'Object Name'
COLUMN  cnt           HEADING  'Partition Count'
COLUMN  size_mb       HEADING  'Space Usage(MB)'

SELECT v.segment_name,
       v.cnt,
       CASE
         WHEN v.size_mb > 32767 THEN
          '<div align="right"><font color="red"><b>' || v.size_mb || '</b></font></div>'
         ELSE
          '<div align="right">' || v.size_mb || '</div>'
       END size_mb
  FROM (SELECT segment_name,
               COUNT(bytes) cnt,
               round(SUM(bytes) / 1024 / 1024) size_mb
          FROM dba_segments
         WHERE segment_name LIKE 'WRH$_%'
           AND segment_type LIKE '%TABLE%'
         GROUP BY segment_name
         ORDER BY 3 DESC) v
 WHERE rownum < 11;

prompt <a name="sysaux_app"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● SYSAUX Application</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="auxapp" border="1" width="90%" align="center" summary="Script output" '

COLUMN  occupant_desc       HEADING  'Occupant Desc'
COLUMN  occupant_name       HEADING  'Occupant Name'
COLUMN  space_usage_mbytes  HEADING  'Space Usage(MB)'

SELECT v.occupant_desc,
       v.occupant_name,
       CASE
         WHEN space_usage_mbytes > 32767 THEN
          '<font color="red"><b>' || v.space_usage_mbytes || '</b></font>'
         ELSE
          to_char(v.space_usage_mbytes)
       END space_usage_mbytes
  FROM (SELECT occupant_desc,
               occupant_name,
               trunc(space_usage_kbytes / 1024) space_usage_mbytes
          FROM v$sysaux_occupants
         WHERE trunc(space_usage_kbytes / 1024) > 100
         ORDER BY 3 DESC) v;

host echo start collect......High Water Mark Statistics... 

prompt <a name="hwmstat_info"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● High Water Mark Statistics</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE ON
SET MARKUP html TABLE 'id="hwminfo" border="1" width="90%" align="center" summary="Script output" '

COLUMN  name        HEADING  'Statistic Name'
COLUMN  version     HEADING  'Database Version'
COLUMN  highwater   HEADING  'Highwater'
COLUMN  last_value  HEADING  'Last Value'

SELECT NAME,
       version,
       CASE
         WHEN NAME IN ('DB_SIZE',
                       'SEGMENT_SIZE') THEN
          round(highwater / 1024 / 1024 / 1024,
                2) || 'G'
         ELSE
          to_char(round(highwater,
                        2))
       END highwater,
       CASE
         WHEN NAME IN ('DB_SIZE',
                       'SEGMENT_SIZE') THEN
          round(last_value / 1024 / 1024 / 1024,
                2) || 'G'
         ELSE
          to_char(round(last_value,
                        2))
       END last_value
  FROM dba_high_water_mark_statistics
 WHERE highwater IS NOT NULL
 ORDER BY NAME;

host echo start collect......Flashback Database Parameters... 

prompt <a name="flashback_parameters"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Flashback Database Parameters</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'border="1" width="90%" align="center" summary="Script output" '

COLUMN inst_id        HEADING 'Inst ID'
COLUMN instance_name  HEADING 'Instance_Name'
COLUMN para_name      HEADING 'Parameter Name'         
COLUMN para_value     HEADING 'Parameter Value'        

SELECT (select inst_id from gv$instance where thread# = i.thread#) inst_id,
       i.instance_name,
       p.name para_name,
       (CASE p.name
         WHEN 'db_recovery_file_dest_size' THEN
          '<div align="right">' || TRIM(to_char(p.value,
                                                '999,999,999,999,999')) || '</div>'
         WHEN 'db_flashback_retention_target' THEN
          '<div align="right">' || TRIM(to_char(p.value,
                                                '999,999,999,999,999')) || '</div>'
         ELSE
          '<div align="right">' || nvl(p.value,
                                       '(null)') || '</div>'
       END) para_value
  FROM gv$parameter p,
       gv$instance  i
 WHERE p.inst_id = i.inst_id
   AND p.name IN ('db_flashback_retention_target',
                  'db_recovery_file_dest_size',
                  'db_recovery_file_dest')
 ORDER BY 1,
          3;

prompt <a name="flashback_status"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Flashback Database Status</b></font>
 
CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'border="1" width="90%" align="center" summary="Script output" '

COLUMN dbid          HEADING 'DBID'           
COLUMN name          HEADING 'DB Name'         
COLUMN log_mode      HEADING 'Log Mode'        
COLUMN flashback_on  HEADING 'Flashback On?'
 
SELECT dbid,
       NAME,
       log_mode,
       flashback_on
  FROM v$database;

-- +====================================================================================================================+
-- |
-- | <<<<<     OverView Database User Information     >>>>>                                         |
-- |                                                                                                                    |
-- +====================================================================================================================+

host echo  start...OverView Database User Information... 

prompt <a name="20"></a>
prompt <font size="+2" face="Consolas" color="#336699"><b>Schema Information</b></font>

host echo start collect......System Manager Role Informaion... 

prompt <a name="userdefault_passwd"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● User with Default Password</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="userpasswd" border="1" width="90%" align="center" summary="Script output" '

COLUMN  username        HEADING  'Username'
COLUMN  account_status  HEADING  'Account Status'

SELECT username,
       account_status
  FROM dba_users
 WHERE password IN ('E066D214D5421CCC', -- dbsnmp
                    '24ABAB8B06281B4C', -- ctxsys
                    '72979A94BAD2AF80', -- mdsys
                    'C252E8FA117AF049', -- odm
                    'A7A32CD03D3CE8D5', -- odm_mtr
                    '88A2B2C183431F00', -- ordplugins
                    '7EFA02EC7EA6B86F', -- ordsys
                    '4A3BA55E08595C81', -- outln
                    'F894844C34402B67', -- scott
                    '3F9FBD883D787341', -- wk_proxy
                    '79DF7A1BD138CF11', -- wk_sys
                    '7C9BA362F8314299', -- wmsys
                    '88D8364765FCE6AF', -- xdb
                    'F9DA8977092B7B81', -- tracesvr
                    '9300C0977D7DC75E', -- oas_public
                    'A97282CE3D94E29E', -- websys
                    'AC9700FD3F1410EB', -- lbacsys
                    'E7B5D92911C831E1', -- rman
                    'AC98877DE1297365', -- perfstat
                    'D4C5016086B2DC6A', -- sys
                    'D4DF7931AB130E37') -- system
 ORDER BY username;

prompt <a name="system_role"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● System Manager Role</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="dbausers" border="1" width="90%" align="center" summary="Script output" '

COLUMN  grantee       HEADING  'Grantee'
COLUMN  granted_role  HEADING  'Granted Role'
COLUMN  admin_option  HEADING  'Admin Option'
COLUMN  default_role  HEADING  'Default Role'

SELECT v.grantee,
       CASE
         WHEN (v.granted_role = 'DBA' AND v.grantee NOT IN ('SYS',
                                                            'SYSTEM')) THEN
          '<font color="red"><b>' || v.granted_role || '</b></font>'
         ELSE
          v.granted_role
       END granted_role,
       v.admin_option,
       v.default_role
  FROM (SELECT a.grantee,
               a.granted_role,
               a.admin_option,
               a.default_role
          FROM dba_role_privs a,
               dba_users      b
         WHERE b.username = a.grantee
           AND b.account_status = 'OPEN'
           AND a.granted_role IN ('DBA',
                                  'SYSDBA',
                                  'SYSOPER',
                                  'EXP_FULL_DATABASE',
                                  'DELETE_CATALOG_ROLE')
         ORDER BY a.granted_role) v;

host echo start collect......Schema Informaion... 

prompt <a name="schema_info"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Schema Info</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="dbusers" border="1" width="90%" align="center" summary="Script output" '

COLUMN  username              HEADING  'Username'
COLUMN  default_tablespace    HEADING  'Default Tablespace'
COLUMN  temporary_tablespace  HEADING  'Temporary Tablespace'
COLUMN  profile               HEADING  'Profile'
COLUMN  created               HEADING  'Create Time'

SELECT v.username,
       CASE
         WHEN v.created > (SELECT controlfile_created + 1
                             FROM v$database)
              AND v.default_tablespace IN ('SYSTEM',
                                           'SYSAUX') THEN
          '<font color="red"><b>' || v.default_tablespace || '</b></font>'
         ELSE
          to_char(v.default_tablespace)
       END default_tablespace,
       v.temporary_tablespace,
       v.profile,
       CASE
         WHEN v.created > (SELECT controlfile_created + 1
                             FROM v$database) THEN
          '<font color="blue"><b>' || v.created || '</b></font>'
         ELSE
          to_char(v.created)
       END created
  FROM (SELECT username,
               account_status,
               default_tablespace,
               temporary_tablespace,
               profile,
               created
          FROM dba_users
         WHERE account_status = 'OPEN'
         ORDER BY 2 DESC) v;

host echo start collect......Profile Informaion... 

prompt <a name="profile"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Profile</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="profiles" border="1" width="90%" align="center" summary="Script output" '

COLUMN  profile        HEADING  'Profile'
COLUMN  resource_name  HEADING  'Resource Name'
COLUMN  resource_type  HEADING  'Resource Type'
COLUMN  limits         HEADING  'Limits'

SELECT profile,
       resource_name,
       resource_type,
       CASE
         WHEN resource_name NOT IN ('FAILED_LOGIN_ATTEMPTS',
                                    'PASSWORD_GRACE_TIME',
                                    'PASSWORD_LOCK_TIME')
              AND LIMIT NOT IN ('UNLIMITED',
                                'DEFAULT',
                                'NULL',
                                'VERIFY_FUNCTION_11G',
                                'ORA12C_STRONG_VERIFY_FUNCTION',
                                'ORA_STIG_PROFILE') THEN
          '<font color="red"><b>' || LIMIT || '</b></font>'
         ELSE
          '<font color="green"><b>' || LIMIT || '</b></font>'
       END limits
  FROM dba_profiles
 WHERE profile IN (SELECT DISTINCT profile
                     FROM dba_users
                    WHERE account_status = 'OPEN')
 ORDER BY 1,
          2;

host echo start collect......Directory Informaion... 

prompt <a name="directory"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Directory</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE "border='1' width='90%' align='center' summary='Script output'"

COLUMN  owner           HEADING  'Owner'
COLUMN  directory_name  HEADING  'Directory Name'
COLUMN  directory_path  HEADING  'Directory Path'

SELECT owner,
       directory_name,
       directory_path
  FROM dba_directories
 ORDER BY 1,
          2;

host echo start collect......Job Informaion... 

prompt <a name="job"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Job</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE "border='1' width='90%' align='center' summary='Script output'"

COLUMN  job        HEADING  'Job Name'
COLUMN  priv_user  HEADING  'Priv User'
COLUMN  what       HEADING  'What'
COLUMN  status     HEADING  'Status'
COLUMN  next_date  HEADING  'Next Date'
COLUMN  interval   HEADING  'Interval'

SELECT job,
       priv_user,
       what,
       decode(broken,
              'Y',
              '<font color="red"><b>Broken</b></font>',
              '<font color="green"><b>Normal</b></font>') status,
       to_char(next_date,
               'YYYY-MM-DD HH24:MI:SS') next_date,
       INTERVAL
  FROM dba_jobs;

prompt <a name="scheduler_jobs"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● DBA Scheduler Jobs </b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE "border='1' width='90%' align='center' summary='Script output'"

SELECT j.owner,
       j.job_name,
       j.state,
       j.job_type,
       j.job_action,
       j.repeat_interval,
       to_char(j.start_date,
               'YYYY-MM-DD HH24:mi:ss') start_date,
       to_char(j.end_date,
               'YYYY-MM-DD HH24:mi:ss') end_date,
       to_char(j.next_run_date,
               'YYYY-MM-DD HH24:mi:ss') next_run_date,
       to_char(j.last_start_date,
               'YYYY-MM-DD HH24:mi:ss') last_start_date,
       (j.last_run_duration) last_run_duration,
       j.run_count
  FROM dba_scheduler_jobs j
  LEFT OUTER JOIN dba_scheduler_running_jobs rj
    ON j.job_name = rj.job_name
  LEFT OUTER JOIN gv$session b
    ON (rj.session_id = b.sid AND rj.running_instance = b.inst_id)
 ORDER BY b.inst_id,
          j.state,
          j.owner,
          j.job_name;

prompt <a name="jobs_info_errors"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Job Error Information</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE "border='1' width='90%' align='center' summary='Script output'"

SELECT *
  FROM (SELECT n.owner,
               n.log_id,
               n.job_name,
               n.job_class,
               to_char(n.log_date,
                       'YYYY-MM-DD HH24:mi:ss') log_date,
               n.operation,
               n.status,
               jrd.error#,
               jrd.run_duration,
               to_char(jrd.actual_start_date,
                       'YYYY-MM-DD HH24:mi:ss') actual_start_date,
               jrd.instance_id,
               jrd.session_id,
               jrd.slave_pid,
               n.additional_info log_additional_info,
               jrd.additional_info detail_additional_info,
               dense_rank() over(PARTITION BY n.owner, n.job_name ORDER BY n.log_id DESC) rank_order
          FROM dba_scheduler_job_log         n,
               dba_scheduler_job_run_details jrd
         WHERE n.log_id = jrd.log_id(+)
           AND n.status <> 'SUCCEEDED'
           AND n.job_name NOT LIKE 'ORA$AT_OS_OPT_SY%'
           AND n.log_date >= SYSDATE - 7
         ORDER BY n.log_date DESC)
 WHERE rank_order <= 3;

host echo start collect......Database Link Informaion... 

prompt <a name="dblink"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Database Link</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE "border='1' width='90%' align='center' summary='Script output'"

COLUMN  owner     HEADING  'Owner'
COLUMN  db_link   HEADING  'DBLink'
COLUMN  username  HEADING  'Username'
COLUMN  host      HEADING  'Host'

SELECT owner,
       db_link,
       username,
       host
  FROM dba_db_links
 ORDER BY 1,
          2;

host echo start collect......Autotask Informaion... 

prompt <a name="autotask"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Autotask</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="autotasks" border="1" width="90%" align="center" summary="Script output" '

COLUMN job_name     FORMAT A50  HEADING 'Task Name'
COLUMN status       FORMAT A50  HEADING 'Task Status'

SELECT job_name,
       CASE
         WHEN job_name = 'GATHER_STATS_JOB'
              AND state != 'SCHEDULED' THEN
          '<font color="red"><b>' || state || '</b></font>'
         WHEN job_name = 'AUTO_SPACE_ADVISOR_JOB'
              AND state = 'SCHEDULED' THEN
          '<font color="red"><b>' || state || '</b></font>'
         ELSE
          '<font color="green"><b>' || state || '</b></font>'
       END status
  FROM dba_scheduler_jobs
  WHERE job_name in ('GATHER_STATS_JOB','AUTO_SPACE_ADVISOR_JOB')
 ORDER BY 1;

prompt <a name="autotask_windows"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Autotask Job Windows</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE "border='1' width='90%' align='center' summary='Script output'"

COLUMN  window_name      HEADING  'Window Name'
COLUMN  schedule_type    HEADING  'Schedule Type'
COLUMN  repeat_interval  HEADING  'Repeat Interval'
COLUMN  duration         HEADING  'Duration'
COLUMN  next_start_date  HEADING  'Next Start Date'
COLUMN  last_start_date  HEADING  'Last Start Date'
COLUMN  enabled          HEADING  'Enabled'

SELECT w.window_name,
       w.schedule_type,
       w.repeat_interval,
       w.duration,
       w.next_start_date,
       w.last_start_date,
       CASE
         WHEN w.enabled = 'TRUE' THEN
          '<div align="left"><font color="green"><b>' || w.enabled || '</b></font></div>'
         ELSE
          '<div align="left"><font color="green"><b>' || w.enabled || '</b></font></div>'
       END enabled
  FROM dba_scheduler_windows w;

-- +====================================================================================================================+
-- |
-- | <<<<<     OverView Database of Backup and Recover Information     >>>>>                                         |
-- |                                                                                                                    |
-- +====================================================================================================================+

host echo start...OverView Database of Backup and Recover Information... 

prompt <a name="10"></a>
prompt <font size="+2" face="Consolas" color="#336699"><b>Backup and DataGuard</b></font>

host echo start collect......Dataguard Parameter... 

prompt <a name="dg_para"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Dataguard Parameter</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="dgparas" border="1" width="90%" align="center" summary="Script output" '

COLUMN  name       HEADING  'Parameter Name'
COLUMN  paravalue  HEADING  'Parameter Value'

SELECT v.name,
       CASE
         WHEN v.name = 'standby_file_management'
              AND v.value = 'MANUAL' THEN
          '<font color="red"><b>' || v.value || '</b></font>'
         ELSE
          to_char(v.value)
       END paravalue
  FROM (SELECT NAME,
               VALUE
          FROM v$parameter
         WHERE NAME IN ('log_archive_config',
                        'fal_client',
                        'fal_server',
                        'standby_file_management',
                        'redo_transport_user')
           AND VALUE IS NOT NULL
        UNION ALL
        SELECT p1.name || ' ' || upper(p2.value) NAME,
               p1.value
          FROM v$parameter p1,
               v$parameter p2
         WHERE substr(p1.name,
                      -2) = substr(p2.name,
                                   -2)
           AND p1.name LIKE 'log_archive_dest_%'
           AND p1.value IS NOT NULL
           AND p2.name LIKE 'log_archive_dest_state_%'
           AND (p1.name NOT LIKE 'log_archive_dest_state_%' AND p1.value IS NOT NULL)) v;

host echo start collect......Dataguard Applied Status... 

prompt <a name="dgapply_info"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Dataguard Apllied Status</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="dgapply" border="1" width="90%" align="center" summary="Script output" '

COLUMN  inst_id            HEADING  'ID'
COLUMN  protection_mode    HEADING  'Protection Mode'
COLUMN  switchover_status  HEADING  'Switchover Status'
COLUMN  applog             HEADING  'Applied Log'
COLUMN  nowlog             HEADING  'Now Log'

-- V$ARCHIVED_LOG.APPLIED is not an authoritative apply-lag metric on a
-- primary database.  Keep this legacy sequence table uncolored and use the
-- destination-status section below for the primary-side health assessment.
SELECT (select inst_id from gv$instance where thread# = a.thread#) inst_id,
       c.protection_mode,
       c.switchover_status,
       a.applog,
       b.nowlog
  FROM (SELECT thread#,
               MAX(sequence#) applog
          FROM v$archived_log
         WHERE applied = 'YES'
         GROUP BY thread#) a,
       (SELECT thread#,
               MAX(sequence#) nowlog
          FROM v$log
         GROUP BY thread#) b,
       (SELECT open_mode,
               protection_mode,
               database_role,
               switchover_status
          FROM v$database) c
 WHERE a.thread# = b.thread#
 ORDER BY a.thread#;

host echo start collect......Dataguard Status... 

prompt <a name="dg_stats"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Dataguard Status</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="dgdeststat" border="1" width="90%" align="center" summary="Script output" '

COLUMN  inst_id                 HEADING  'ID'
COLUMN  dest_name               HEADING  'Name'
COLUMN  status                  HEADING  'Status'
COLUMN  type                    HEADING  'Type'
COLUMN  database_mode           HEADING  'Database Mode'
COLUMN  synchronization_status  HEADING  'GAP Status'
COLUMN  apply_seq_gap           HEADING  'Apply Seq Gap'
COLUMN  archived_thread#        HEADING  'Archived Thread'
COLUMN  archived_seq#           HEADING  'Archived Seq'
COLUMN  applied_thread#         HEADING  'Applied Thread'
COLUMN  applied_seq#            HEADING  'Applied Seq'
COLUMN  delay_mins              HEADING  'Delay Mins'
COLUMN  error                   HEADING  'Error'

SELECT s.inst_id,
       s.dest_name,
       CASE
         WHEN NVL(s.status, 'UNKNOWN') <> 'VALID' THEN
          '<font color="red"><b>' || NVL(s.status, 'UNKNOWN') || '</b></font>'
         ELSE
          '<font color="green"><b>' || s.status || '</b></font>'
       END status,
       s.TYPE,
       decode(s.database_mode,
               'OPEN_READ-ONLY',
               'ADG',
               'DG') database_mode,
       CASE
         WHEN NVL(s.synchronization_status, 'UNKNOWN') = 'CHECK CONFIGURATION' AND
              s.protection_mode = 'MAXIMUM PERFORMANCE' THEN
          '<font color="green"><b>OK</b></font>'
         WHEN NVL(s.synchronization_status, 'UNKNOWN') <> 'OK' THEN
          '<font color="red"><b>' || NVL(s.synchronization_status, 'UNKNOWN') || '</b></font>'
         ELSE
          '<font color="green"><b>' || s.synchronization_status || '</b></font>'
       END synchronization_status,
       CASE
         WHEN s.archived_thread# IS NULL OR s.applied_thread# IS NULL OR
              s.archived_seq# IS NULL OR s.applied_seq# IS NULL THEN
          NULL
         WHEN s.archived_thread# <> s.applied_thread# THEN
          NULL
         ELSE
          GREATEST(s.archived_seq# - s.applied_seq#, 0)
       END apply_seq_gap,
       s.archived_thread#,
       s.archived_seq#,
       s.applied_thread#,
       s.applied_seq#,
       d.delay_mins,
       CASE
         WHEN s.error IS NOT NULL THEN
          '<font color="red"><b>' || s.error || '</b></font>'
         ELSE
          s.error
       END error
  FROM gv$archive_dest_status s
  LEFT JOIN gv$archive_dest d
    ON d.inst_id = s.inst_id
   AND d.dest_id = s.dest_id
 WHERE s.TYPE = 'PHYSICAL'
   AND NVL(s.status, 'UNKNOWN') <> 'INACTIVE'
 ORDER BY s.inst_id,
          s.dest_id;

host echo start collect......RMAN Backup Info... 

prompt <a name="rmanbackup_info"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● RMAN Backup Info</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="rmaninfo" border="1" width="90%" align="center" summary="Script output" '

COLUMN  command_id          HEADING  'Start Time'
COLUMN  time_taken_display  HEADING  'Elapsed(Hour)'
COLUMN  status              HEADING  'Status'
COLUMN  input_type          HEADING  'Input Type'
COLUMN  output_device_type  HEADING  'Output Type'
COLUMN  input_bytes         HEADING  'Input Size'
COLUMN  output_bytes        HEADING  'Output Size'
COLUMN  output_size         HEADING  'Output(Sec)'

SELECT command_id,
       time_taken_display,
       decode(status,
              'COMPLETED',
              '<font color="green"><b>' || status || '</b></font>',
              'RUNNING',
              '<font color="green"><b>' || status || '</b></font>',
              '<font color="red"><b>' || status || '</b></font>') status,
       input_type,
       output_device_type,
       TRIM(input_bytes_display) input_bytes,
       TRIM(output_bytes_display) output_bytes,
       TRIM(output_bytes_per_sec_display) output_size
  FROM v$rman_backup_job_details
 WHERE SYSDATE - start_time <= 14
   AND input_type in ('DB INCR','DB FULL')
 ORDER BY start_time DESC;

prompt <a name="datapump_info"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Orphaned Datapump Job</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE "border='1' width='90%' align='center' summary='Script output'"

COLUMN  owner_name         HEADING  'Owner'
COLUMN  job_name           HEADING  'Job Name'
COLUMN  operation          HEADING  'Operation'
COLUMN  job_mode           HEADING  'Job Mode'
COLUMN  state              HEADING  'State'
COLUMN  attached_sessions  HEADING  'Attached Sessions'

SELECT owner_name,
       job_name,
       operation,
       job_mode,
       CASE
         WHEN state != 'NOT RUNNING' THEN
          '<font color="red"><b>' || state || '</b></font>'
         ELSE
          state
       END state,
       attached_sessions
  FROM dba_datapump_jobs
 WHERE job_name NOT LIKE 'BIN$%'
 ORDER BY 1,
          2;


-- +====================================================================================================================+
-- |
-- | <<<<<     OverView Database of ASM Information     >>>>>                                         |
-- |                                                                                                                    |
-- +====================================================================================================================+

host echo start...OverView Database of ASM Information... 

host echo start collect......ASM Disk Group... 

prompt <a name="asmdiskgroup_usage"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● ASM Diskgroup Usage</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="asmdiskinfo" border="1" width="90%" align="center" summary="Script output" '

COLUMN  group_name                             HEADING  'DiskGroup Name'
COLUMN  au_size                                HEADING  'AU Size'
COLUMN  state                                  HEADING  'State'
COLUMN  type                                   HEADING  'Type'
COLUMN  total_gb        FORMAT 999,999,990.99  HEADING  'Total Size(GB)'
COLUMN  free_gb         FORMAT 999,999,990.99  HEADING  'Free Size(GB)'
COLUMN  usable_file_gb  FORMAT 999,999,990.99  HEADING  'Usable Size(GB)'
COLUMN  offline_disks                          HEADING  'Offline Disks'
COLUMN  used                                   HEADING  'Percent(%)'

SELECT '<font color="blue"><b>' || NAME || '</b></font>' group_name,
       allocation_unit_size / 1024 / 1024 au_size,
       '<font color="green"><b>' || state || '</b></font>' state,
       TYPE,
       total_mb/1024 total_gb,
       free_mb/1024 free_gb,
       usable_file_mb/1024 usable_file_gb,
       CASE
         WHEN offline_disks > 0 THEN
          '<div align="right"><font color="red"><b>' || offline_disks || '</b></font></div>'
         ELSE
          '<div align="right"><font color="green"><b>' || offline_disks || '</b></font></div>'
       END offline_disks,
       CASE
         WHEN (total_mb - free_mb) / total_mb * 100 >= 90 THEN
          '<div align="right"><font color="red"><b>' || TRIM(to_char((total_mb - free_mb) / total_mb * 100,
                                                                     '990.99')) || '</b></font></div>'
         ELSE
          '<div align="right"><font color="green"><b>' || TRIM(to_char((total_mb - free_mb) / total_mb * 100,
                                                                       '990.99')) || '</b></font></div>'
       END used
  FROM v$asm_diskgroup;

-- +====================================================================================================================+
-- |
-- | <<<<<     OverView Database Performace Information     >>>>>                                         |
-- |                                                                                                                    |
-- +====================================================================================================================+

host echo start...OverView Database Performace Information... 

prompt <a name="50"></a>
prompt <font size="+2" face="Consolas" color="#336699"><b>Performace Information</b></font>

host echo start collect......AWR Configure Informaion... 

prompt <a name="awraux_check"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● AWR and SYSAUX Check</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="awrcount" border="1" width="90%" align="center" summary="Script output" '

COLUMN  min_id    HEADING  'SnapId(Min)'
COLUMN  max_id    HEADING  'SnapId(Max)'
COLUMN  cnt_snap  HEADING  'Snap Count'

SELECT MIN(snap_id) min_id,
       MAX(snap_id) max_id,
       CASE
         WHEN COUNT(DISTINCT snap_id) > 1000 THEN
          '<div align="right"><font color="red"><b>' || COUNT(DISTINCT snap_id) || '</b></font></div>'
         ELSE
          '<div align="right"><font color="green"><b>' || COUNT(DISTINCT snap_id) || '</b></font></div>'
       END cnt_snap
  FROM dba_hist_sysmetric_summary;

prompt <a name="awrsnap_rate"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Awrsnap Rate</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE OFF
SET MARKUP html TABLE 'id="awrrate" border="1" width="90%" align="center" summary="Script output" '

COLUMN  dbid            HEADING  'DBID'
COLUMN  snap_int_min    HEADING  'Snap Interval(Min)'
COLUMN  retention_days  HEADING  'Retention(Day)'

SELECT v.dbid,
       CASE
         WHEN v.snap_int_min < 15 THEN
          '<div align="right"><font color="red"><b>' || v.snap_int_min || '</b></font></div>'
         ELSE
          '<div align="right">' || v.snap_int_min || '</div>'
       END snap_int_min,
       CASE
         WHEN v.retention_days > 60 THEN
          '<div align="right"><font color="red"><b>' || v.retention_days || '</b></font></div>'
         ELSE
          '<div align="right">' || v.retention_days || '</div>'
       END retention_days
  FROM (SELECT dbid,
               extract(hour FROM snap_interval) * 60 + extract(minute FROM snap_interval) snap_int_min,
               extract(DAY FROM retention) retention_days
          FROM dba_hist_wr_control) v;

host echo start collect......Awrrpt Snap Informaion... 

prompt <a name="awrsnap_info"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Awrsnap Info</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE ON
SET MARKUP html TABLE 'id="awrinfo" border="1" width="90%" align="center" summary="Script output" '

COLUMN  name       HEADING  'Name'
COLUMN  snap_id    HEADING  'Snap Id'
COLUMN  snap_time  HEADING  'Snap Time'
COLUMN  sessions   HEADING  'Sessions'
COLUMN  cursos     HEADING  'Cursors/Session'

SELECT v.inst_id,
       v.name,
       '<div align="right">' || v.snap_id || '</div>' snap_id,
       CASE
         WHEN v.name IN ('DB Time:',
                         'Elapsed:') THEN
          '<div align="right">' ||
          TRIM(TO_CHAR(v.snap_minutes, '999,999,990.99')) ||
          ' (mins)</div>'
         ELSE
          '<div align="right">' || v.snap_time || '</div>'
       END snap_time,
       decode(v.name,
               'DB Time:',
               CASE
                 WHEN v.snap_minutes / NULLIF(&_cpus, 0) / NULLIF(&_snap_int_min, 0) * 100 > 80 THEN
                  '<div align="right"><font color="red"><b>' || TRIM(to_char(v.snap_minutes / NULLIF(&_cpus, 0) / NULLIF(&_snap_int_min, 0) * 100,
                                                        '999,999,990.99')) || '%</b></font></div>'
                 ELSE
                  '<div align="right"><font color="green"><b>' || TRIM(to_char(v.snap_minutes / NULLIF(&_cpus, 0) / NULLIF(&_snap_int_min, 0) * 100,
                                                        '999,999,990.99')) || '%</b></font></div>'
              END,
              '<div align="right">' || v.sessions || '</div>') sessions,
       round(v.cursos,
             1) cursos
  FROM (SELECT '1' id,
               a.instance_number inst_id,
               'Begin Snap:' NAME,
               to_char(a.snap_id) snap_id,
               to_char(a.end_interval_time,
                       'yyyy-mm-dd hh24:mi:ss') snap_time,
               CAST(NULL AS NUMBER) snap_minutes,
               b.value sessions,
               c.value / b.value cursos
          FROM dba_hist_snapshot a,
               (SELECT snap_id,
                       instance_number,
                       VALUE
                  FROM dba_hist_sysstat
                 WHERE stat_name = 'logons current') b,
               (SELECT snap_id,
                       instance_number,
                       VALUE
                  FROM dba_hist_sysstat
                 WHERE stat_name = 'opened cursors current') c
         WHERE a.snap_id = b.snap_id
           AND a.snap_id = c.snap_id
           AND a.instance_number = b.instance_number
           AND a.instance_number = c.instance_number
           AND a.snap_id = &_snap_beg
        UNION ALL
        SELECT '2' id,
               a.instance_number inst_id,
               'End Snap:' NAME,
               to_char(a.snap_id) snap_id,
               to_char(a.end_interval_time,
                       'yyyy-mm-dd hh24:mi:ss') snap_time,
               CAST(NULL AS NUMBER) snap_minutes,
               b.value sessions,
               c.value / b.value cursos
          FROM dba_hist_snapshot a,
               (SELECT snap_id,
                       instance_number,
                       VALUE
                  FROM dba_hist_sysstat
                 WHERE stat_name = 'logons current') b,
               (SELECT snap_id,
                       instance_number,
                       VALUE
                  FROM dba_hist_sysstat
                 WHERE stat_name = 'opened cursors current') c
         WHERE a.snap_id = b.snap_id
           AND a.snap_id = c.snap_id
           AND a.instance_number = b.instance_number
           AND a.instance_number = c.instance_number
           AND a.snap_id = &_snap_end
        UNION ALL
        SELECT '3' id,
               b.instance_number inst_id,
               'Elapsed:' NAME,
               '' snap_id,
               CAST(NULL AS VARCHAR2(20)) snap_time,
               extract(DAY FROM e.end_interval_time - b.end_interval_time) * 1440 +
               extract(hour FROM e.end_interval_time - b.end_interval_time) * 60 +
               extract(minute FROM e.end_interval_time - b.end_interval_time) +
               extract(SECOND FROM e.end_interval_time - b.end_interval_time) / 60 snap_minutes,
               NULL sessions,
               NULL cursos
          FROM dba_hist_snapshot b,
               dba_hist_snapshot e
         WHERE b.snap_id = &_snap_beg
           AND e.snap_id = &_snap_end
           AND b.instance_number = e.instance_number
           AND b.startup_time = e.startup_time
           AND b.end_interval_time < e.end_interval_time
        UNION ALL
        SELECT '4' id,
               b.instance_number inst_id,
               'DB Time:' NAME,
               '' snap_id,
               CAST(NULL AS VARCHAR2(20)) snap_time,
               (e.value - b.value) / 1000000 / 60 snap_minutes,
               NULL sessions,
               NULL cursos
          FROM dba_hist_sys_time_model b,
               dba_hist_sys_time_model e
         WHERE b.dbid(+) = e.dbid
           AND b.instance_number(+) = e.instance_number
           AND b.snap_id = &_snap_beg
           AND e.snap_id = &_snap_end
           AND b.stat_id = e.stat_id
           AND b.stat_name = 'DB time'
         ORDER BY inst_id,
                  id) v;

host echo start collect......Awrrpt Load Profile Informaion... 

prompt <a name="load_profile"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Load Profile</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE ON
SET MARKUP html TABLE 'id="loadprofile" border="1" width="90%" align="center" summary="Script output" '

COLUMN  statistic_name  HEADING 'Statistic Name'
COLUMN  value           HEADING 'Per Second'

WITH st AS
 (SELECT b.instance_number inst_id,
         extract(DAY FROM e.end_interval_time - b.end_interval_time) * 86400 + extract(hour FROM e.end_interval_time - b.end_interval_time) * 3600 +
         extract(minute FROM e.end_interval_time - b.end_interval_time) * 60 + extract(SECOND FROM e.end_interval_time - b.end_interval_time) snaptime
    FROM dba_hist_snapshot b,
         dba_hist_snapshot e
   WHERE b.snap_id = &_snap_beg
     AND e.snap_id = &_snap_end
     AND b.instance_number = e.instance_number),
sysstat AS
 (SELECT b.instance_number inst_id,
         b.stat_name,
         e.value - b.value VALUE
    FROM dba_hist_sysstat b,
         dba_hist_sysstat e
   WHERE b.dbid(+) = e.dbid
     AND b.instance_number(+) = e.instance_number
     AND b.stat_id = e.stat_id
     AND b.snap_id = &_snap_beg
     AND e.snap_id = &_snap_end),
systimemodel AS
 (SELECT b.instance_number inst_id,
         b.stat_name,
         (e.value - b.value) / 1000000 VALUE
    FROM dba_hist_sys_time_model b,
         dba_hist_sys_time_model e
   WHERE b.dbid(+) = e.dbid
     AND b.instance_number(+) = e.instance_number
     AND b.snap_id = &_snap_beg
     AND e.snap_id = &_snap_end
     AND b.stat_id = e.stat_id)
SELECT v.inst_id,
       v.statistic_name,
       CASE
         WHEN v.statistic_name IN ('Hard parses (SQL)',
                                   'Logons')
              AND v.value > 50 THEN
          '<div align="left"><font color="red"><b>' || to_char(v.value,
                                                                '999,999,999,999,999,990.9') || '</b></font></div>'
         ELSE
          '<div align="left">' || to_char(v.value,
                                           '999,999,999,999,999,990.9') || '</div>'
       END VALUE
  FROM (SELECT 1 id,
               st.inst_id,
               'DB Time(s)' statistic_name,
               round(systimemodel.value / st.snaptime,
                     2) VALUE
          FROM st,
               systimemodel
         WHERE st.inst_id = systimemodel.inst_id
           AND systimemodel.stat_name = 'DB time'
        UNION ALL
        SELECT 2 id,
               st.inst_id,
               'DB CPU(s)' statistic_name,
               round(systimemodel.value / st.snaptime,
                     2) VALUE
          FROM st,
               systimemodel
         WHERE st.inst_id = systimemodel.inst_id
           AND systimemodel.stat_name = 'DB CPU'
        UNION ALL
        SELECT 3 id,
               st.inst_id,
               'Redo size (bytes)' statistic_name,
               round(sysstat.value / st.snaptime,
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'redo size'
        UNION ALL
        SELECT 4 id,
               st.inst_id,
               'Logical read (blocks)' statistic_name,
               round(sysstat.value / st.snaptime,
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'session logical reads'
        UNION ALL
        SELECT 5 id,
               st.inst_id,
               'Block changes' statistic_name,
               round(sysstat.value / st.snaptime,
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'db block changes'
        UNION ALL
        SELECT 6 id,
               st.inst_id,
               'Physical read (blocks)' statistic_name,
               round(sysstat.value / st.snaptime,
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'physical reads'
        UNION ALL
        SELECT 7 id,
               st.inst_id,
               'Physical write (blocks)' statistic_name,
               round(sysstat.value / st.snaptime,
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'physical writes'
        UNION ALL
        SELECT 8 id,
               st.inst_id,
               'User calls' statistic_name,
               round(sysstat.value / st.snaptime,
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'user calls'
        UNION ALL
        SELECT 9 id,
               st.inst_id,
               'Parses (SQL)' statistic_name,
               round(sysstat.value / st.snaptime,
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'parse count (total)'
        UNION ALL
        SELECT 10 id,
               st.inst_id,
               'Hard parses (SQL)' statistic_name,
               round(sysstat.value / st.snaptime,
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'parse count (hard)'
        UNION ALL
        SELECT 11 id,
               st.inst_id,
               'Sorts' statistic_name,
               round(SUM(sysstat.value / st.snaptime),
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name IN ('sorts (disk)',
                                     'sorts (memory)')
         GROUP BY st.inst_id
        UNION ALL
        SELECT 12 id,
               st.inst_id,
               'Logons' statistic_name,
               round(sysstat.value / st.snaptime,
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'logons cumulative'
        UNION ALL
        SELECT 13 id,
               st.inst_id,
               'Executes (SQL)' statistic_name,
               round(sysstat.value / st.snaptime,
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'execute count'
        UNION ALL
        SELECT 14 id,
               st.inst_id,
               'Rollbacks' statistic_name,
               round(sysstat.value / st.snaptime,
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'user rollbacks'
        UNION ALL
        SELECT 15 id,
               st.inst_id,
               'Transactions' statistic_name,
               round(SUM(sysstat.value / st.snaptime),
                     2) VALUE
          FROM st,
               sysstat
         WHERE st.inst_id = sysstat.inst_id
           AND sysstat.stat_name IN ('user rollbacks',
                                     'user commits')
         GROUP BY st.inst_id
         ORDER BY inst_id,
                  id) v;

host echo start collect......Instance Efficiency Percentages... 

prompt <a name="insteffi"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Instance Efficiency Percentages (Target 100%)</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE ON
SET MARKUP html TABLE 'id="insteffi" border="1" width="90%" align="center" summary="Script output" '

COLUMN  statistic_name  HEADING 'Statistic Name'
COLUMN  value           HEADING 'Percent(%)'

WITH sysstat AS
 (SELECT b.instance_number inst_id,
         b.stat_name,
         e.value - b.value VALUE
    FROM dba_hist_sysstat b,
         dba_hist_sysstat e
   WHERE b.dbid(+) = e.dbid
     AND b.instance_number(+) = e.instance_number
     AND b.stat_id = e.stat_id
     AND b.snap_id = &_snap_beg
     AND e.snap_id = &_snap_end),
systimemodel AS
 (SELECT b.instance_number inst_id,
         b.stat_name,
         (e.value - b.value) / 10000 VALUE
    FROM dba_hist_sys_time_model b,
         dba_hist_sys_time_model e
   WHERE b.dbid(+) = e.dbid
     AND b.instance_number(+) = e.instance_number
     AND b.snap_id = &_snap_beg
     AND e.snap_id = &_snap_end
     AND b.stat_id = e.stat_id),
waitstat AS
 (SELECT b.instance_number inst_id,
         SUM(e.wait_count - b.wait_count) VALUE
    FROM dba_hist_waitstat b,
         dba_hist_waitstat e
   WHERE b.dbid(+) = e.dbid
     AND b.instance_number(+) = e.instance_number
     AND b.snap_id = &_snap_beg
     AND e.snap_id = &_snap_end
   GROUP BY b.instance_number),
librarycache AS
 (SELECT b.instance_number inst_id,
         round(100 * (SUM(e.pinhits) - SUM(b.pinhits)) / (SUM(e.pins) - SUM(b.pins)),
               2) VALUE
    FROM dba_hist_librarycache b,
         dba_hist_librarycache e
   WHERE b.dbid(+) = e.dbid
     AND b.instance_number(+) = e.instance_number
     AND b.snap_id = &_snap_beg
     AND e.snap_id = &_snap_end
   GROUP BY b.instance_number),
latch AS
 (SELECT b.instance_number inst_id,
         round(100 * (1 - (SUM(e.misses) - SUM(b.misses)) / (SUM(e.gets) - SUM(b.gets))),
               2) VALUE
    FROM dba_hist_latch b,
         dba_hist_latch e
   WHERE b.dbid(+) = e.dbid
     AND b.instance_number(+) = e.instance_number
     AND b.snap_id = &_snap_beg
     AND e.snap_id = &_snap_end
   GROUP BY b.instance_number)
SELECT v.inst_id,
       v.statistic_name,
       CASE
         WHEN v.value < 90 THEN
          '<div align="left"><font color="red"><b>' || to_char(v.value,
                                                                '990.99') || '</b></font></div>'
         ELSE
          '<div align="left">' || to_char(v.value,
                                           '990.99') || '</div>'
       END VALUE
  FROM (SELECT 1 id,
               sysstat.inst_id,
               'Buffer Nowait %' AS statistic_name,
               round(100 * (1 - waitstat.value / sysstat.value),
                     2) VALUE
          FROM waitstat,
               sysstat
         WHERE waitstat.inst_id = sysstat.inst_id
           AND sysstat.stat_name = 'session logical reads'
        UNION ALL
        SELECT 2 id,
               a.inst_id,
               'Redo NoWait %' AS statistic_name,
               round(100 * (1 - a.value / b.value),
                     2) VALUE
          FROM sysstat a,
               sysstat b
         WHERE a.inst_id = b.inst_id
           AND a.stat_name = 'redo log space requests'
           AND b.stat_name = 'redo entries'
        UNION ALL
        SELECT 3 id,
               a.inst_id,
               'Buffer Hit %' AS statistic_name,
               round(100 * (1 - (a.value - b.value - nvl(c.value,
                                                         0)) / d.value),
                     2) VALUE
          FROM sysstat a,
               sysstat b,
               sysstat c,
               sysstat d
         WHERE a.inst_id = b.inst_id
           AND b.inst_id = c.inst_id
           AND c.inst_id = d.inst_id
           AND a.stat_name = 'physical reads'
           AND b.stat_name = 'physical reads direct'
           AND c.stat_name = 'physical reads direct (lob)'
           AND d.stat_name = 'session logical reads'
        UNION ALL
        SELECT 4 id,
               a.inst_id,
               'In-memory Sort %' AS statistic_name,
               round(100 * a.value / (a.value + b.value),
                     2) VALUE
          FROM sysstat a,
               sysstat b
         WHERE a.inst_id = b.inst_id
           AND a.stat_name = 'sorts (memory)'
           AND b.stat_name = 'sorts (disk)'
        UNION ALL
        SELECT 5 id,
               inst_id,
               'Library Hit %' AS statistic_name,
               VALUE
          FROM librarycache
        UNION ALL
        SELECT 6 id,
               a.inst_id,
               'Soft Parse %' AS statistic_name,
               round(100 * (1 - a.value / b.value),
                     2) VALUE
          FROM sysstat a,
               sysstat b
         WHERE a.inst_id = b.inst_id
           AND a.stat_name = 'parse count (hard)'
           AND b.stat_name = 'parse count (total)'
        UNION ALL
        SELECT 7 id,
               a.inst_id,
               'Execute to Parse %' AS statistic_name,
               round(100 * (1 - a.value / b.value),
                     2) VALUE
          FROM sysstat a,
               sysstat b
         WHERE a.inst_id = b.inst_id
           AND a.stat_name = 'parse count (total)'
           AND b.stat_name = 'execute count'
        UNION ALL
        SELECT 8 id,
               inst_id,
               'Latch Hit %' AS statistic_name,
               VALUE
          FROM latch
        UNION ALL
        SELECT 9 id,
               a.inst_id,
               'Parse CPU to Parse Elapsd %' AS statistic_name,
               decode(b.value,0,to_number(null),round(100 * a.value / b.value,2)) VALUE
          FROM sysstat a,
               sysstat b
         WHERE a.inst_id = b.inst_id
           AND a.stat_name = 'parse time cpu'
           AND b.stat_name = 'parse time elapsed'
        UNION ALL
        SELECT 10 id,
               a.inst_id,
               '% Non-Parse CPU' AS statistic_name,
               round(100 * (1 - a.value / b.value),
                     2) VALUE
          FROM sysstat      a,
               systimemodel b
         WHERE a.inst_id = b.inst_id
           AND a.stat_name = 'parse time cpu'
           AND b.stat_name = 'DB CPU'
         ORDER BY inst_id,
                  id) v;

host echo start collect......TOP 10 Wait Event... 

prompt <a name="top10_event"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● TOP 10 Wait Event</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE ON
SET MARKUP html TABLE 'id="top10event" border="1" width="90%" align="center" summary="Script output" '

COLUMN  event                                        HEADING 'Event'
COLUMN  waits       FORMAT  999,999,999,999,999,999  HEADING 'Waits'
COLUMN  times                                        HEADING 'Total Wait Time (sec)'
COLUMN  avwait                                       HEADING 'Wait Avg(ms)'
COLUMN  pct         FORMAT  990.9                    HEADING '% DB time'
COLUMN  wait_class                                   HEADING 'Wait Class'

WITH systimemodel AS
 (SELECT b.instance_number inst_id,
         b.stat_name,
         (e.value - b.value) VALUE
    FROM dba_hist_sys_time_model b,
         dba_hist_sys_time_model e
   WHERE b.dbid(+) = e.dbid
     AND b.instance_number(+) = e.instance_number
     AND b.snap_id = &_snap_beg
     AND e.snap_id = &_snap_end
     AND b.stat_id = e.stat_id)
SELECT inst_id,
       event,
       waits,
       '<div align="right">' || to_char(times,
                                        '999,999,999,999,999,990.9') || '</div>' times,
       CASE
         WHEN (avwait > 10 AND times > 1000) THEN
          '<div align="right"><font color="red"><b>' || to_char(avwait,
                                                                '999,999,999,999,999,999') || '</b></font></div>'
         ELSE
          '<div align="right">' || to_char(avwait,
                                           '999,999,999,999,999,999') || '</div>'
       END avwait,
       pct,
       '<div align="right">' || wait_class || '</div>' wait_class
  FROM (SELECT inst_id,
               event,
               wtfg waits,
               tmfg / 1000000 times,
               decode(wtfg,
                      0,
                      to_number(NULL),
                      tmfg / wtfg) / 1000 avwait,
               (SELECT decode(systimemodel.value,
                              0,
                              NULL,
                              tmfg / (SELECT systimemodel.value
                                        FROM systimemodel
                                       WHERE systimemodel.inst_id = v.inst_id
                                         AND systimemodel.stat_name = 'DB time'))
                  FROM systimemodel
                 WHERE systimemodel.inst_id = v.inst_id
                   AND systimemodel.stat_name = 'DB time') * 100 pct,
               wait_class,
               row_number() over(PARTITION BY inst_id ORDER BY tmfg DESC) rank
          FROM (SELECT b.instance_number inst_id,
                       e.event_name event,
                       (e.total_waits - nvl(b.total_waits,
                                                                                    0)) - greatest(0,
                                                                                                   (nvl(ebg.total_waits,
                                                                                                        0) - nvl(bbg.total_waits,
                                                                                                                  0))) wtfg,
                                                            (e.total_timeouts - nvl(b.total_timeouts,
                                                                                       0)) - greatest(0,
                                                                                                      (nvl(ebg.total_timeouts,
                                                                                                           0) - nvl(bbg.total_timeouts,
                                                                                                                     0))) ttofg,
                                                            (e.time_waited_micro - nvl(b.time_waited_micro,
                                                                                          0)) -
                                                               greatest(0,
                                                                        (nvl(ebg.time_waited_micro,
                                                                             0) - nvl(bbg.time_waited_micro,
                                                                                       0))) tmfg,
                       e.wait_class
                  FROM dba_hist_system_event     b,
                       dba_hist_system_event     e,
                       dba_hist_bg_event_summary bbg,
                       dba_hist_bg_event_summary ebg
                 WHERE b.snap_id = bbg.snap_id
                   AND e.snap_id = ebg.snap_id
                   AND b.snap_id = &_snap_beg
                   AND e.snap_id = &_snap_end
                   AND e.dbid = b.dbid(+)
                   AND e.instance_number = b.instance_number(+)
                   AND e.event_id = b.event_id(+)
                   AND e.dbid = ebg.dbid(+)
                   AND e.instance_number = ebg.instance_number(+)
                   AND e.event_id = ebg.event_id(+)
                   AND e.dbid = bbg.dbid(+)
                   AND e.instance_number = bbg.instance_number(+)
                   AND e.event_id = bbg.event_id(+)
                   AND e.total_waits > nvl(b.total_waits,
                                           0)
                   AND e.wait_class <> 'Idle'
                UNION ALL
                SELECT systimemodel.inst_id inst_id,
                       'DB CPU' event,
                       to_number(NULL) wtfg,
                       to_number(NULL) ttofg,
                       systimemodel.value tmfg,
                       ' ' wait_class
                  FROM systimemodel
                 WHERE systimemodel.stat_name = 'DB CPU'
                   AND systimemodel.value > 0
                 ORDER BY inst_id) v
         ORDER BY inst_id,
                  tmfg    DESC,
                  wtfg    DESC)
 WHERE rank <= 10;

host echo start collect......System Time Model... 

prompt <a name="timemodel"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● System Time Model</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE ON
SET MARKUP html TABLE 'id="timemodel" border="1" width="90%" align="center" summary="Script output" '

COLUMN  stat_name  HEADING 'Statistic Name'
COLUMN  times      HEADING 'Time (s)'
COLUMN  pct_of_db  HEADING '% of DB Time'

WITH systimemodel AS
 (SELECT b.instance_number inst_id,
         b.stat_name,
         (e.value - b.value) VALUE
    FROM dba_hist_sys_time_model b,
         dba_hist_sys_time_model e
   WHERE b.dbid(+) = e.dbid
     AND b.instance_number(+) = e.instance_number
     AND b.snap_id = &_snap_beg
     AND e.snap_id = &_snap_end
     AND b.stat_id = e.stat_id)
SELECT v.inst_id,
       v.stat_name,
       '<div align="left">' || v.times || '</div>' times,
       CASE
         WHEN (v.pct_of_db > 20 AND v.stat_name LIKE '%parse%' AND v.times > 500) THEN
          '<div align="left"><font color="red"><b>' || v.pct_of_db || '</b></font></div>'
         ELSE
          '<div align="left">' || v.pct_of_db || '</div>'
       END pct_of_db
  FROM (SELECT b.instance_number inst_id,
               e.stat_name,
               round((e.value - b.value) / 1000000,
                     2) times,
               round((CASE
                       WHEN e.stat_name IN ('DB time',
                                            'background cpu time',
                                            'background elapsed time',
                                            'total CPU time') THEN
                        NULL
                       ELSE
                        100 * (e.value - b.value)
                     END) / (SELECT systimemodel.value
                               FROM systimemodel
                              WHERE systimemodel.inst_id = b.instance_number
                                AND systimemodel.stat_name = 'DB time'),
                     2) pct_of_db
          FROM dba_hist_sys_time_model e,
               dba_hist_sys_time_model b
         WHERE b.snap_id(+) = &_snap_beg
           AND e.snap_id = &_snap_end
           AND b.instance_number(+) = e.instance_number
           AND b.stat_id = e.stat_id
           AND e.value - b.value > 0
         ORDER BY 1,
                  4 DESC NULLS LAST) v;

host echo start collect......TOP 10 SQL Order by Elapsed Time... 

prompt <a name="top10_sql"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● TOP 10 SQL Order by Elapsed Time</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE ON
SET MARKUP html TABLE 'id="top10sql" border="1" width="90%" align="center" summary="Script output" '

COLUMN  elsp_time      FORMAT  999,999,999,999,990.99   HEADING 'Elapsed Time (s)'
COLUMN  exec           FORMAT  999,999,999,999,999,999  HEADING 'Executions'
COLUMN  elsp_per_exec                                   HEADING 'Elapsed Time per Exec (s)'
COLUMN  norm_val                                        HEADING '%Total'
COLUMN  cpu                                             HEADING '%CPU'
COLUMN  io                                              HEADING '%IO'
COLUMN  sql_id                                          HEADING 'SQL Id'
COLUMN  module                                          HEADING 'SQL Module'

WITH systimemodel AS
 (SELECT b.instance_number inst_id,
         b.stat_name,
         (e.value - b.value) VALUE
    FROM dba_hist_sys_time_model b,
         dba_hist_sys_time_model e
   WHERE b.dbid(+) = e.dbid
     AND b.instance_number(+) = e.instance_number
     AND b.snap_id = &_snap_beg
     AND e.snap_id = &_snap_end
     AND b.stat_id = e.stat_id)
SELECT v.inst_id,
       v.elsp_time,
       v.exec,
       CASE
         WHEN v.elsp_per_exec > 60 THEN
          '<div align="right"><font color="red"><b>' || TRIM(to_char(v.elsp_per_exec,
                                                                     '999,999,999,990.99')) || '</b></font></div>'
         ELSE
          '<div align="right">' || TRIM(to_char(v.elsp_per_exec,
                                                '999,999,999,990.99')) || '</div>'
       END elsp_per_exec,
       CASE
         WHEN v.norm_val > 20 THEN
          '<div align="right"><font color="red"><b>' || TRIM(to_char(v.norm_val,
                                                                     '999,999,999,990.99')) || '</b></font></div>'
         ELSE
          '<div align="right">' || TRIM(to_char(v.norm_val,
                                                '999,999,999,990.99')) || '</div>'
       END norm_val,
       '<div align="right">' || TRIM(to_char(v.cpu,
                                             '990.99')) || '</div>' cpu,
       '<div align="right">' || TRIM(to_char(v.io,
                                             '990.99')) || '</div>' io,
       '<div align="left">' || v.sql_id || '</div>' sql_id,
       '<div align="left">' || v.module || '</div>' module
  FROM (SELECT sqt.inst_id,
               nvl(sqt.elap / 1000000,
                   NULL) elsp_time,
               sqt.exec,
               decode(sqt.exec,
                      0,
                      NULL,
                      sqt.elap / sqt.exec / 1000000) elsp_per_exec,
               100 * (sqt.elap / (SELECT systimemodel.value
                                    FROM systimemodel
                                   WHERE systimemodel.inst_id = sqt.inst_id
                                     AND systimemodel.stat_name = 'DB time')) norm_val,
               decode(sqt.elap,
                      0,
                      NULL,
                      100 * sqt.cput / sqt.elap) cpu,
               decode(sqt.elap,
                      0,
                      NULL,
                      100 * sqt.iowt / sqt.elap) io,
               sqt.sql_id,
               decode(sqt.module,
                      NULL,
                      NULL,
                      sqt.module) module,
               row_number() over(PARTITION BY inst_id ORDER BY elap DESC) rank
          FROM (SELECT instance_number inst_id,
                       sql_id,
                       MAX(module) module,
                       SUM(elapsed_time_delta) elap,
                       SUM(cpu_time_delta) cput,
                       SUM(executions_delta) exec,
                       SUM(iowait_delta) iowt
                  FROM dba_hist_sqlstat
                 WHERE snap_id > &_snap_beg
                   AND snap_id <= &_snap_end
                 GROUP BY instance_number,
                          sql_id) sqt,
               dba_hist_sqltext st
         WHERE st.sql_id(+) = sqt.sql_id
           AND st.sql_text NOT LIKE '%td%'
           AND st.sql_text NOT LIKE 'call%'
           AND st.sql_text NOT LIKE 'begin%'
           AND st.sql_text NOT LIKE 'BEGIN%'
           AND st.sql_text NOT LIKE 'declare%'
           AND st.sql_text NOT LIKE 'DECLARE%'
           AND sqt.module NOT LIKE 'DBMS_SCHEDULER%'
         ORDER BY sqt.inst_id,
                  nvl(sqt.elap,
                      -1) DESC,
                  sqt.sql_id) v
 WHERE v.rank <= 10;

prompt <a name="top10_sqltext"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● TOP 10 SQL Text</b></font>

CLEAR COLUMNS COMPUTES
SET DEFINE ON
SET MARKUP html TABLE 'border="1" width="90%" align="center" summary="Script output" '

COLUMN  sql_id                                          HEADING 'SQL Id'
COLUMN  sqltext                                         HEADING 'SQL Text'

SELECT DISTINCT v.sql_id,
                v.sqltext
  FROM (SELECT sqt.sql_id,
               nvl(dbms_lob.substr(st.sql_text,
                                   decode(sign(dbms_lob.getlength(st.sql_text) - 4000),
                                          -1,
                                          dbms_lob.getlength(st.sql_text),
                                          3000),
                                   1),
                   to_clob('** SQL Text Not Available **')) sqltext,
               row_number() over(PARTITION BY inst_id ORDER BY elap DESC) rank
          FROM (SELECT instance_number inst_id,
                       sql_id,
                       MAX(module) module,
                       SUM(elapsed_time_delta) elap
                  FROM dba_hist_sqlstat
                 WHERE snap_id > &_snap_beg
                   AND snap_id <= &_snap_end
                 GROUP BY instance_number,
                          sql_id) sqt,
               dba_hist_sqltext st
         WHERE st.sql_id(+) = sqt.sql_id
           AND st.sql_text NOT LIKE '%td%'
           AND st.sql_text NOT LIKE 'call%'
           AND st.sql_text NOT LIKE 'begin%'
           AND st.sql_text NOT LIKE 'BEGIN%'
           AND st.sql_text NOT LIKE 'declare%'
           AND st.sql_text NOT LIKE 'DECLARE%'
           AND sqt.module NOT LIKE 'DBMS_SCHEDULER%'
         ORDER BY sqt.inst_id,
                  nvl(sqt.elap,
                      -1) DESC,
                  sqt.sql_id) v
 WHERE v.rank <= 10;

host echo start collect......Awrcrt Informaion... 

prompt <a name="awrcrt"></a>
prompt <font size="+1" face="Consolas" color="#336699"><b>● Awrcrt Information</b></font><br>

set autoprint on
set heading off
set markup HTML OFF SPOOL OFF
CLEAR COLUMNS COMPUTES
SET DEFINE ON
-----------------------------------------------------------------------
prompt <font size="+0.5" face="Consolas" color="#336699"><b>CPU Utilization</b></font><br><br>
SELECT chr(10) || '<font face="Consolas" color="#990000"><b>Instance: ' || instance_name || '</b></font>'
    || chr(10) || '<div style="width:100%;">'
    || chr(10) || '<canvas width="1800" height="600" id="canvas_cpu' || instance_number || '"></canvas>'
    || chr(10) || '</div>'
  FROM gv$instance
ORDER BY instance_number;
-------------------------------------------------------------
prompt <font size="+0.5" face="Consolas" color="#336699"><b>Connections</b></font><br><br>
SELECT chr(10) || '<font face="Consolas" color="#990000"><b>Instance: ' || instance_name || '</b></font>'
    || chr(10) || '<div style="width:100%;">'
    || chr(10) || '<canvas width="1800" height="600" id="canvas_conn' || instance_number || '"></canvas>'
    || chr(10) || '</div>'
  FROM gv$instance
ORDER BY instance_number;
------------------------------------------------------------------
prompt <font size="+0.5" face="Consolas" color="#336699"><b>User Commits and Redo size</b></font><br><br>
SELECT chr(10) || '<font face="Consolas" color="#990000"><b>Instance: ' || instance_name || '</b></font>'
    || chr(10) || '<div style="width:100%;">'
    || chr(10) || '<canvas width="1800" height="600" id="canvas_commit' || instance_number || '"></canvas>'
    || chr(10) || '</div>'
  FROM gv$instance
ORDER BY instance_number;
-------------------------------------------------------------
prompt <font size="+0.5" face="Consolas" color="#336699"><b>User Logon</b></font><br><br>
SELECT chr(10) || '<font face="Consolas" color="#990000"><b>Instance: ' || instance_name || '</b></font>'
    || chr(10) || '<div style="width:100%;">'
    || chr(10) || '<canvas width="1800" height="600" id="canvas_logon' || instance_number || '"></canvas>'
    || chr(10) || '</div>'
  FROM gv$instance
ORDER BY instance_number;
-------------------------------------------------------------------------
prompt <font size="+0.5" face="Consolas" color="#336699"><b>Top5 Wait Event</b></font><br><br>
SELECT chr(10) || '<font face="Consolas" color="#990000"><b>Instance: ' || instance_name || '</b></font>'
    || chr(10) || '<div style="width:100%;">'
    || chr(10) || '<canvas width="1800" height="600" id="canvas_event' || instance_number || '"></canvas>'
    || chr(10) || '</div>'
  FROM gv$instance
ORDER BY instance_number;
prompt <script>
------------------------cpu---------------------------------------
prompt  
prompt
DECLARE
  TYPE valuelist IS TABLE OF VARCHAR2(200);
  backdbcpu    valuelist;
  servercpu    valuelist;
  dbcpu        valuelist;
  snaptime     valuelist;
  cpu_cur      SYS_REFCURSOR;
  v_backdb_cpu VARCHAR2(200);
  v_server_cpu VARCHAR2(200);
  v_db_cpu     VARCHAR2(200);
  v_snap_time  VARCHAR2(200);
BEGIN
  FOR instinfo_cur IN (SELECT instance_number instid
                         FROM gv$instance
                        ORDER BY instance_number)
  LOOP
    dbms_output.put_line('var cpudata' || instinfo_cur.instid || ' = { type: "line", data: { labels: [');
    OPEN cpu_cur FOR
      SELECT SUM(CASE
                   WHEN e.metric_name = 'Background CPU Usage Per Sec' THEN
                    e.pct
                   ELSE
                    0
                 END) backdb_cpu,
             SUM(CASE
                   WHEN e.metric_name = 'Host CPU Utilization (%)' THEN
                    e.pct
                   ELSE
                    0
                 END) server_cpu,
             SUM(CASE
                   WHEN e.metric_name = 'CPU Usage Per Sec' THEN
                    e.pct
                   ELSE
                    0
                 END) db_cpu,
             (SELECT '"' || to_char(f.end_interval_time,
                                    'mm-dd hh24:mi') || '"'
                FROM dba_hist_snapshot f
               WHERE f.snap_id = e.snap_id
                 AND f.instance_number = instinfo_cur.instid) snap_time
        FROM (SELECT a.snap_id,
                     trunc(decode(a.metric_name,
                                  'Host CPU Utilization (%)',
                                  a.average,
                                  'CPU Usage Per Sec',
                                  a.average / 100 / (SELECT VALUE
                                                       FROM v$parameter t
                                                      WHERE t.name = 'cpu_count') * 100,
                                  a.average / 100 / (SELECT VALUE
                                                       FROM v$parameter t
                                                      WHERE t.name = 'cpu_count') * 100,
                                  a.average),
                           2) pct,
                     a.metric_name,
                     a.metric_unit
                FROM dba_hist_sysmetric_summary a
               WHERE a.snap_id >= &_crt_snap_beg
                 AND a.snap_id <= &_crt_snap_end
                 AND a.instance_number = instinfo_cur.instid
                 AND a.metric_name IN ('Host CPU Utilization (%)',
                                       'CPU Usage Per Sec',
                                       'Background CPU Usage Per Sec')
               ORDER BY 1,
                        3) e
       GROUP BY snap_id
       ORDER BY snap_id;
    FETCH cpu_cur BULK COLLECT
      INTO backdbcpu,
           servercpu,
           dbcpu,
           snaptime;
    CLOSE cpu_cur;
    ---handle null list---------------------
    IF (snaptime.count = 0) THEN
      snaptime.extend;
      snaptime(1) := '"1981-03-30 20:00:00"';
      backdbcpu.extend;
      backdbcpu(1) := '0';
      servercpu.extend;
      servercpu(1) := 0;
      dbcpu.extend;
      dbcpu(1) := 0;
    END IF;
    -----------------------------------------
    FOR i IN snaptime.first .. snaptime.last
    LOOP
      IF (i < snaptime.count) THEN
        dbms_output.put_line(snaptime(i) || ',');
      ELSIF (i = snaptime.count) THEN
        dbms_output.put_line(snaptime(i));
      END IF;
    END LOOP;
    -----------------------------------------
    dbms_output.put_line('],datasets: [{');
    dbms_output.put_line('label: "Backup CPU",');
    dbms_output.put_line('lineTension :0,');
    dbms_output.put_line('borderColor: "rgba(255, 255, 0, 1)" ,');
    dbms_output.put_line('backgroundColor: "rgba(128, 128, 0, 1)" ,');
    dbms_output.put_line('data: [');
    -----------------------------------------
    FOR i IN backdbcpu.first .. backdbcpu.last
    LOOP
      IF (i < backdbcpu.count) THEN
        dbms_output.put_line(backdbcpu(i) || ',');
      ELSIF (i = backdbcpu.count) THEN
        dbms_output.put_line(backdbcpu(i));
      END IF;
    END LOOP;
    -----------------------------------------
    dbms_output.put_line('], }, {');
    dbms_output.put_line('label: "Database CPU",');
    dbms_output.put_line('lineTension :0,');
    dbms_output.put_line('borderColor: "rgba(0, 0, 0, 128)" ,');
    dbms_output.put_line('backgroundColor: "rgba(0, 0, 255, 1)" ,');
    dbms_output.put_line('data: [');
    -----------------------------------------
    FOR i IN dbcpu.first .. dbcpu.last
    LOOP
      IF (i < dbcpu.count) THEN
        dbms_output.put_line(dbcpu(i) || ',');
      ELSIF (i = dbcpu.count) THEN
        dbms_output.put_line(dbcpu(i));
      END IF;
    END LOOP;
    -----------------------------------------
    dbms_output.put_line('],}, {');
    dbms_output.put_line('label: "Server CPU",');
    dbms_output.put_line('lineTension :0,');
    dbms_output.put_line('borderColor: "rgba(0, 128, 0, 1)" ,');
    dbms_output.put_line('backgroundColor: "rgba(0, 255, 0, 1)" ,');
    dbms_output.put_line('data: [');
    -----------------------------------------
    FOR i IN servercpu.first .. servercpu.last
    LOOP
      IF (i < servercpu.count) THEN
        dbms_output.put_line(servercpu(i) || ',');
      ELSIF (i = servercpu.count) THEN
        dbms_output.put_line(servercpu(i));
      END IF;
    END LOOP;
    -----------------------------------------
    dbms_output.put_line('],},]},');
    dbms_output.put_line('      options: {');
    dbms_output.put_line('        responsive: true,');
    dbms_output.put_line('        title:{');
    dbms_output.put_line('          display:true,');
    dbms_output.put_line('          text:"CPU Utilization"');
    dbms_output.put_line('        },');
    dbms_output.put_line('        tooltips: {');
    dbms_output.put_line('          mode: "index",');
    dbms_output.put_line('        },');
    dbms_output.put_line('        hover: {');
    dbms_output.put_line('          mode: "index"');
    dbms_output.put_line('        },');
    dbms_output.put_line('        scales: {');
    dbms_output.put_line('          xAxes: [{');
    dbms_output.put_line('            scaleLabel: {');
    dbms_output.put_line('              display: true,');
    dbms_output.put_line('              labelString: "Snap Time"');
    dbms_output.put_line('            }');
    dbms_output.put_line('          }],');
    dbms_output.put_line('          yAxes: [{');
    dbms_output.put_line('           ticks: {min : 0,  max :100 },');
    dbms_output.put_line('            stacked: false,');
    dbms_output.put_line('            scaleLabel: {');
    dbms_output.put_line('              display: true,');
    dbms_output.put_line('              labelString: "Value"');
    dbms_output.put_line('            }');
    dbms_output.put_line('          }]');
    dbms_output.put_line('        }');
    dbms_output.put_line('      }');
    dbms_output.put_line('    };');
  END LOOP;
END;
/
----------------------------cpu end-----------------------------
--------------------conn-----------------------------------
DECLARE
  TYPE valuelist IS TABLE OF VARCHAR2(200);
  snap_id  valuelist;
  proc     valuelist; ---Process
  se       valuelist; ---Session  
  snaptime valuelist;
  se_cur   SYS_REFCURSOR;
BEGIN
  FOR instinfo_cur IN (SELECT instance_number instid
                         FROM gv$instance
                        ORDER BY instance_number)
  LOOP
    dbms_output.put_line('var conndata' || instinfo_cur.instid || ' = { type: "line", data: { labels: [');
    OPEN se_cur FOR
      SELECT pr,
             se,
             (SELECT '"' || to_char(f.end_interval_time,
                                    'mm-dd hh24:mi') || '"'
                FROM dba_hist_snapshot f
               WHERE f.snap_id = a1.snap_id
                 AND f.instance_number = instinfo_cur.instid) snap_time,
             snap_id
        FROM (SELECT snap_id,
                     SUM(CASE
                           WHEN a.resource_name = 'processes' THEN
                            a.current_utilization
                           ELSE
                            0
                         END) pr,
                     SUM(CASE
                           WHEN a.resource_name = 'sessions' THEN
                            a.current_utilization
                           ELSE
                            0
                         END) se
                FROM dba_hist_resource_limit a
               WHERE a.snap_id >= &_crt_snap_beg
                 AND a.snap_id <= &_crt_snap_end
                 AND a.instance_number = instinfo_cur.instid
                 AND (a.resource_name = 'sessions' OR a.resource_name = 'processes')
               GROUP BY snap_id
               ORDER BY snap_id) a1;
  
    FETCH se_cur BULK COLLECT
      INTO proc,
           se,
           snaptime,
           snap_id;
    CLOSE se_cur;
    ---handle null list---------------------
    IF (snaptime.count = 0) THEN
      snaptime.extend;
      snaptime(1) := '"1981-03-30 20:00:00"';
      proc.extend;
      proc(1) := '0';
      se.extend;
      se(1) := '0';
    END IF;
    -----------------------------------------  
    FOR i IN snaptime.first .. snaptime.last
    LOOP
      IF (i < snaptime.count) THEN
        dbms_output.put_line(snaptime(i) || ',');
      ELSIF (i = snaptime.count) THEN
        dbms_output.put_line(snaptime(i));
      END IF;
    END LOOP;
    dbms_output.put_line('  ], datasets: [{');
    dbms_output.put_line('label: "Processes",');
    dbms_output.put_line('lineTension :0,');
    dbms_output.put_line('backgroundColor: window.awrColors.blue2,');
    dbms_output.put_line('borderColor: window.awrColors.blue2,');
    dbms_output.put_line('data: [ ');
    FOR i IN proc.first .. proc.last
    LOOP
      IF (i < proc.count) THEN
        dbms_output.put_line(proc(i) || ',');
      ELSIF (i = proc.count) THEN
        dbms_output.put_line(proc(i));
      END IF;
    END LOOP;
    dbms_output.put_line('], fill: false, }, {');
    dbms_output.put_line('label: "Sessions",');
    dbms_output.put_line('lineTension :0,');
    dbms_output.put_line('fill: false,');
    dbms_output.put_line('backgroundColor: window.awrColors.green1,');
    dbms_output.put_line('borderColor: window.awrColors.green1,');
    dbms_output.put_line('data: [');
    FOR i IN se.first .. se.last
    LOOP
      IF (i < se.count) THEN
        dbms_output.put_line(se(i) || ',');
      ELSIF (i = se.count) THEN
        dbms_output.put_line(se(i));
      END IF;
    END LOOP;
    dbms_output.put_line(' ], }] },             ');
    dbms_output.put_line('options: {            ');
    dbms_output.put_line('responsive: true,     ');
    dbms_output.put_line('title:{               ');
    dbms_output.put_line('display:true,         ');
    dbms_output.put_line('text:"Connections"    ');
    dbms_output.put_line('},                    ');
    dbms_output.put_line('tooltips: {           ');
    dbms_output.put_line('mode: "index",        ');
    dbms_output.put_line('intersect: false,     ');
    dbms_output.put_line('},                    ');
    dbms_output.put_line('hover: {              ');
    dbms_output.put_line('mode: "nearest",      ');
    dbms_output.put_line('intersect: true       ');
    dbms_output.put_line('},                    ');
    dbms_output.put_line('scales: {             ');
    dbms_output.put_line('xAxes: [{             ');
    dbms_output.put_line('display: true,        ');
    dbms_output.put_line('scaleLabel: {         ');
    dbms_output.put_line('display: true,        ');
    dbms_output.put_line('labelString: "Snap"   ');
    dbms_output.put_line('}                     ');
    dbms_output.put_line('}],                   ');
    dbms_output.put_line('yAxes: [{             ');
    dbms_output.put_line('display: true,        ');
    dbms_output.put_line('scaleLabel: {         ');
    dbms_output.put_line('display: true,        ');
    dbms_output.put_line('labelString:  "Value" ');
    dbms_output.put_line('} }] } } };           ');
  END LOOP;
END;
/

---------------------------conn end-----------------------------------
----------------------------logon-----------------------------
DECLARE
  TYPE valuelist IS TABLE OF VARCHAR2(200);
  snaptime  valuelist;
  max_logon valuelist; ---max logon
  avg_logon valuelist;
  cr_cur    SYS_REFCURSOR;
BEGIN
  FOR instinfo_cur IN (SELECT instance_number instid
                         FROM gv$instance
                        ORDER BY instance_number)
  LOOP
    dbms_output.put_line('var logondata' || instinfo_cur.instid || ' = { type: "line", data: { labels: [');
    OPEN cr_cur FOR
      SELECT SUM(a1.maxlogon),
             SUM(a1.avglogon),
             (SELECT '"' || to_char(f.end_interval_time,
                                    'mm-dd hh24:mi') || '"'
                FROM dba_hist_snapshot f
               WHERE f.snap_id = a1.snap_id
                 AND f.instance_number = instinfo_cur.instid) snap_time
        FROM (SELECT a.snap_id,
                     CASE
                       WHEN metric_name = 'Logons Per Sec' THEN
                        trunc(a.maxval)
                       ELSE
                        0
                     END maxlogon,
                     CASE
                       WHEN metric_name = 'Logons Per Sec' THEN
                        trunc(a.average)
                       ELSE
                        0
                     END avglogon
                FROM dba_hist_sysmetric_summary a
               WHERE a.snap_id >= &_crt_snap_beg
                 AND a.snap_id <= &_crt_snap_end
                 AND a.instance_number = instinfo_cur.instid
                 AND a.metric_name IN ('Logons Per Sec')) a1
       GROUP BY a1.snap_id
       ORDER BY a1.snap_id;
    FETCH cr_cur BULK COLLECT
      INTO max_logon,
           avg_logon,
           snaptime;
    CLOSE cr_cur;
    ---handle null list---------------------
    IF (snaptime.count = 0) THEN
      snaptime.extend;
      snaptime(1) := '"1981-03-30 20:00:00"';
      max_logon.extend;
      max_logon(1) := '0';
      avg_logon.extend;
      avg_logon(1) := '0';
    END IF;
    -----------------------------------------  
    FOR i IN snaptime.first .. snaptime.last
    LOOP
      IF (i < snaptime.count) THEN
        IF i = 1 THEN
          NULL;
        ELSE
          dbms_output.put_line(snaptime(i) || ',');
        END IF;
      ELSIF (i = snaptime.count) THEN
        dbms_output.put_line(snaptime(i));
      END IF;
    END LOOP;
    ------------------------------------
    dbms_output.put_line('], datasets: [{');
    dbms_output.put_line('label: "Max Logon",');
    dbms_output.put_line('lineTension :0,');
    dbms_output.put_line('backgroundColor: window.awrColors.red1,');
    dbms_output.put_line('borderColor: window.awrColors.red2,');
    dbms_output.put_line('data: [');
    ------------------------------
    FOR i IN max_logon.first .. max_logon.last
    LOOP
      IF (i < max_logon.count) THEN
        IF i = 1 THEN
          NULL;
        ELSE
          dbms_output.put_line(max_logon(i) || ',');
        END IF;
      ELSIF (i = max_logon.count) THEN
        dbms_output.put_line(max_logon(i));
      END IF;
    END LOOP;
    dbms_output.put_line('], fill: false, }, {');
    dbms_output.put_line('label: "Average logon",');
    dbms_output.put_line('lineTension :0,');
    dbms_output.put_line('fill: false,');
    dbms_output.put_line('borderDash: [5, 5],');
    dbms_output.put_line('backgroundColor: window.awrColors.blue1,');
    dbms_output.put_line('borderColor: window.awrColors.blue2,');
    dbms_output.put_line('data: [');
    FOR i IN avg_logon.first .. avg_logon.last
    LOOP
      IF (i < avg_logon.count) THEN
        IF i = 1 THEN
          NULL;
        ELSE
          dbms_output.put_line(avg_logon(i) || ',');
        END IF;
      ELSIF (i = avg_logon.count) THEN
        dbms_output.put_line(avg_logon(i));
      END IF;
    END LOOP;
    dbms_output.put_line('], }] },                    ');
    dbms_output.put_line('options: {                  ');
    dbms_output.put_line('responsive: true,           ');
    dbms_output.put_line('title:{                     ');
    dbms_output.put_line('display:true,               ');
    dbms_output.put_line('text:"User logon per Second"');
    dbms_output.put_line('},                          ');
    dbms_output.put_line('tooltips: {                 ');
    dbms_output.put_line('mode: "index",              ');
    dbms_output.put_line('intersect: false,           ');
    dbms_output.put_line('},                          ');
    dbms_output.put_line('hover: {                    ');
    dbms_output.put_line('mode: "nearest",            ');
    dbms_output.put_line('intersect: true             ');
    dbms_output.put_line('},                          ');
    dbms_output.put_line('scales: {                   ');
    dbms_output.put_line('xAxes: [{                   ');
    dbms_output.put_line('display: true,              ');
    dbms_output.put_line('scaleLabel: {               ');
    dbms_output.put_line('display: true,              ');
    dbms_output.put_line('labelString: "Snap"         ');
    dbms_output.put_line('}                           ');
    dbms_output.put_line('}],                         ');
    dbms_output.put_line('yAxes: [{                   ');
    dbms_output.put_line('display: true,              ');
    dbms_output.put_line('scaleLabel: {               ');
    dbms_output.put_line('display: true,              ');
    dbms_output.put_line('labelString:  "Value"       ');
    dbms_output.put_line('} }] } } };                 ');
  END LOOP;
END;
/
----------------------------logon end-----------------------------
-----------------------------commit and redo--------------------------------
DECLARE
  TYPE valuelist IS TABLE OF VARCHAR2(200);
  rd       valuelist; ---redo size
  uc       valuelist; ---User Commits
  snaptime valuelist;
  uc_cur   SYS_REFCURSOR;
BEGIN
  FOR instinfo_cur IN (SELECT instance_number instid
                         FROM gv$instance
                        ORDER BY instance_number)
  LOOP
    dbms_output.put_line('var commitdata' || instinfo_cur.instid || ' = {labels: [');
    OPEN uc_cur FOR
      SELECT trunc((a2.rd - lag(a2.rd,
                                1,
                                a2.rd) over(ORDER BY a2.snap_id)) / &_iv / 1024) rd,
             trunc((a2.uc - lag(a2.uc,
                                1,
                                a2.uc) over(ORDER BY a2.snap_id)) / &_iv) uc,
             (SELECT '"' || to_char(f.end_interval_time,
                                    'mm-dd hh24:mi') || '"'
                FROM dba_hist_snapshot f
               WHERE f.snap_id = a2.snap_id
                 AND f.instance_number = instinfo_cur.instid) snap_time
        FROM (SELECT a1.snap_id,
                     SUM(CASE
                           WHEN a1.stat_name = 'redo size' THEN
                            a1.value
                           ELSE
                            0
                         END) rd,
                     SUM(CASE
                           WHEN a1.stat_name = 'user commits' THEN
                            a1.value
                           ELSE
                            0
                         END) uc
                FROM (SELECT a.snap_id,
                             a.stat_name,
                             a.value
                        FROM dba_hist_sysstat a
                       WHERE (a.stat_name = 'redo size' OR a.stat_name = 'user commits')
                         AND snap_id >= &_crt_snap_beg
                         AND snap_id <= &_crt_snap_end
                         AND a.instance_number = instinfo_cur.instid
                       ORDER BY a.snap_id,
                                a.stat_name) a1
               GROUP BY a1.snap_id
               ORDER BY a1.snap_id) a2;
  
    FETCH uc_cur BULK COLLECT
      INTO rd,
           uc,
           snaptime;
    CLOSE uc_cur;
    ---handle null list---------------------
    IF (snaptime.count = 0) THEN
      snaptime.extend;
      snaptime(1) := '"1981-03-30 20:00:00"';
      rd.extend;
      rd(1) := '0';
      uc.extend;
      uc(1) := '0';
    END IF;
    -----------------------------------------  
    FOR i IN snaptime.first .. snaptime.last
    LOOP
      IF (i < snaptime.count) THEN
        dbms_output.put_line(snaptime(i) || ',');
      ELSIF (i = snaptime.count) THEN
        dbms_output.put_line(snaptime(i));
      END IF;
    END LOOP;
    dbms_output.put_line('  ],datasets: [{');
    dbms_output.put_line('label: "User Commit",');
    dbms_output.put_line('lineTension :0,');
    dbms_output.put_line('borderColor: window.awrColors.blue2,');
    dbms_output.put_line('backgroundColor: window.awrColors.blue1,');
    dbms_output.put_line('fill: false,');
    dbms_output.put_line('data: [');
    FOR i IN uc.first .. uc.last
    LOOP
      IF (i < uc.count) THEN
        dbms_output.put_line(uc(i) || ',');
      ELSIF (i = uc.count) THEN
        dbms_output.put_line(uc(i));
      END IF;
    END LOOP;
    dbms_output.put_line('], yAxisID: "y-axis-1", }, {');
    dbms_output.put_line('label: "Redo size(KB)",');
    dbms_output.put_line('lineTension :0,');
    dbms_output.put_line('borderColor: window.awrColors.green2,');
    dbms_output.put_line('backgroundColor:window.awrColors.green0,');
    dbms_output.put_line('fill: true,');
    dbms_output.put_line('data: [');
    FOR i IN rd.first .. rd.last
    LOOP
      IF (i < rd.count) THEN
        dbms_output.put_line(rd(i) || ',');
      ELSIF (i = rd.count) THEN
        dbms_output.put_line(rd(i));
      END IF;
    END LOOP;
    dbms_output.put_line('],yAxisID: "y-axis-2"}]};');
  END LOOP;
END;
/
---------------------------------commit and redo end------------------------------------
------------------------event---------------------------------
DECLARE
  TYPE valuelist IS TABLE OF VARCHAR2(200);
  pct    valuelist;
  event  valuelist;
  my_cur SYS_REFCURSOR;
BEGIN
  FOR instinfo_cur IN (SELECT instance_number instid
                         FROM gv$instance
                        ORDER BY instance_number)
  LOOP
    dbms_output.put_line('var eventdata' || instinfo_cur.instid || ' = { data: { datasets: [{ data: [');
    OPEN my_cur FOR
      SELECT pct,
             event
        FROM (SELECT trunc(pctwtt,
                           2) pct,
                     event,
                     rownum rn
                FROM (SELECT event,
                             waits,
                             TIME,
                             pctwtt,
                             wait_class
                        FROM (SELECT e.event_name event,
                                     e.total_waits - nvl(b.total_waits,
                                                            0) waits,
                                     (e.time_waited_micro - nvl(b.time_waited_micro,
                                                                   0)) / 1000000 TIME,
                                     100 * (e.time_waited_micro - nvl(b.time_waited_micro,
                                                                         0)) /
                                     ((SELECT SUM(VALUE)
                                         FROM dba_hist_sys_time_model e
                                        WHERE e.snap_id = &_crt_snap_end
                                          AND e.instance_number = instinfo_cur.instid
                                          AND e.stat_name = 'DB time') - (SELECT SUM(VALUE)
                                                                             FROM dba_hist_sys_time_model b
                                                                            WHERE b.snap_id = &_crt_snap_beg
                                                                              AND b.instance_number = instinfo_cur.instid
                                                                              AND b.stat_name = 'DB time')) pctwtt,
                                     e.wait_class wait_class
                                FROM dba_hist_system_event b,
                                     dba_hist_system_event e
                               WHERE b.snap_id(+) = &_crt_snap_beg
                                 AND e.snap_id = &_crt_snap_end
                                 AND b.instance_number(+) = instinfo_cur.instid
                                 AND e.instance_number = instinfo_cur.instid
                                 AND b.event_id(+) = e.event_id
                                 AND e.total_waits > nvl(b.total_waits,
                                                         0)
                                 AND e.wait_class != 'Idle'
                              UNION ALL
                              SELECT 'CPU time' event,
                                     to_number(NULL) waits,
                                     ((SELECT SUM(VALUE)
                                         FROM dba_hist_sys_time_model e
                                        WHERE e.snap_id = &_crt_snap_end
                                          AND e.instance_number = instinfo_cur.instid
                                          AND e.stat_name = 'DB CPU') - (SELECT SUM(VALUE)
                                                                            FROM dba_hist_sys_time_model b
                                                                           WHERE b.snap_id = &_crt_snap_beg
                                                                             AND b.instance_number = instinfo_cur.instid
                                                                             AND b.stat_name = 'DB CPU')) / 1000000 TIME,
                                     100 *
                                     ((SELECT SUM(VALUE)
                                         FROM dba_hist_sys_time_model e
                                        WHERE e.snap_id = &_crt_snap_end
                                          AND e.instance_number = instinfo_cur.instid
                                          AND e.stat_name = 'DB CPU') - (SELECT SUM(VALUE)
                                                                            FROM dba_hist_sys_time_model b
                                                                           WHERE b.snap_id = &_crt_snap_beg
                                                                             AND b.instance_number = instinfo_cur.instid
                                                                             AND b.stat_name = 'DB CPU')) /
                                     ((SELECT SUM(VALUE)
                                         FROM dba_hist_sys_time_model e
                                        WHERE e.snap_id = &_crt_snap_end
                                          AND e.instance_number = instinfo_cur.instid
                                          AND e.stat_name = 'DB time') - (SELECT SUM(VALUE)
                                                                             FROM dba_hist_sys_time_model b
                                                                            WHERE b.snap_id = &_crt_snap_beg
                                                                              AND b.instance_number = instinfo_cur.instid
                                                                              AND b.stat_name = 'DB time')) pctwtt,
                                     NULL wait_class
                                FROM dual
                               WHERE ((SELECT SUM(VALUE)
                                         FROM dba_hist_sys_time_model e
                                        WHERE e.snap_id = &_crt_snap_end
                                          AND e.instance_number = instinfo_cur.instid
                                          AND e.stat_name = 'DB CPU') - (SELECT SUM(VALUE)
                                                                            FROM dba_hist_sys_time_model b
                                                                           WHERE b.snap_id = &_crt_snap_beg
                                                                             AND b.instance_number = instinfo_cur.instid
                                                                             AND b.stat_name = 'DB CPU')) > 0)
                       ORDER BY TIME  DESC,
                                waits DESC)
               WHERE rownum <= 5) a1
       ORDER BY rn;
    FETCH my_cur BULK COLLECT
      INTO pct,
           event;
    CLOSE my_cur;
    IF (pct.count = 0) THEN
      pct.extend;
      pct(1) := '0';
      event.extend;
      event(1) := 'No AWR data';
    END IF;
    FOR i IN pct.first .. pct.last
    LOOP
      IF (i < pct.count) THEN
        dbms_output.put_line(pct(i) || ',');
      ELSIF (i = pct.count) THEN
        dbms_output.put_line(pct(i));
      END IF;
    END LOOP;
    dbms_output.put_line('], backgroundColor: [');
    dbms_output.put_line('window.awrColors.red2,');
    dbms_output.put_line('window.awrColors.blue2,');
    dbms_output.put_line('window.awrColors.green1,');
    dbms_output.put_line('window.awrColors.yellow1,');
    dbms_output.put_line('window.awrColors.orange1');
    dbms_output.put_line('], label: "Event" }],');
    dbms_output.put_line('labels: [');
    FOR i IN event.first .. event.last
    LOOP
      IF (i < event.count) THEN
        dbms_output.put_line('"' || event(i) || '",');
      ELSIF (i = event.count) THEN
        dbms_output.put_line('"' || event(i) || '"');
      END IF;
    END LOOP;
    dbms_output.put_line('   ] },');
    dbms_output.put_line('  options: {');
    dbms_output.put_line('     responsive: true,');
    dbms_output.put_line('   legend: {');
    dbms_output.put_line('    position: "right",');
    dbms_output.put_line(' },');
    dbms_output.put_line(' title: {');
    dbms_output.put_line('   display: true,');
    dbms_output.put_line('  text: "event"');
    dbms_output.put_line(' },');
    dbms_output.put_line(' scale: {');
    dbms_output.put_line(' ticks: {');
    dbms_output.put_line(' beginAtZero: true');
    dbms_output.put_line(' },');
    dbms_output.put_line(' reverse: false');
    dbms_output.put_line(' },');
    dbms_output.put_line(' animation: {');
    dbms_output.put_line(' animateRotate: false,');
    dbms_output.put_line(' animateScale: true');
    dbms_output.put_line('} } };');
  END LOOP;
END;
/
----------------------event end--------------------------------
prompt window.onload = function() {
--------------------------------
SELECT chr(10) || 'var ctx = document.getElementById("canvas_cpu' || instance_number || '").getContext("2d");'
    || chr(10) || 'window.myLine = new Chart(ctx, cpudata' || instance_number || ');'
  FROM gv$instance;
SELECT chr(10) || 'var ctx7 = document.getElementById("canvas_commit' || instance_number || '").getContext("2d");'
    || chr(10) || 'window.myLine = Chart.Line(ctx7, {'
    || chr(10) || 'data: commitdata' || instance_number || ','
    || chr(10) || 'options: {'
    || chr(10) || 'responsive: true,'
    || chr(10) || 'hoverMode: "index",'
    || chr(10) || 'stacked: false,'
    || chr(10) || 'title:{'
    || chr(10) || 'display: true,'
    || chr(10) || 'text:"Commits and Redo size per second"'
    || chr(10) || '},'
    || chr(10) || 'scales: {'
    || chr(10) || 'yAxes: [{'
    || chr(10) || 'type: "linear",'
    || chr(10) || 'display: true,'
    || chr(10) || 'position: "left",'
    || chr(10) || 'id: "y-axis-1",'
    || chr(10) || '}, {'
    || chr(10) || 'type: "linear",' 
    || chr(10) || 'display: true,'
    || chr(10) || 'position: "right",'
    || chr(10) || 'id: "y-axis-2",'
    || chr(10) || '// grid line settings'
    || chr(10) || 'gridLines: {'
    || chr(10) || 'drawOnChartArea: false,'  
    || chr(10) || '}, }], }} });'
  FROM gv$instance;
SELECT chr(10) || 'var ctxconn = document.getElementById("canvas_conn' || instance_number || '").getContext("2d");'
    || chr(10) || 'window.myLine = new Chart(ctxconn, conndata' || instance_number || ');'
  FROM gv$instance;
SELECT chr(10) || 'var ctxlogon = document.getElementById("canvas_logon' || instance_number || '").getContext("2d");'
    || chr(10) || 'window.myLine = new Chart(ctxlogon, logondata' || instance_number || ');'
  FROM gv$instance;
SELECT chr(10) || 'var ctxevent = document.getElementById("canvas_event' || instance_number || '");'
    || chr(10) || 'window.myPolarArea = Chart.PolarArea(ctxevent, eventdata' || instance_number || ');'
  FROM gv$instance;
------------------------------------
prompt };
prompt 	</script>	
prompt <hr>

-- +----------------------------------------------------------------------------+
-- |                            - END OF REPORT -                               |
-- +----------------------------------------------------------------------------+

prompt 　　 　　 　　 　　
host echo Database script execution ends....
COLUMN date_time_end NEW_VALUE _date_time_end NOPRINT
SELECT TO_CHAR(SYSDATE,'YYYY-MM-DD HH24:MI:SS') date_time_end FROM dual;

prompt <center><font size="+2" face="Consolas" color="darkgreen"><b>DATABASE CHECK END</b></font></center>

prompt
prompt <b><font face="Consolas" color="#990000">ENDTIME</font>: &_date_time_end </font> </b>
prompt

prompt <a name="html_bottom_link"></a>
prompt <center>[<a class="noLink" href="#directory"><b>Back to Top</b></a>]</center><p>

SET TERMOUT ON
SET MARKUP HTML OFF PREFORMAT OFF entmap on
exit
