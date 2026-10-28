#!/bin/sh
# ===========================================================================
# SQL Server 数据库巡检采集脚本 (Linux / macOS)
# 生成 HTML 格式的巡检数据文件，供 ora100.com 巡检报告平台解析
#
# 依赖: sqlcmd (mssql-tools / mssql-tools18)
# 用法: sh mssql_check.sh -S <host> -U <user> -P <password> [-p <port>] [-o <output>]
#
# 版本: 1.0
# ===========================================================================

set -u

SCRIPT_VERSION="1.0"
SERVER="localhost"
PORT="1433"
USER=""
PASSWORD=""
OUTPUT=""
DATABASE="master"
QUERY_TIMEOUT=30
CONN_TIMEOUT=10

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
usage() {
  cat <<'USAGE'
用法: sh mssql_check.sh [选项]

选项:
  -S <host>       SQL Server 地址 (默认 localhost)
  -p <port>       端口 (默认 1433)
  -U <user>       登录用户名 (必填)
  -P <password>   登录密码 (必填)
  -d <database>   数据库名 (默认 master)
  -o <output>     输出文件路径 (默认当前目录)
  -h              显示帮助

示例:
  sh mssql_check.sh -S 192.168.1.100 -U sa -P 'YourPassword'
  sh mssql_check.sh -S dbserver -p 1434 -U sa -P 'Pass' -o /tmp/check.html
USAGE
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    -S) SERVER="$2"; shift 2 ;;
    -p) PORT="$2"; shift 2 ;;
    -U) USER="$2"; shift 2 ;;
    -P) PASSWORD="$2"; shift 2 ;;
    -d) DATABASE="$2"; shift 2 ;;
    -o) OUTPUT="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "[ERROR] 未知参数: $1"; usage ;;
  esac
done

if [ -z "$USER" ] || [ -z "$PASSWORD" ]; then
  echo "[ERROR] 必须提供 -U <user> 和 -P <password>"
  exit 1
fi

# Locate sqlcmd
SQLCMD=""
for cmd in sqlcmd /opt/mssql-tools18/bin/sqlcmd /opt/mssql-tools/bin/sqlcmd; do
  if command -v "$cmd" >/dev/null 2>&1; then
    SQLCMD="$cmd"
    break
  fi
done
if [ -z "$SQLCMD" ]; then
  echo "[ERROR] 未找到 sqlcmd，请安装 mssql-tools 或 mssql-tools18"
  echo "  参考: https://learn.microsoft.com/sql/linux/sql-server-linux-setup-tools"
  exit 1
fi

COLLECT_TIME=$(date '+%Y-%m-%d %H:%M:%S')
INSTANCE_NAME="${SERVER}:${PORT}"

if [ -z "$OUTPUT" ]; then
  TS=$(date '+%Y%m%d_%H%M%S')
  OUTPUT="./sqlserver_check_${TS}.html"
fi

echo "============================================"
echo " SQL Server 巡检采集脚本 (Linux) v${SCRIPT_VERSION}"
echo " 目标: ${INSTANCE_NAME}"
echo " 输出: ${OUTPUT}"
echo "============================================"

# ---------------------------------------------------------------------------
# Helper: run SQL query, return tab-separated output (no headers)
# ---------------------------------------------------------------------------
run_sql() {
  _query="$1"
  _timeout="${2:-$QUERY_TIMEOUT}"
  _err=$(mktemp "${TMPDIR:-/tmp}/mssql_check.XXXXXX") || return 1
  _out=$("$SQLCMD" -S "${SERVER},${PORT}" -U "$USER" -P "$PASSWORD" -d "$DATABASE" \
    -l "$CONN_TIMEOUT" -t "$_timeout" \
    -W -s "	" -h -1 -C \
    -Q "$_query" 2>"$_err")
  _rc=$?
  if [ "$_rc" -ne 0 ]; then
    echo "[ERROR] SQL 执行失败: $_query" >&2
    sed -n '1,6p' "$_err" >&2
    rm -f "$_err"
    return "$_rc"
  fi
  rm -f "$_err"
  printf '%s\n' "$_out" | sed '/^$/d; /^([0-9]* rows* affected)/d'
}

