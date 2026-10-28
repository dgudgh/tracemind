#!/bin/sh
# ============================================================================
# pg_check.sh — PostgreSQL 巡检采集脚本
# 版本: 1.0
# 用法: sh pg_check.sh -H <host> -P <port> -u <user> -d <dbname> [-n <name>] [-o <file>]
#
# 依赖: psql (PostgreSQL 客户端)
# 认证: 支持 ~/.pgpass / PGPASSWORD 环境变量 / -W 交互输入
# 输出: pg_check_<name>_<YYYYMMDD_HHMMSS>.html
# ============================================================================

SCRIPT_VERSION="1.0"

# --- 默认参数 ---
PG_HOST="127.0.0.1"
PG_PORT="5432"
PG_USER="postgres"
PG_DBNAME="postgres"
INSTANCE_NAME=""
OUTPUT_FILE=""
COLLECT_SYSTEM="yes"
SQL_TIMEOUT=30
CONNECT_TIMEOUT=10
TOP_TABLES_LIMIT=20
LONG_QUERY_SECONDS=30
PROCESSLIST_LIMIT=50

# --- 颜色 (stderr) ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SECTION_TOTAL=13
SECTION_CURRENT=0

# ============================================================================
# 帮助
# ============================================================================
usage() {
  cat <<'USAGE'
用法: sh pg_check.sh [选项]

连接选项:
  -H, --host <host>        PostgreSQL 主机地址 (默认: 127.0.0.1)
  -P, --port <port>        端口 (默认: 5432)
  -u, --user <user>        用户名 (默认: postgres)
  -d, --dbname <dbname>    数据库名 (默认: postgres)
  -W, --password           交互输入密码
  -n, --name <name>        实例名称 (默认: host:port)
  -o, --output <file>      输出文件名

采集选项:
  --no-system              跳过系统信息采集
  --timeout <seconds>      SQL 超时 (默认: 30)

其他:
  -h, --help               显示帮助

认证方式:
  1. ~/.pgpass 文件 (推荐)
  2. PGPASSWORD 环境变量
  3. -W 交互输入密码

示例:
  sh pg_check.sh -H 192.168.1.100 -P 5432 -u postgres -d mydb
  PGPASSWORD=secret sh pg_check.sh -H db.example.com
USAGE
  exit 0
}

# ============================================================================
# 参数解析
# ============================================================================
PG_PASSWORD_SET=""
while [ $# -gt 0 ]; do
  case $1 in
    -H|--host)      PG_HOST="$2"; shift 2 ;;
    -P|--port)      PG_PORT="$2"; shift 2 ;;
    -u|--user)      PG_USER="$2"; shift 2 ;;
    -d|--dbname)    PG_DBNAME="$2"; shift 2 ;;
    -W|--password)
      printf "  PostgreSQL 密码: " >&2
      stty -echo 2>/dev/null
      read PG_PASS
      stty echo 2>/dev/null
      echo "" >&2
      export PGPASSWORD="$PG_PASS"
      PG_PASSWORD_SET="yes"
      shift
      ;;
    -n|--name)      INSTANCE_NAME="$2"; shift 2 ;;
    -o|--output)    OUTPUT_FILE="$2"; shift 2 ;;
    --no-system)    COLLECT_SYSTEM="no"; shift ;;
    --timeout)      SQL_TIMEOUT="$2"; shift 2 ;;
    -h|--help)      usage ;;
    *) echo "未知参数: $1" >&2; usage ;;
  esac
done

if [ -z "$INSTANCE_NAME" ]; then
  INSTANCE_NAME="${PG_HOST}:${PG_PORT}"
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
COLLECT_TIME=$(date +"%Y-%m-%dT%H:%M:%S")
if [ -z "$OUTPUT_FILE" ]; then
  OUTPUT_FILE="pg_check_${INSTANCE_NAME}_${TIMESTAMP}.html"
  OUTPUT_FILE=$(echo "$OUTPUT_FILE" | sed 's/:/_/g')
fi

# ============================================================================
# psql 连接参数
# ============================================================================
PSQL_BASE="psql -h ${PG_HOST} -p ${PG_PORT} -U ${PG_USER} -d ${PG_DBNAME} --no-password"
PSQL_CONN="${PSQL_BASE} -v ON_ERROR_STOP=0 --pset=footer=off"

# ============================================================================
# 进度显示
# ============================================================================
next_section() {
  SECTION_CURRENT=$((SECTION_CURRENT + 1))
  printf "${BLUE}[%d/%d]${NC} %s\n" "$SECTION_CURRENT" "$SECTION_TOTAL" "$1" >&2
}

# ============================================================================
# SQL 执行函数
# ============================================================================
run_sql() {
  timeout "$SQL_TIMEOUT" $PSQL_CONN -t -A -c "$1" 2>/dev/null
}

run_sql_html() {
  timeout "$SQL_TIMEOUT" $PSQL_CONN --html -c "$1" 2>/dev/null
}

safe_sql_html() {
  _result=$(run_sql_html "$1")
  if [ -z "$_result" ] || ! echo "$_result" | grep -q '<t'; then
    echo "<p>${2:-无法获取数据}</p>"
  else
    echo "$_result"
  fi
}

# ============================================================================
# HTML 输出函数
# ============================================================================
section_start() {
  echo "<div class=\"section\" id=\"$1\">"
  echo "<h2>$2</h2>"
}

section_end() {
  echo "</div>"
}