# Helper: run SQL query with headers (for multi-column tables)
run_sql_with_headers() {
  _query="$1"
  _timeout="${2:-$QUERY_TIMEOUT}"
  _err=$(mktemp "${TMPDIR:-/tmp}/mssql_check.XXXXXX") || return 1
  _out=$("$SQLCMD" -S "${SERVER},${PORT}" -U "$USER" -P "$PASSWORD" -d "$DATABASE" \
    -l "$CONN_TIMEOUT" -t "$_timeout" \
    -W -s "	" -C \
    -Q "$_query" 2>"$_err")
  _rc=$?
  if [ "$_rc" -ne 0 ]; then
    echo "[ERROR] SQL 执行失败: $_query" >&2
    sed -n '1,6p' "$_err" >&2
    rm -f "$_err"
    return "$_rc"
  fi
  rm -f "$_err"
  printf '%s\n' "$_out" | sed '/^$/d; /^([0-9]* rows* affected)/d; /^---/d'
}

# Helper: HTML-escape a value
html_escape() {
  printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g'
}

# Helper: Build key-value HTML table from "key<tab>value" lines
build_kv_table() {
  _data="$1"
  if [ -z "$_data" ]; then
    echo "<p>无数据</p>"
    return
  fi
  echo "<table>"
  echo "$_data" | while IFS='	' read -r _k _v; do
    [ -z "$_k" ] && continue
    _ke=$(html_escape "$_k")
    _ve=$(html_escape "${_v:-N/A}")
    echo "<tr><td>${_ke}</td><td>${_ve}</td></tr>"
  done
  echo "</table>"
}

# Helper: Build multi-column HTML table from header + data lines
build_multi_table() {
  _data="$1"
  if [ -z "$_data" ]; then
    echo "<p>无数据</p>"
    return
  fi
  _header=$(echo "$_data" | head -1)
  echo "<table>"
  # header row
  printf "<tr>"
  echo "$_header" | tr '	' '\n' | while read -r _h; do
    _he=$(html_escape "$_h")
    printf "<th>%s</th>" "$_he"
  done
  printf "</tr>\n"
  # data rows
  echo "$_data" | tail -n +2 | while IFS= read -r _line; do
    [ -z "$_line" ] && continue
    printf "<tr>"
    echo "$_line" | tr '	' '\n' | while read -r _cell; do
      _ce=$(html_escape "$_cell")
      printf "<td>%s</td>" "$_ce"
    done
    printf "</tr>\n"
  done
  echo "</table>"
}

# ===========================================================================
# 1. Version info
# ===========================================================================
echo ""
echo "[1/17] 采集版本信息..."
if ! SQL_PROBE=$(run_sql "SELECT 1"); then
  echo "[ERROR] SQL Server 连接或认证失败，停止生成巡检 HTML，避免产生 N/A 假数据。" >&2
  exit 1
fi
SS_VERSION=$(run_sql "SELECT @@VERSION" | head -1)
SS_VERSION=$(echo "$SS_VERSION" | cut -c1-200)
PROD_VER=$(run_sql "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128))" | head -1)
SS_EDITION=$(run_sql "SELECT CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128))" | head -1)
SS_PRODUCT_LEVEL=$(run_sql "SELECT CAST(SERVERPROPERTY('ProductLevel') AS NVARCHAR(128))" | head -1)

VERSION_KV=$(cat <<KV
SQL Server 版本	${SS_VERSION:-N/A}
产品版本	${PROD_VER:-N/A}
版本	${SS_EDITION:-N/A}
Service Pack	${SS_PRODUCT_LEVEL:-N/A}
KV
)
VERSION_HTML=$(build_kv_table "$VERSION_KV")

# ===========================================================================
# 2. Connection info
# ===========================================================================
echo "[2/17] 采集连接信息..."
MAX_CONN=$(run_sql "SELECT @@MAX_CONNECTIONS" | head -1)
MAX_CONN=$(echo "$MAX_CONN" | tr -d ' ')
CONN_KV=$(printf "max_connections\t%s" "${MAX_CONN:-32767}")
CONN_HTML=$(build_kv_table "$CONN_KV")

# ===========================================================================
# 3. Active sessions
# ===========================================================================
echo "[3/17] 采集活动会话..."
SESSION_DATA=$(run_sql_with_headers "SELECT session_id, login_name, host_name, program_name, status FROM sys.dm_exec_sessions WHERE is_user_process = 1")
SESSION_HTML=$(build_multi_table "$SESSION_DATA")

# ===========================================================================
# 4. Database size
# ===========================================================================
echo "[4/17] 采集数据库大小..."
DBSIZE_DATA=$(run_sql_with_headers "SELECT d.name AS database_name, CAST(SUM(mf.size) * 8 / 1024.0 AS DECIMAL(10,2)) AS size_mb, d.state_desc, d.recovery_model_desc FROM sys.databases d JOIN sys.master_files mf ON d.database_id = mf.database_id GROUP BY d.name, d.state_desc, d.recovery_model_desc")
DBSIZE_HTML=$(build_multi_table "$DBSIZE_DATA")

# ===========================================================================
# 5. Database status
# ===========================================================================
echo "[5/17] 采集数据库状态..."
DBSTATUS_DATA=$(run_sql_with_headers "SELECT name, state_desc, recovery_model_desc, compatibility_level FROM sys.databases")
DBSTATUS_HTML=$(build_multi_table "$DBSTATUS_DATA")

# ===========================================================================
# 6. Config parameters
# ===========================================================================
echo "[6/17] 采集关键配置..."
CONFIG_DATA=$(run_sql_with_headers "SELECT name, CAST(value AS NVARCHAR(100)) AS value, CAST(value_in_use AS NVARCHAR(100)) AS value_in_use FROM sys.configurations WHERE name IN ('max server memory (MB)','min server memory (MB)','max degree of parallelism','cost threshold for parallelism','backup compression default')")
CONFIG_HTML=$(build_multi_table "$CONFIG_DATA")

# ===========================================================================
# 7. Wait stats
# ===========================================================================
echo "[7/17] 采集等待统计..."
WAIT_DATA=$(run_sql_with_headers "SELECT TOP 10 wait_type, wait_time_ms, waiting_tasks_count FROM sys.dm_os_wait_stats WHERE wait_time_ms > 0 ORDER BY wait_time_ms DESC")
WAIT_HTML=$(build_multi_table "$WAIT_DATA")

# ===========================================================================
# 8. CPU usage
# ===========================================================================
echo "[8/17] 采集 CPU 信息..."
CPU_RAW=$(run_sql "SELECT CAST(@@CPU_BUSY AS NVARCHAR(50)) + '|' + CAST(@@IDLE AS NVARCHAR(50)) + '|' + CAST(@@TIMETICKS AS NVARCHAR(50))" | head -1)
CPU_BUSY=$(echo "$CPU_RAW" | cut -d'|' -f1 | tr -d ' ')
CPU_IDLE=$(echo "$CPU_RAW" | cut -d'|' -f2 | tr -d ' ')
CPU_TICKS=$(echo "$CPU_RAW" | cut -d'|' -f3 | tr -d ' ')
CPU_KV=$(cat <<KV
cpu_busy_ms	${CPU_BUSY:-0}
idle_ms	${CPU_IDLE:-0}
time_ticks	${CPU_TICKS:-0}
KV
)
CPU_HTML=$(build_kv_table "$CPU_KV")