kv_table_start() {
  echo "<table><tr><th>项目</th><th>值</th></tr>"
}

kv_table_row() {
  echo "<tr><td>$1</td><td>$2</td></tr>"
}

kv_table_end() {
  echo "</table>"
}

subsection() {
  echo "<h3>$1</h3>"
}

# ============================================================================
# 连接测试
# ============================================================================
printf "${BLUE}=== PostgreSQL 巡检采集脚本 v${SCRIPT_VERSION} ===${NC}\n" >&2
printf "  目标: ${PG_HOST}:${PG_PORT}/${PG_DBNAME}\n" >&2
printf "  用户: ${PG_USER}\n" >&2
printf "  测试连接..." >&2

PG_VERSION=$(run_sql "SELECT version();" 2>/dev/null)
if [ -z "$PG_VERSION" ]; then
  printf " ${RED}失败${NC}\n" >&2
  echo "无法连接到 PostgreSQL，请检查连接参数和认证配置。" >&2
  exit 1
fi

PG_VERSION_SHORT=$(echo "$PG_VERSION" | grep -oP 'PostgreSQL \K[0-9]+\.[0-9]+' || echo "$PG_VERSION" | awk '{print $2}')
PG_VERSION_MAJOR=$(echo "$PG_VERSION_SHORT" | cut -d. -f1)
printf " ${GREEN}成功${NC} (PostgreSQL %s)\n" "$PG_VERSION_SHORT" >&2
printf "  输出文件: ${OUTPUT_FILE}\n\n" >&2