# ===========================================================================
# 9. Blocking
# ===========================================================================
echo "[9/17] 采集阻塞进程..."
BLOCK_DATA=$(run_sql_with_headers "SELECT session_id, blocking_session_id, wait_duration_ms, wait_type FROM sys.dm_os_waiting_tasks WHERE blocking_session_id <> 0")
if [ -z "$BLOCK_DATA" ] || [ "$(echo "$BLOCK_DATA" | wc -l)" -le 1 ]; then
  BLOCK_HTML="<p>无阻塞进程</p>"
else
  BLOCK_HTML=$(build_multi_table "$BLOCK_DATA")
fi

# ===========================================================================
# 10. Lock info
# ===========================================================================
echo "[10/17] 采集锁信息..."
LOCK_RAW=$(run_sql "SELECT CAST((SELECT COUNT(*) FROM sys.dm_tran_locks) AS NVARCHAR(20)) + '|' + CAST(ISNULL((SELECT MAX(wt.wait_duration_ms) FROM sys.dm_os_waiting_tasks wt JOIN sys.dm_tran_locks tl ON wt.resource_address = tl.lock_owner_address WHERE wt.wait_type LIKE 'LCK%'), 0) AS NVARCHAR(20))" | head -1)
LOCK_COUNT=$(echo "$LOCK_RAW" | cut -d'|' -f1 | tr -d ' ')
LOCK_MAX_WAIT=$(echo "$LOCK_RAW" | cut -d'|' -f2 | tr -d ' ')
LOCK_KV=$(cat <<KV
lock_count	${LOCK_COUNT:-0}
max_wait_time	${LOCK_MAX_WAIT:-0}
KV
)
LOCK_HTML=$(build_kv_table "$LOCK_KV")

# ===========================================================================
# 11. Deadlock info
# ===========================================================================
echo "[11/17] 采集死锁信息..."
DL_COUNT=$(run_sql "BEGIN TRY SELECT COUNT(*) FROM sys.fn_xe_file_target_read_file('DeadlockTracking*.xel', NULL, NULL, NULL) WHERE object_name = 'xml_deadlock_report' END TRY BEGIN CATCH SELECT 0 END CATCH" 2>/dev/null | head -1)
DL_COUNT=$(echo "$DL_COUNT" | tr -d ' ')
DEADLOCK_KV=$(printf "deadlock_count\t%s" "${DL_COUNT:-0}")
DEADLOCK_HTML=$(build_kv_table "$DEADLOCK_KV")

# ===========================================================================
# 12. Backup info
# ===========================================================================
echo "[12/17] 采集备份信息..."
BACKUP_DATA=$(run_sql_with_headers "SELECT database_name, CONVERT(NVARCHAR(20), MAX(backup_finish_date), 120) AS last_backup_time, CASE MAX(type) WHEN 'D' THEN N'完整' WHEN 'I' THEN N'差异' WHEN 'L' THEN N'日志' ELSE N'其他' END AS backup_type, CAST(MAX(backup_size)/1024.0/1024.0 AS DECIMAL(10,2)) AS backup_size_mb FROM msdb.dbo.backupset GROUP BY database_name ORDER BY database_name")
if [ -z "$BACKUP_DATA" ] || [ "$(echo "$BACKUP_DATA" | wc -l)" -le 1 ]; then
  BACKUP_HTML="<p>无备份信息</p>"
else
  BACKUP_HTML=$(build_multi_table "$BACKUP_DATA")
fi

# ===========================================================================
# 13. Log space
# ===========================================================================
echo "[13/17] 采集日志空间..."
LOG_DATA=$(run_sql_with_headers "CREATE TABLE #logspace (DatabaseName sysname, LogSizeMB float, LogSpaceUsedPercent float, Status int); INSERT #logspace EXEC ('DBCC SQLPERF(LOGSPACE)'); SELECT DatabaseName, CAST(LogSizeMB AS DECIMAL(10,2)) AS LogSizeMB, CAST(LogSpaceUsedPercent AS DECIMAL(5,2)) AS LogSpaceUsedPercent, Status FROM #logspace; DROP TABLE #logspace;" 2>/dev/null)
LOG_HTML=$(build_multi_table "$LOG_DATA")

# ===========================================================================
# 14. Memory usage
# ===========================================================================
echo "[14/17] 采集 SQL Server 内存..."
MEM_RAW=$(run_sql "SELECT CAST(CAST(physical_memory_in_use_kb/1024.0 AS DECIMAL(10,2)) AS NVARCHAR(20)) + '|' + CAST(CAST(available_commit_limit_kb/1024.0 AS DECIMAL(10,2)) AS NVARCHAR(20)) FROM sys.dm_os_process_memory" | head -1)
MEM_USED=$(echo "$MEM_RAW" | cut -d'|' -f1 | tr -d ' ')
MEM_AVAIL=$(echo "$MEM_RAW" | cut -d'|' -f2 | tr -d ' ')
MEM_KV=$(cat <<KV
memory_used_mb	${MEM_USED:-0}
available_mb	${MEM_AVAIL:-0}
KV
)
MEMORY_HTML=$(build_kv_table "$MEM_KV")

# ===========================================================================
# 15. Linked servers
# ===========================================================================
echo "[15/17] 采集链接服务器..."
LINKED_DATA=$(run_sql_with_headers "EXEC sp_linkedservers" 10)
if [ -z "$LINKED_DATA" ] || [ "$(echo "$LINKED_DATA" | wc -l)" -le 1 ]; then
  LINKED_HTML="<p>无链接服务器</p>"
else
  LINKED_HTML=$(build_multi_table "$LINKED_DATA")
fi

# ===========================================================================
# 16. File groups
# ===========================================================================
echo "[16/17] 采集文件组..."
FG_DATA=$(run_sql_with_headers "SELECT data_space_id, name, type_desc, is_default FROM sys.filegroups")
FG_HTML=$(build_multi_table "$FG_DATA")

# ===========================================================================
# 17. System info (disk / memory / cpu)
# ===========================================================================
echo "[17/17] 采集主机资源..."

# --- Disk ---
DISK_HTML="<table><tr><th>挂载点</th><th>总大小(GB)</th><th>可用(GB)</th><th>使用率(%)</th></tr>"
if command -v df >/dev/null 2>&1; then
  df -BG --output=target,size,avail,pcent -x tmpfs -x devtmpfs 2>/dev/null | tail -n +2 | while read -r mount total avail pct; do
    total_n=$(echo "$total" | tr -d 'G')
    avail_n=$(echo "$avail" | tr -d 'G')
    pct_n=$(echo "$pct" | tr -d '%')
    mount_e=$(html_escape "$mount")
    DISK_HTML="${DISK_HTML}<tr><td>${mount_e}</td><td>${total_n}</td><td>${avail_n}</td><td>${pct_n}</td></tr>"
    echo "<tr><td>${mount_e}</td><td>${total_n}</td><td>${avail_n}</td><td>${pct_n}</td></tr>"
  done
  DISK_ROWS=$(df -BG --output=target,size,avail,pcent -x tmpfs -x devtmpfs 2>/dev/null | tail -n +2 | while read -r mount total avail pct; do
    total_n=$(echo "$total" | tr -d 'G')
    avail_n=$(echo "$avail" | tr -d 'G')
    pct_n=$(echo "$pct" | tr -d '%')
    mount_e=$(html_escape "$mount")
    echo "<tr><td>${mount_e}</td><td>${total_n}</td><td>${avail_n}</td><td>${pct_n}</td></tr>"
  done)
  DISK_HTML="<table><tr><th>挂载点</th><th>总大小(GB)</th><th>可用(GB)</th><th>使用率(%)</th></tr>${DISK_ROWS}</table>"
else
  DISK_HTML="<table><tr><th>挂载点</th><th>总大小(GB)</th><th>可用(GB)</th><th>使用率(%)</th></tr><tr><td colspan='4'>采集失败</td></tr></table>"