# ============================================================================
# HTML 头部
# ============================================================================
cat > "$OUTPUT_FILE" <<HTMLHEAD
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="generator" content="pg_check.sh">
<meta name="version" content="${SCRIPT_VERSION}">
<meta name="db-type" content="postgresql">
<meta name="collect-time" content="${COLLECT_TIME}">
<meta name="instance-name" content="${INSTANCE_NAME}">
<meta name="instance-host" content="${PG_HOST}">
<meta name="instance-port" content="${PG_PORT}">
<meta name="pg-version" content="${PG_VERSION_SHORT}">
<title>PostgreSQL 巡检报告 - ${INSTANCE_NAME}</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:20px;background:#f5f5f5;color:#333}
.header{background:linear-gradient(135deg,#336791,#264d73);color:#fff;padding:24px 30px;border-radius:8px;margin-bottom:20px}
.header h1{margin:0 0 4px;font-size:22px}
.header p{margin:2px 0;font-size:13px;opacity:0.9}
h2{color:#336791;font-size:16px;border-left:4px solid #336791;padding-left:10px;margin:18px 0 10px}
h3{color:#264d73;font-size:14px;margin:14px 0 8px}
.section{background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.08)}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
th{background:#336791;color:#fff;padding:8px 12px;text-align:left;font-weight:600}
td{padding:6px 12px;border-bottom:1px solid #e0e0e0}
tr:nth-child(even){background:#f8f9fa}
.warn{color:#e65100;font-weight:600}
.critical{color:#c62828;font-weight:600}
.ok{color:#2e7d32;font-weight:600}
</style>
</head>
<body>
<div class="header">
<h1>PostgreSQL 巡检报告</h1>
<p>实例: ${INSTANCE_NAME} | 地址: ${PG_HOST}:${PG_PORT} | 版本: ${PG_VERSION_SHORT} | 采集时间: ${COLLECT_TIME}</p>
</div>
HTMLHEAD

# ============================================================================
# Section 1: 基本信息
# ============================================================================
next_section "基本信息"
{
  section_start "basic_info" "1. 基本信息"

  PG_STARTUP=$(run_sql "SELECT pg_postmaster_start_time()::text;")
  PG_UPTIME=$(run_sql "SELECT (now() - pg_postmaster_start_time())::text;")
  PG_DATADIR=$(run_sql "SHOW data_directory;")
  PG_CONFIG_FILE=$(run_sql "SHOW config_file;")
  PG_IS_RECOVERY=$(run_sql "SELECT pg_is_in_recovery()::text;")
  PG_CLUSTER_NAME=$(run_sql "SHOW cluster_name;" 2>/dev/null || echo "")
  PG_SERVER_ENCODING=$(run_sql "SHOW server_encoding;")

  kv_table_start
  kv_table_row "实例名称" "$INSTANCE_NAME"
  kv_table_row "服务器地址" "${PG_HOST}:${PG_PORT}"
  kv_table_row "数据库名" "$PG_DBNAME"
  kv_table_row "采集时间" "$COLLECT_TIME"
  kv_table_row "PostgreSQL 版本" "$PG_VERSION_SHORT"
  kv_table_row "启动时间" "$PG_STARTUP"
  kv_table_row "运行时长" "$PG_UPTIME"
  kv_table_row "数据目录" "$PG_DATADIR"
  kv_table_row "配置文件" "$PG_CONFIG_FILE"
  kv_table_row "服务器编码" "$PG_SERVER_ENCODING"
  kv_table_row "恢复模式" "$PG_IS_RECOVERY"
  if [ -n "$PG_CLUSTER_NAME" ]; then
    kv_table_row "集群名称" "$PG_CLUSTER_NAME"
  fi
  kv_table_end

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 2: 连接信息
# ============================================================================
next_section "连接信息"
{
  section_start "connection" "2. 连接信息"

  PG_MAX_CONN=$(run_sql "SHOW max_connections;")
  PG_SUPERUSER_RESERVED=$(run_sql "SHOW superuser_reserved_connections;")
  PG_CURRENT_CONN=$(run_sql "SELECT count(*) FROM pg_stat_activity;")

  subsection "2.1 连接概览"
  kv_table_start
  kv_table_row "最大连接数 (max_connections)" "$PG_MAX_CONN"
  kv_table_row "超级用户预留 (superuser_reserved_connections)" "$PG_SUPERUSER_RESERVED"
  kv_table_row "当前连接数" "$PG_CURRENT_CONN"
  if [ -n "$PG_MAX_CONN" ] && [ "$PG_MAX_CONN" -gt 0 ] 2>/dev/null; then
    PG_CONN_PCT=$(( PG_CURRENT_CONN * 100 / PG_MAX_CONN ))
    kv_table_row "连接使用率" "${PG_CONN_PCT}%"
  fi
  kv_table_end

  subsection "2.2 连接状态分布"
  safe_sql_html "
    SELECT state AS \"状态\",
           count(*) AS \"数量\",
           round(100.0 * count(*) / greatest(sum(count(*)) over(), 1), 1) AS \"占比(%)\"
    FROM pg_stat_activity
    WHERE pid <> pg_backend_pid()
    GROUP BY state
    ORDER BY count(*) DESC;
  " "无法获取连接状态"

  subsection "2.3 按用户连接分布"
  safe_sql_html "
    SELECT usename AS \"用户\",
           count(*) AS \"连接数\",
           count(*) FILTER (WHERE state = 'active') AS \"活跃\",
           count(*) FILTER (WHERE state = 'idle') AS \"空闲\"
    FROM pg_stat_activity
    WHERE pid <> pg_backend_pid()
    GROUP BY usename
    ORDER BY count(*) DESC
    LIMIT 15;
  " "无法获取用户连接分布"

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 3: 数据库信息
# ============================================================================
next_section "数据库信息"
{
  section_start "database_info" "3. 数据库信息"

  safe_sql_html "
    SELECT d.datname AS \"数据库\",
           pg_size_pretty(pg_database_size(d.datname)) AS \"大小\",
           d.encoding AS \"编码ID\",
           pg_encoding_to_char(d.encoding) AS \"编码\",
           d.datcollate AS \"排序规则\",
           d.datconnlimit AS \"连接限制\",
           s.numbackends AS \"当前连接\"
    FROM pg_database d
    LEFT JOIN pg_stat_database s ON d.datname = s.datname
    WHERE d.datistemplate = false
    ORDER BY pg_database_size(d.datname) DESC;
  " "无法获取数据库信息"

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 4: 表统计
# ============================================================================
next_section "表统计"
{
  section_start "table_stats" "4. 表统计（Top ${TOP_TABLES_LIMIT}）"

  safe_sql_html "
    SELECT schemaname AS \"模式\",
           relname AS \"表名\",
           n_live_tup AS \"活跃行数\",
           n_dead_tup AS \"死元组数\",
           CASE WHEN n_live_tup + n_dead_tup > 0
                THEN round(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 1)
                ELSE 0
           END AS \"死元组率(%)\",
           to_char(last_vacuum, 'YYYY-MM-DD HH24:MI') AS \"上次VACUUM\",
           to_char(last_autovacuum, 'YYYY-MM-DD HH24:MI') AS \"上次自动VACUUM\",
           to_char(last_analyze, 'YYYY-MM-DD HH24:MI') AS \"上次ANALYZE\"
    FROM pg_stat_user_tables
    ORDER BY n_dead_tup DESC
    LIMIT ${TOP_TABLES_LIMIT};
  " "无法获取表统计"

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 5: 索引使用
# ============================================================================
next_section "索引使用"
{
  section_start "index_usage" "5. 索引使用分析"

  subsection "5.1 索引扫描率（Top ${TOP_TABLES_LIMIT}）"
  safe_sql_html "
    SELECT schemaname AS \"模式\",
           relname AS \"表名\",
           idx_scan AS \"索引扫描次数\",
           seq_scan AS \"顺序扫描次数\",
           CASE WHEN idx_scan + seq_scan > 0
                THEN round(100.0 * idx_scan / (idx_scan + seq_scan), 1)
                ELSE 0
           END AS \"索引扫描率(%)\"
    FROM pg_stat_user_tables
    WHERE seq_scan + idx_scan > 100
    ORDER BY seq_scan DESC
    LIMIT ${TOP_TABLES_LIMIT};
  " "无法获取索引扫描率"

  subsection "5.2 未使用索引"
  safe_sql_html "
    SELECT s.schemaname AS \"模式\",
           s.relname AS \"表名\",
           s.indexrelname AS \"索引名\",
           pg_size_pretty(pg_relation_size(s.indexrelid)) AS \"索引大小\",
           s.idx_scan AS \"扫描次数\"
    FROM pg_stat_user_indexes s
    JOIN pg_index i ON s.indexrelid = i.indexrelid
    WHERE s.idx_scan = 0
      AND NOT i.indisunique
      AND NOT i.indisprimary
      AND pg_relation_size(s.indexrelid) > 65536
    ORDER BY pg_relation_size(s.indexrelid) DESC
    LIMIT 20;
  " "无法获取未使用索引（或不存在未使用索引）"

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 6: 性能指标
# ============================================================================
next_section "性能指标"
{
  section_start "performance" "6. 性能指标"

  subsection "6.1 缓存命中率"
  safe_sql_html "
    SELECT datname AS \"数据库\",
           blks_read AS \"物理读\",
           blks_hit AS \"缓存命中\",
           CASE WHEN blks_read + blks_hit > 0
                THEN round(100.0 * blks_hit / (blks_read + blks_hit), 2)
                ELSE 0
           END AS \"命中率(%)\"
    FROM pg_stat_database
    WHERE datname NOT LIKE 'template%'
    ORDER BY blks_read + blks_hit DESC;
  " "无法获取缓存命中率"

  subsection "6.2 BGWriter 统计"
  # PG 17+ moved checkpoint stats from pg_stat_bgwriter to pg_stat_checkpointer
  # PG ≤16: checkpoints_timed, checkpoints_req, buffers_checkpoint, buffers_clean, buffers_backend, buffers_alloc in pg_stat_bgwriter
  # PG 17+: pg_stat_bgwriter only has buffers_clean, buffers_alloc; checkpoint & backend stats in pg_stat_checkpointer
  if [ "$PG_VERSION_MAJOR" -ge 17 ] 2>/dev/null; then
    safe_sql_html "
      SELECT c.num_timed AS \"定时检查点\",
             c.num_requested AS \"请求检查点\",
             c.buffers_written AS \"检查点写缓冲\",
             b.buffers_clean AS \"后台清理缓冲\",
             io.writes AS \"后端直接写\",
             b.buffers_alloc AS \"分配缓冲\"
      FROM pg_stat_checkpointer c
      CROSS JOIN pg_stat_bgwriter b
      LEFT JOIN (
        SELECT sum(writes) AS writes
        FROM pg_stat_io
        WHERE backend_type = 'client backend' AND context = 'normal'
      ) io ON true;
    " "无法获取 BGWriter 统计"
  else
    safe_sql_html "
      SELECT checkpoints_timed AS \"定时检查点\",
             checkpoints_req AS \"请求检查点\",
             buffers_checkpoint AS \"检查点写缓冲\",
             buffers_clean AS \"后台清理缓冲\",
             buffers_backend AS \"后端直接写\",
             buffers_alloc AS \"分配缓冲\"
      FROM pg_stat_bgwriter;
    " "无法获取 BGWriter 统计"
  fi

  subsection "6.3 数据库事务统计"
  safe_sql_html "
    SELECT datname AS \"数据库\",
           xact_commit AS \"提交事务\",
           xact_rollback AS \"回滚事务\",
           CASE WHEN xact_commit + xact_rollback > 0
                THEN round(100.0 * xact_rollback / (xact_commit + xact_rollback), 2)
                ELSE 0
           END AS \"回滚率(%)\"
    FROM pg_stat_database
    WHERE datname NOT LIKE 'template%'
      AND xact_commit + xact_rollback > 0
    ORDER BY xact_commit + xact_rollback DESC;
  " "无法获取事务统计"

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 7: 复制状态
# ============================================================================
next_section "复制状态"
{
  section_start "replication" "7. 复制状态"

  PG_WAL_LEVEL=$(run_sql "SHOW wal_level;")
  PG_ARCHIVE_MODE=$(run_sql "SHOW archive_mode;")
  PG_MAX_WAL_SENDERS=$(run_sql "SHOW max_wal_senders;")

  kv_table_start
  kv_table_row "wal_level" "$PG_WAL_LEVEL"
  kv_table_row "archive_mode" "$PG_ARCHIVE_MODE"
  kv_table_row "max_wal_senders" "$PG_MAX_WAL_SENDERS"
  kv_table_row "恢复模式" "$PG_IS_RECOVERY"
  kv_table_end

  subsection "7.1 复制插槽"
  safe_sql_html "
    SELECT slot_name AS \"插槽名\",
           plugin AS \"插件\",
           slot_type AS \"类型\",
           active::text AS \"活跃\",
           restart_lsn::text AS \"重启LSN\"
    FROM pg_replication_slots
    ORDER BY slot_name;
  " "无复制插槽"

  if [ "$PG_IS_RECOVERY" = "f" ] || [ "$PG_IS_RECOVERY" = "false" ]; then
    subsection "7.2 WAL 发送进程（主库）"
    safe_sql_html "
      SELECT pid AS \"PID\",
             usename AS \"用户\",
             client_addr::text AS \"客户端\",
             state AS \"状态\",
             sent_lsn::text AS \"已发送LSN\",
             replay_lsn::text AS \"已回放LSN\",
             CASE WHEN sent_lsn IS NOT NULL AND replay_lsn IS NOT NULL
                  THEN pg_wal_lsn_diff(sent_lsn, replay_lsn)::text
                  ELSE 'N/A'
             END AS \"延迟(字节)\",
             sync_state AS \"同步状态\"
      FROM pg_stat_replication
      ORDER BY client_addr;
    " "无活跃复制连接"
  else
    subsection "7.2 WAL 接收进程（备库）"
    safe_sql_html "
      SELECT pid AS \"PID\",
             status AS \"状态\",
             sender_host AS \"主库地址\",
             sender_port AS \"主库端口\",
             written_lsn::text AS \"已写入LSN\",
             latest_end_lsn::text AS \"最新LSN\"
      FROM pg_stat_wal_receiver;
    " "无 WAL 接收进程"
  fi

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 8: 锁信息
# ============================================================================
next_section "锁信息"
{
  section_start "lock_info" "8. 锁信息"

  subsection "8.1 锁概览"
  safe_sql_html "
    SELECT mode AS \"锁模式\",
           count(*) AS \"数量\",
           count(*) FILTER (WHERE granted) AS \"已授予\",
           count(*) FILTER (WHERE NOT granted) AS \"等待中\"
    FROM pg_locks
    GROUP BY mode
    ORDER BY count(*) DESC;
  " "无法获取锁信息"

  subsection "8.2 锁等待详情"
  safe_sql_html "
    SELECT blocked.pid AS \"阻塞PID\",
           blocked_activity.usename AS \"阻塞用户\",
           blocked.mode AS \"请求锁模式\",
           blocking.pid AS \"持锁PID\",
           blocking_activity.usename AS \"持锁用户\",
           blocking_activity.state AS \"持锁状态\",
           left(blocking_activity.query, 120) AS \"持锁SQL\"
    FROM pg_locks blocked
    JOIN pg_locks blocking ON blocking.locktype = blocked.locktype
      AND blocking.database IS NOT DISTINCT FROM blocked.database
      AND blocking.relation IS NOT DISTINCT FROM blocked.relation
      AND blocking.page IS NOT DISTINCT FROM blocked.page
      AND blocking.tuple IS NOT DISTINCT FROM blocked.tuple
      AND blocking.transactionid IS NOT DISTINCT FROM blocked.transactionid
      AND blocking.pid <> blocked.pid
      AND blocking.granted
    JOIN pg_stat_activity blocked_activity ON blocked.pid = blocked_activity.pid
    JOIN pg_stat_activity blocking_activity ON blocking.pid = blocking_activity.pid
    WHERE NOT blocked.granted
    LIMIT 20;
  " "无锁等待"

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 9: 长查询
# ============================================================================
next_section "长查询"
{
  section_start "long_queries" "9. 长查询（> ${LONG_QUERY_SECONDS}s）"

  safe_sql_html "
    SELECT pid AS \"PID\",
           usename AS \"用户\",
           datname AS \"数据库\",
           state AS \"状态\",
           round(extract(epoch FROM (now() - query_start)))::int AS \"运行秒数\",
           wait_event_type AS \"等待类型\",
           wait_event AS \"等待事件\",
           left(query, 200) AS \"SQL\"
    FROM pg_stat_activity
    WHERE state <> 'idle'
      AND pid <> pg_backend_pid()
      AND query_start < now() - interval '${LONG_QUERY_SECONDS} seconds'
    ORDER BY query_start ASC
    LIMIT ${PROCESSLIST_LIMIT};
  " "无长查询"

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 10: 等待事件
# ============================================================================
next_section "等待事件"
{
  section_start "wait_events" "10. 等待事件"

  safe_sql_html "
    SELECT wait_event_type AS \"等待类型\",
           wait_event AS \"等待事件\",
           count(*) AS \"会话数\",
           string_agg(DISTINCT state, ', ') AS \"会话状态\"
    FROM pg_stat_activity
    WHERE wait_event IS NOT NULL
      AND pid <> pg_backend_pid()
    GROUP BY wait_event_type, wait_event
    ORDER BY count(*) DESC
    LIMIT 20;
  " "无等待事件"

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 11: 安全
# ============================================================================
next_section "安全信息"
{
  section_start "security" "11. 安全信息"

  subsection "11.1 用户角色"
  safe_sql_html "
    SELECT rolname AS \"角色\",
           rolsuper::text AS \"超级用户\",
           rolcreaterole::text AS \"创建角色\",
           rolcreatedb::text AS \"创建数据库\",
           rolcanlogin::text AS \"可登录\",
           rolreplication::text AS \"复制权限\",
           rolconnlimit AS \"连接限制\",
           to_char(rolvaliduntil, 'YYYY-MM-DD') AS \"密码过期\"
    FROM pg_roles
    WHERE rolname NOT LIKE 'pg_%'
    ORDER BY rolsuper DESC, rolname;
  " "无法获取用户角色"

  subsection "11.2 超级用户列表"
  safe_sql_html "
    SELECT rolname AS \"超级用户\",
           rolcanlogin::text AS \"可登录\",
           rolconnlimit AS \"连接限制\"
    FROM pg_roles
    WHERE rolsuper = true
    ORDER BY rolname;
  " "无法获取超级用户"

  subsection "11.3 已安装扩展"
  safe_sql_html "
    SELECT extname AS \"扩展名\",
           extversion AS \"版本\",
           n.nspname AS \"模式\"
    FROM pg_extension e
    JOIN pg_namespace n ON e.extnamespace = n.oid
    ORDER BY extname;
  " "无法获取扩展列表"

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 12: 关键参数
# ============================================================================
next_section "关键参数"
{
  section_start "settings" "12. 关键参数"

  safe_sql_html "
    SELECT name AS \"参数\",
           setting AS \"当前值\",
           unit AS \"单位\",
           source AS \"来源\",
           short_desc AS \"说明\"
    FROM pg_settings
    WHERE name IN (
      'max_connections', 'shared_buffers', 'effective_cache_size',
      'work_mem', 'maintenance_work_mem', 'wal_level',
      'max_wal_size', 'min_wal_size', 'checkpoint_completion_target',
      'archive_mode', 'archive_command', 'max_wal_senders',
      'hot_standby', 'log_statement', 'log_min_duration_statement',
      'log_destination', 'autovacuum', 'autovacuum_max_workers',
      'random_page_cost', 'effective_io_concurrency',
      'huge_pages', 'listen_addresses', 'max_worker_processes',
      'max_parallel_workers_per_gather', 'max_parallel_workers',
      'shared_preload_libraries', 'password_encryption',
      'ssl', 'track_activities', 'track_counts'
    )
    ORDER BY name;
  " "无法获取参数配置"

  section_end
} >> "$OUTPUT_FILE"

# ============================================================================
# Section 13: 系统信息
# ============================================================================
next_section "系统信息"
if [ "$COLLECT_SYSTEM" = "yes" ]; then
{
  section_start "system_info" "13. 系统信息"

  # --- 主机信息 ---
  subsection "13.1 主机信息"
  SYS_HOSTNAME=$(hostname 2>/dev/null || echo "N/A")
  SYS_PLATFORM=$(uname -a 2>/dev/null || echo "N/A")
  SYS_UPTIME=$(uptime 2>/dev/null || echo "N/A")

  kv_table_start
  kv_table_row "主机名" "$SYS_HOSTNAME"
  kv_table_row "平台" "$SYS_PLATFORM"
  kv_table_row "系统运行时间" "$SYS_UPTIME"
  kv_table_end

  # --- CPU ---
  subsection "13.2 CPU"
  CPU_LOGICAL=$(nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo "N/A")
  CPU_PHYSICAL=$(grep 'physical id' /proc/cpuinfo 2>/dev/null | sort -u | wc -l || echo "N/A")
  CPU_MODEL=$(grep 'model name' /proc/cpuinfo 2>/dev/null | head -1 | sed 's/.*: //' || echo "N/A")

  # CPU 使用率 (1s 采样)
  if command -v mpstat >/dev/null 2>&1; then
    CPU_USAGE=$(mpstat 1 1 2>/dev/null | awk '/Average|平均/ {printf "%.1f", 100 - $NF}')
  elif [ -f /proc/stat ]; then
    read cpu user nice system idle rest < /proc/stat
    sleep 1
    read cpu user2 nice2 system2 idle2 rest2 < /proc/stat
    TOTAL1=$((user + nice + system + idle))
    TOTAL2=$((user2 + nice2 + system2 + idle2))
    DIFF_IDLE=$((idle2 - idle))
    DIFF_TOTAL=$((TOTAL2 - TOTAL1))
    if [ "$DIFF_TOTAL" -gt 0 ] 2>/dev/null; then
      CPU_USAGE=$(awk "BEGIN {printf \"%.1f\", 100 * (1 - $DIFF_IDLE / $DIFF_TOTAL)}")
    else
      CPU_USAGE="N/A"
    fi
  else
    CPU_USAGE="N/A"
  fi

  kv_table_start
  kv_table_row "逻辑 CPU" "$CPU_LOGICAL"
  kv_table_row "物理 CPU" "$CPU_PHYSICAL"
  kv_table_row "CPU 型号" "$CPU_MODEL"
  kv_table_row "CPU 使用率" "${CPU_USAGE}%"
  kv_table_end

  # --- 内存 ---
  subsection "13.3 内存"
  if command -v free >/dev/null 2>&1; then
    MEM_TOTAL=$(free -b 2>/dev/null | awk '/Mem:/ {printf "%.2f", $2/1024/1024/1024}')
    MEM_USED=$(free -b 2>/dev/null | awk '/Mem:/ {printf "%.2f", $3/1024/1024/1024}')
    MEM_AVAILABLE=$(free -b 2>/dev/null | awk '/Mem:/ {printf "%.2f", $7/1024/1024/1024}')
    MEM_PCT=$(free -b 2>/dev/null | awk '/Mem:/ {printf "%.1f", $3/$2*100}')
    SWAP_TOTAL=$(free -b 2>/dev/null | awk '/Swap:/ {printf "%.2f", $2/1024/1024/1024}')
    SWAP_USED=$(free -b 2>/dev/null | awk '/Swap:/ {printf "%.2f", $3/1024/1024/1024}')
    SWAP_PCT=$(free -b 2>/dev/null | awk '/Swap:/ {if($2>0) printf "%.1f", $3/$2*100; else printf "0.0"}')
  else
    MEM_TOTAL="N/A"; MEM_USED="N/A"; MEM_AVAILABLE="N/A"; MEM_PCT="N/A"
    SWAP_TOTAL="N/A"; SWAP_USED="N/A"; SWAP_PCT="N/A"
  fi

  kv_table_start
  kv_table_row "总内存" "${MEM_TOTAL} GB"
  kv_table_row "已使用" "${MEM_USED} GB"
  kv_table_row "可用" "${MEM_AVAILABLE} GB"
  kv_table_row "使用率" "${MEM_PCT}%"
  kv_table_row "Swap 总计" "${SWAP_TOTAL} GB"
  kv_table_row "Swap 已用" "${SWAP_USED} GB"
  kv_table_row "Swap 使用率" "${SWAP_PCT}%"
  kv_table_end

  # --- 磁盘 ---
  subsection "13.4 磁盘"
  echo "<table><tr><th>设备</th><th>挂载点</th><th>总计</th><th>已用</th><th>可用</th><th>使用率</th></tr>"
  df -h 2>/dev/null | grep -vE '^(Filesystem|tmpfs|devtmpfs|overlay|udev|none)' | while IFS= read -r line; do
    DEVICE=$(echo "$line" | awk '{print $1}')
    MOUNT=$(echo "$line" | awk '{print $6}')
    TOTAL=$(echo "$line" | awk '{print $2}')
    USED=$(echo "$line" | awk '{print $3}')
    FREE=$(echo "$line" | awk '{print $4}')
    PCT=$(echo "$line" | awk '{print $5}' | tr -d '%')
    if [ "$PCT" -ge 90 ] 2>/dev/null; then
      CLASS="critical"
    elif [ "$PCT" -ge 80 ] 2>/dev/null; then
      CLASS="warn"
    else
      CLASS="ok"
    fi
    echo "<tr><td>${DEVICE}</td><td>${MOUNT}</td><td>${TOTAL}</td><td>${USED}</td><td>${FREE}</td><td class=\"${CLASS}\">${PCT}%</td></tr>"
  done
  echo "</table>"

  # --- Load Average ---
  subsection "13.5 Load Average"
  if [ -f /proc/loadavg ]; then
    LOAD_AVG=$(awk '{print $1, $2, $3}' /proc/loadavg 2>/dev/null || echo "N/A N/A N/A")
    LOAD_1=$(echo "$LOAD_AVG" | awk '{print $1}')
    LOAD_5=$(echo "$LOAD_AVG" | awk '{print $2}')
    LOAD_15=$(echo "$LOAD_AVG" | awk '{print $3}')
    kv_table_start
    kv_table_row "1 分钟" "$LOAD_1"
    kv_table_row "5 分钟" "$LOAD_5"
    kv_table_row "15 分钟" "$LOAD_15"
    kv_table_row "逻辑 CPU" "$CPU_LOGICAL"
    kv_table_end
  else
    echo "<p>/proc/loadavg 不可用</p>"
  fi

  # --- 磁盘 IO 统计 (iostat) ---
  subsection "13.6 磁盘 IO 统计"
  if command -v iostat >/dev/null 2>&1; then
    IOSTAT_RAW=$(iostat -xd 1 2 2>/dev/null || echo "")
    if [ -n "$IOSTAT_RAW" ]; then
      SECOND_BLOCK=$(echo "$IOSTAT_RAW" | awk 'BEGIN{n=0} /^Device/{n++} n==2{print}')
      if [ -n "$SECOND_BLOCK" ]; then
        echo "<table><tr><th>设备</th><th>r/s</th><th>w/s</th><th>rMB/s</th><th>wMB/s</th><th>await(ms)</th><th>%util</th></tr>"
        echo "$SECOND_BLOCK" | grep -v "^Device" | grep -v "^$" | while IFS= read -r line; do
          dev=$(echo "$line" | awk '{print $1}')
          [ -z "$dev" ] && continue
          rs=$(echo "$line" | awk '{print $4}')
          ws=$(echo "$line" | awk '{print $5}')
          rmb=$(echo "$line" | awk '{printf "%.2f", $6/1024}' 2>/dev/null || echo "$line" | awk '{print $6}')
          wmb=$(echo "$line" | awk '{printf "%.2f", $7/1024}' 2>/dev/null || echo "$line" | awk '{print $7}')
          await=$(echo "$line" | awk '{print $10}')
          util=$(echo "$line" | awk '{print $NF}')
          util_num=$(echo "$util" | cut -d. -f1)
          if [ "$util_num" -ge 80 ] 2>/dev/null; then
            echo "<tr><td>${dev}</td><td>${rs}</td><td>${ws}</td><td>${rmb}</td><td>${wmb}</td><td>${await}</td><td class=\"critical\">${util}%</td></tr>"
          else
            echo "<tr><td>${dev}</td><td>${rs}</td><td>${ws}</td><td>${rmb}</td><td>${wmb}</td><td>${await}</td><td>${util}%</td></tr>"
          fi
        done
        echo "</table>"
      else
        echo "<p>iostat 输出解析失败</p>"
      fi
    else
      echo "<p>iostat 执行失败</p>"
    fi
  else
    echo "<p>iostat 未安装（需要 sysstat 包）</p>"
  fi

  # --- PostgreSQL 进程资源 ---
  subsection "13.7 PostgreSQL 进程资源"
  PG_PID=""
  # 方法 1: 通过 SQL 获取 postmaster PID
  if [ -z "$PG_PID" ]; then
    PG_PID=$(run_sql "SELECT pg_backend_pid();" 2>/dev/null | head -1 | tr -d ' ')
    if [ -n "$PG_PID" ] && [ -f "/proc/$PG_PID/status" ]; then
      # 获取父进程 (postmaster)
      PPID_VAL=$(grep PPid /proc/$PG_PID/status 2>/dev/null | awk '{print $2}')
      if [ -n "$PPID_VAL" ] && [ -f "/proc/$PPID_VAL/status" ]; then
        PG_PID="$PPID_VAL"
      fi
    else
      PG_PID=""
    fi
  fi
  # 方法 2: pgrep
  if [ -z "$PG_PID" ]; then
    PG_PID=$(pgrep -f 'postgres.*-D' 2>/dev/null | head -1)
  fi
  # 方法 3: postmaster.pid
  if [ -z "$PG_PID" ] && [ -n "$PG_DATADIR" ] && [ -f "$PG_DATADIR/postmaster.pid" ]; then
    PG_PID=$(head -1 "$PG_DATADIR/postmaster.pid" 2>/dev/null)
  fi

  if [ -n "$PG_PID" ] && [ -f "/proc/$PG_PID/status" ]; then
    VmRSS=$(grep VmRSS /proc/$PG_PID/status 2>/dev/null | awk '{printf "%.2f", $2/1024/1024}' || echo "N/A")
    VmSize=$(grep VmSize /proc/$PG_PID/status 2>/dev/null | awk '{printf "%.2f", $2/1024/1024}' || echo "N/A")
    ProcThreads=$(grep Threads /proc/$PG_PID/status 2>/dev/null | awk '{print $2}' || echo "N/A")

    kv_table_start
    kv_table_row "主进程 PID" "$PG_PID"
    kv_table_row "RSS 内存" "${VmRSS} GB"
    kv_table_row "虚拟内存" "${VmSize} GB"
    kv_table_row "线程数" "$ProcThreads"
    kv_table_end
  else
    echo "<p>未找到 PostgreSQL 主进程（可能运行在容器或远程）</p>"
  fi

  # --- 虚拟内存统计 (vmstat) ---
  subsection "13.8 虚拟内存统计"
  if [ -f /proc/vmstat ]; then
    PGFAULT=$(grep "^pgfault " /proc/vmstat 2>/dev/null | awk '{print $2}' || echo "N/A")
    PGMAJFAULT=$(grep "^pgmajfault " /proc/vmstat 2>/dev/null | awk '{print $2}' || echo "N/A")
    PSWPIN=$(grep "^pswpin " /proc/vmstat 2>/dev/null | awk '{print $2}' || echo "N/A")
    PSWPOUT=$(grep "^pswpout " /proc/vmstat 2>/dev/null | awk '{print $2}' || echo "N/A")

    kv_table_start
    kv_table_row "pgfault" "$PGFAULT"
    kv_table_row "pgmajfault" "$PGMAJFAULT"
    kv_table_row "pswpin" "$PSWPIN"
    kv_table_row "pswpout" "$PSWPOUT"
    kv_table_end
  else
    echo "<p>/proc/vmstat 不可用</p>"
  fi

  # --- 网络接口统计 ---
  subsection "13.9 网络接口统计"
  if [ -f /proc/net/dev ]; then
    echo "<table><tr><th>接口</th><th>RX (GB)</th><th>RX 包</th><th>RX 错误</th><th>TX (GB)</th><th>TX 包</th><th>TX 错误</th></tr>"
    grep -E "^\s*(eth|ens|bond|em|eno|enp)" /proc/net/dev 2>/dev/null | while IFS= read -r line; do
      iface=$(echo "$line" | awk -F: '{print $1}' | tr -d ' ')
      stats=$(echo "$line" | awk -F: '{print $2}')
      rx_bytes=$(echo "$stats" | awk '{printf "%.2f", $1/1024/1024/1024}')
      rx_packets=$(echo "$stats" | awk '{print $2}')
      rx_errors=$(echo "$stats" | awk '{print $3}')
      tx_bytes=$(echo "$stats" | awk '{printf "%.2f", $9/1024/1024/1024}')
      tx_packets=$(echo "$stats" | awk '{print $10}')
      tx_errors=$(echo "$stats" | awk '{print $11}')
      echo "<tr><td>${iface}</td><td>${rx_bytes}</td><td>${rx_packets}</td><td>${rx_errors}</td><td>${tx_bytes}</td><td>${tx_packets}</td><td>${tx_errors}</td></tr>"
    done
    echo "</table>"
  else
    echo "<p>/proc/net/dev 不可用</p>"
  fi

  # --- TCP 连接状态 ---
  subsection "13.10 TCP 连接状态"
  if command -v ss >/dev/null 2>&1; then
    ESTABLISHED=$(ss -tan state established 2>/dev/null | tail -n +2 | wc -l || echo "0")
    TIME_WAIT=$(ss -tan state time-wait 2>/dev/null | tail -n +2 | wc -l || echo "0")
    CLOSE_WAIT=$(ss -tan state close-wait 2>/dev/null | tail -n +2 | wc -l || echo "0")
    LISTEN=$(ss -tln 2>/dev/null | tail -n +2 | wc -l || echo "0")
    SYN_RECV=$(ss -tan state syn-recv 2>/dev/null | tail -n +2 | wc -l || echo "0")
    TOTAL_TCP=$(ss -tan 2>/dev/null | tail -n +2 | wc -l || echo "0")

    kv_table_start
    kv_table_row "总计" "$TOTAL_TCP"
    kv_table_row "ESTABLISHED" "$ESTABLISHED"
    kv_table_row "TIME_WAIT" "$TIME_WAIT"
    kv_table_row "CLOSE_WAIT" "$CLOSE_WAIT"
    kv_table_row "LISTEN" "$LISTEN"
    kv_table_row "SYN_RECV" "$SYN_RECV"
    kv_table_end
  else
    echo "<p>ss 命令不可用</p>"
  fi

  section_end
} >> "$OUTPUT_FILE"
else
{
  section_start "system_info" "13. 系统信息"
  echo "<p>系统信息采集已跳过（--no-system）</p>"
  section_end
} >> "$OUTPUT_FILE"
fi

# ============================================================================
# HTML 尾部
# ============================================================================
cat >> "$OUTPUT_FILE" <<'HTMLFOOT'
</body>
</html>
HTMLFOOT

printf "\n${GREEN}=== 采集完成 ===${NC}\n" >&2
printf "  输出文件: ${OUTPUT_FILE}\n" >&2
printf "  请上传至 ora100.com/oracheck-report 生成巡检报告\n\n" >&2