fi

# --- Memory ---
MEM_SYS_HTML="<table><tr><th>内存类型</th><th>总大小(GB)</th><th>已用(GB)</th><th>可用(GB)</th><th>使用率(%)</th></tr>"
if [ -f /proc/meminfo ]; then
  MEM_TOTAL_KB=$(grep '^MemTotal:' /proc/meminfo | awk '{print $2}')
  MEM_AVAIL_KB=$(grep '^MemAvailable:' /proc/meminfo | awk '{print $2}')
  if [ -n "$MEM_TOTAL_KB" ] && [ -n "$MEM_AVAIL_KB" ]; then
    MEM_TOTAL_GB=$(awk "BEGIN {printf \"%.2f\", $MEM_TOTAL_KB/1048576}")
    MEM_AVAIL_GB=$(awk "BEGIN {printf \"%.2f\", $MEM_AVAIL_KB/1048576}")
    MEM_USED_GB=$(awk "BEGIN {printf \"%.2f\", ($MEM_TOTAL_KB-$MEM_AVAIL_KB)/1048576}")
    MEM_PCT=$(awk "BEGIN {printf \"%.1f\", ($MEM_TOTAL_KB-$MEM_AVAIL_KB)/$MEM_TOTAL_KB*100}")
    MEM_SYS_HTML="${MEM_SYS_HTML}<tr><td>物理内存</td><td>${MEM_TOTAL_GB}</td><td>${MEM_USED_GB}</td><td>${MEM_AVAIL_GB}</td><td>${MEM_PCT}</td></tr>"
  else
    MEM_SYS_HTML="${MEM_SYS_HTML}<tr><td colspan='5'>采集失败</td></tr>"
  fi
else
  MEM_SYS_HTML="${MEM_SYS_HTML}<tr><td colspan='5'>采集失败</td></tr>"
fi
MEM_SYS_HTML="${MEM_SYS_HTML}</table>"

# --- CPU ---
CPU_SYS_HTML="<table><tr><th>指标</th><th>值</th></tr>"
if command -v mpstat >/dev/null 2>&1; then
  CPU_PCT=$(mpstat 1 1 2>/dev/null | tail -1 | awk '{print 100 - $NF}')
  CPU_SYS_HTML="${CPU_SYS_HTML}<tr><td>CPU使用率</td><td>${CPU_PCT:-N/A}%</td></tr>"
elif [ -f /proc/stat ]; then
  # Fallback: read /proc/stat twice with 1s interval
  read -r _ u1 n1 s1 i1 _ < /proc/stat
  sleep 1
  read -r _ u2 n2 s2 i2 _ < /proc/stat
  TOTAL1=$((u1+n1+s1+i1))
  TOTAL2=$((u2+n2+s2+i2))
  IDLE_DIFF=$((i2-i1))
  TOTAL_DIFF=$((TOTAL2-TOTAL1))
  if [ "$TOTAL_DIFF" -gt 0 ]; then
    CPU_PCT=$(awk "BEGIN {printf \"%.1f\", (1-$IDLE_DIFF/$TOTAL_DIFF)*100}")
  else
    CPU_PCT="N/A"
  fi
  CPU_SYS_HTML="${CPU_SYS_HTML}<tr><td>CPU使用率</td><td>${CPU_PCT}%</td></tr>"
else
  CPU_SYS_HTML="${CPU_SYS_HTML}<tr><td>CPU使用率</td><td>N/A</td></tr>"
fi
CPU_SYS_HTML="${CPU_SYS_HTML}</table>"

# ===========================================================================
# Build HTML
# ===========================================================================
echo ""
echo "生成 HTML 报告..."

cat > "$OUTPUT" <<HTML_EOF
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="db-type" content="sqlserver">
<meta name="generator" content="sqlserver_check.ps1">
<meta name="version" content="${SCRIPT_VERSION}">
<meta name="collect-time" content="${COLLECT_TIME}">
<meta name="instance-name" content="${INSTANCE_NAME}">
<meta name="instance-host" content="${SERVER}">
<meta name="instance-port" content="${PORT}">
<meta name="sqlserver-version" content="${SS_VERSION:-N/A}">
<meta name="sqlserver-edition" content="${SS_EDITION:-N/A}">
<meta name="sqlserver-product-level" content="${SS_PRODUCT_LEVEL:-N/A}">
<title>SQL Server 巡检报告 - ${INSTANCE_NAME} - ${COLLECT_TIME}</title>
<style>
body { font-family: 'Microsoft YaHei', sans-serif; margin: 20px; background: #f5f5f5; }
h1 { color: #1565C0; border-bottom: 2px solid #1565C0; padding-bottom: 8px; }
h2 { color: #1976D2; margin-top: 20px; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; background: #fff; }
th, td { border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 13px; }
th { background: #1565C0; color: #fff; }
tr:nth-child(even) { background: #f9f9f9; }
.section { margin: 15px 0; padding: 15px; background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
</style>
</head>
<body>
<h1>SQL Server 巡检报告</h1>
<p>实例: ${INSTANCE_NAME} | 采集时间: ${COLLECT_TIME} | 脚本版本: v${SCRIPT_VERSION}</p>

<div class="section" id="version_info">
<h2>一、版本信息</h2>
${VERSION_HTML}
</div>

<div class="section" id="connection_info">
<h2>二、连接信息</h2>
${CONN_HTML}
</div>

<div class="section" id="active_sessions">
<h2>三、活动会话</h2>
${SESSION_HTML}
</div>

<div class="section" id="database_size">
<h2>四、数据库大小</h2>
${DBSIZE_HTML}
</div>

<div class="section" id="database_status">
<h2>五、数据库状态</h2>
${DBSTATUS_HTML}
</div>

<div class="section" id="config_parameters">
<h2>六、关键配置</h2>
${CONFIG_HTML}
</div>

<div class="section" id="wait_stats">
<h2>七、等待统计 (TOP 10)</h2>
${WAIT_HTML}
</div>

<div class="section" id="cpu_usage">
<h2>八、CPU 信息</h2>
${CPU_HTML}
</div>

<div class="section" id="blocking">
<h2>九、阻塞进程</h2>
${BLOCK_HTML}
</div>

<div class="section" id="lock_info">
<h2>十、锁信息</h2>
${LOCK_HTML}
</div>

<div class="section" id="deadlock_info">
<h2>十一、死锁信息</h2>
${DEADLOCK_HTML}
</div>

<div class="section" id="backup_info">
<h2>十二、备份信息</h2>
${BACKUP_HTML}
</div>

<div class="section" id="log_space">
<h2>十三、日志空间</h2>
${LOG_HTML}
</div>

<div class="section" id="memory_usage">
<h2>十四、SQL Server 内存</h2>
${MEMORY_HTML}
</div>

<div class="section" id="linked_servers">
<h2>十五、链接服务器</h2>
${LINKED_HTML}
</div>

<div class="section" id="file_groups">
<h2>十六、文件组</h2>
${FG_HTML}
</div>

<div class="section" id="system_info">
<h2>十七、主机资源</h2>
<h3>磁盘</h3>
${DISK_HTML}
<h3>内存</h3>
${MEM_SYS_HTML}
<h3>CPU</h3>
${CPU_SYS_HTML}
</div>

<hr>
<p style="color:#999; font-size:12px;">
生成时间: ${COLLECT_TIME} | 脚本: mssql_check.sh v${SCRIPT_VERSION} | 平台: ora100.com
</p>
</body>
</html>
HTML_EOF

echo ""
echo "采集完成！报告已保存至: ${OUTPUT}"
echo "请将此文件上传至 ora100.com/oracheck-report 生成专业巡检报告。"
