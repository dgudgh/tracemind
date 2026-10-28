#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# ============================================================================
# MySQL 数据库巡检采集脚本 v3.5 (Production-Grade)
# 用于 ora100.com 巡检报告平台
#
# v3.5 变更 (MySQLTuner 专家级指标对标):
#   - 新增 Section 19: 专家级调优指标 (Handler 读取模式统计)
#   - Section 3 新增: table_open_cache, table_definition_cache
#   - Section 5 新增: innodb_max_dirty_pages_pct, innodb_io_capacity_max,
#     innodb_adaptive_hash_index, innodb_change_buffering, innodb_page_size,
#     innodb_log_files_in_group
#   - Section 6.1 新增: Com_commit, Com_rollback, Sort_merge_passes,
#     Sort_scan, Sort_range, Sort_rows, Select_full_join,
#     Select_full_range_join, Select_scan, Select_range, Select_range_check
#   - Section 6.2 新增: Innodb_log_waits, Innodb_log_writes,
#     Innodb_buffer_pool_wait_free
#   - Section 6.3 新增: Opened_tables, Open_tables, Opened_files,
#     Table_open_cache_hits/misses/overflows, Open_table_definitions,
#     Opened_table_definitions, Binlog_cache_disk_use, Binlog_cache_use,
#     Key_reads/read_requests/writes/write_requests
#   - SECTION_TOTAL 从 19 扩展到 20
#
# v3.4 变更 (Phase 6 — 性能诊断能力升级):
#   - 两次采样计算 QPS/TPS/IOPS 实时指标 (--sample-interval, 默认 5s)
#   - iostat 磁盘 IO 采集 (需 sysstat 包)
#   - Load Average 1/5/15 分钟
#   - mysqld 进程 RSS/VSZ/Swap 内存
#   - InnoDB 脏页/空闲页/预读淘汰指标
#   - Per-Table IO 热点 Top 10 (performance_schema)
#   - vmstat 页面错误 & Swap 活跃度
#   - 网络接口统计 (/proc/net/dev)
#   - TCP 连接状态分布 (ss)
#   - SECTION_TOTAL 从 18 扩展到 19
#
# v3.3 变更:
#   - 内存超分检测: 新增 thread_stack、key_buffer_size、binlog_cache_size 采集
#   - IO 指标优化: 新增 Innodb_data_reads、Innodb_data_writes、Innodb_pages_read 等采集
#   - 大表分区检测: Top 20 大表新增分区状态列
#   - NUMA 检测: 新增 NUMA 拓扑 + innodb_numa_interleave 采集
#
# v3.2 变更:
#   - 修复版本号被 MySQL warning 污染 (run_sql 替代 run_sql_verbose 2>&1)
#   - 修复进度条被重定向到 HTML 文件 (next_section 输出到 stderr)
#   - 备份检查 section 17 MariaDB 兼容: 改用 SHOW VARIABLES/STATUS 替代
#     performance_schema.global_variables 子查询
#   - 去重: section 17.1/17.3 与 section 4/5 的重复变量, 仅保留备份专属参数
#
# v3.1 变更:
#   - 新增复制深度诊断 (section 16): 主库 Binlog 状态、已连接从库、
#     半同步复制、Group Replication / InnoDB Cluster、复制过滤器、
#     GTID 执行/清除分析、多源复制通道
#   - 系统资源从 section 16 调整为 section 17
#
# v3.0 变更:
#   - 连接/查询超时保护（--connect-timeout + 单查询 timeout）
#   - 支持 --login-path（MySQL 原生凭据管理）
#   - MySQL 8.0+ 锁诊断改用 data_lock_waits/data_locks
#   - 进程列表改用 performance_schema.processlist（8.0+，fallback SHOW PROCESSLIST）
#   - 新增 SSL/TLS 状态采集
#   - 新增 auto_increment 溢出预警采集
#   - 新增密码策略检查（validate_password）
#   - 新增长查询采集（>300s 的活跃查询）
#   - 新增复制延迟数值采集
#   - 错误处理：关键查询失败时记录而非静默忽略
#   - 所有查询添加只读保护注释
#
# 使用方法:
#   chmod +x mysql_check.sh
#   bash mysql_check.sh -H localhost -P 3306 -u root -p
#   bash mysql_check.sh --login-path=prod           # 使用 mysql_config_editor 配置
#
# 依赖: mysql 客户端 (mysql CLI) 5.7+
# 输出: mysql_check_<实例名>_<时间戳>.html
# 上传到 ora100.com 即可生成专业 Word 巡检报告
# ============================================================================

set -uo pipefail
# NOTE: 不使用 set -e，改为手动检查关键命令的返回值，避免非致命查询失败导致整个脚本退出

SCRIPT_VERSION="3.5"

# ── 常量 ─────────────────────────────────────────────────
CONNECT_TIMEOUT=10
SQL_TIMEOUT=30
TOP_TABLES_LIMIT=20
PROCESSLIST_LIMIT=50
LONG_QUERY_SECONDS=300
SAMPLE_INTERVAL=5

# ── 颜色输出 ─────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ── 参数解析 ─────────────────────────────────────────────────
MYSQL_HOST="localhost"
MYSQL_PORT=3306
MYSQL_USER="root"
MYSQL_PASS=""
MYSQL_SOCKET=""
INSTANCE_NAME="MySQL_Server"
OUTPUT_FILE=""
COLLECT_SYSTEM="yes"
LOGIN_PATH=""

usage() {
  cat <<EOF
MySQL 数据库巡检采集脚本 v${SCRIPT_VERSION} (Production-Grade)

用法: bash $0 [选项]

选项:
  -H, --host HOST        MySQL 主机地址 (默认: localhost)
  -P, --port PORT        MySQL 端口 (默认: 3306)
  -S, --socket SOCK      MySQL socket 文件路径 (如: /var/lib/mysql/mysql.sock)
  -u, --user USER        MySQL 用户名 (默认: root)
  -p, --password PASS    MySQL 密码 (交互式输入)
  --login-path PATH      使用 mysql_config_editor 配置的登录路径 (优先于 -u/-p/-H/-P)
  -n, --name NAME        实例名称 (默认: MySQL_Server)
  -o, --output FILE      输出文件路径 (默认: 自动生成)
  --no-system            跳过系统信息采集
  --timeout SEC          单条 SQL 超时秒数 (默认: ${SQL_TIMEOUT})
  --sample-interval SEC  两次采样间隔秒数 (默认: ${SAMPLE_INTERVAL}，范围 3-30)
  -h, --help             显示帮助信息

示例:
  bash $0 -H 192.168.1.100 -u root -p
  bash $0 -S /var/lib/mysql/mysql.sock -u root -p
  bash $0 --login-path=prod --name "生产库"
  bash $0 -H db.example.com -P 3307 -u dba -p --name "生产库" --timeout 60
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case $1 in
    -H|--host)     MYSQL_HOST="$2"; shift 2 ;;
    -P|--port)     MYSQL_PORT="$2"; shift 2 ;;
    -S|--socket)   MYSQL_SOCKET="$2"; shift 2 ;;
    -u|--user)     MYSQL_USER="$2"; shift 2 ;;
    -p|--password)
      if [[ $# -gt 1 && ! "$2" =~ ^- ]]; then
        MYSQL_PASS="$2"; shift 2
      else
        read -rsp "  MySQL 密码: " MYSQL_PASS; echo; shift
      fi
      ;;
    --login-path|--login-path=*)
      if [[ "$1" == *=* ]]; then
        LOGIN_PATH="${1#*=}"; shift
      else
        LOGIN_PATH="$2"; shift 2
      fi
      ;;
    -n|--name)     INSTANCE_NAME="$2"; shift 2 ;;
    -o|--output)   OUTPUT_FILE="$2"; shift 2 ;;
    --no-system)   COLLECT_SYSTEM="no"; shift ;;
    --timeout)     SQL_TIMEOUT="$2"; shift 2 ;;
    --sample-interval)
      SAMPLE_INTERVAL="$2"
      if [[ "$SAMPLE_INTERVAL" -lt 3 ]] 2>/dev/null; then SAMPLE_INTERVAL=3; fi
      if [[ "$SAMPLE_INTERVAL" -gt 30 ]] 2>/dev/null; then SAMPLE_INTERVAL=30; fi
      shift 2 ;;
    -h|--help)     usage ;;
    *) err "未知参数: $1"; usage ;;
  esac
done

# 如果没有提供 login-path 且没有密码，交互式输入
if [[ -z "$LOGIN_PATH" && -z "$MYSQL_PASS" ]]; then
  read -rsp "  MySQL 密码: " MYSQL_PASS; echo
fi

# 输出文件名
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
COLLECT_TIME=$(date +"%Y-%m-%dT%H:%M:%S")
if [[ -z "$OUTPUT_FILE" ]]; then
  OUTPUT_FILE="mysql_check_${INSTANCE_NAME}_${TIMESTAMP}.html"
fi

echo "=================================================="
echo "  MySQL 数据库巡检采集工具 v${SCRIPT_VERSION}"
echo "  生成结果上传到 ora100.com 即可获取专业报告"
echo "=================================================="

# ── MySQL 执行函数 ────────────────────────────────────────────
# Build base mysql command args
MYSQL_BASE_ARGS=()
if [[ -n "$LOGIN_PATH" ]]; then
  MYSQL_BASE_ARGS+=(--login-path="$LOGIN_PATH")
else
  # Use MYSQL_PWD env var to avoid password exposure in ps output
  export MYSQL_PWD="$MYSQL_PASS"
  if [[ -n "$MYSQL_SOCKET" ]]; then
    MYSQL_BASE_ARGS+=(-S"$MYSQL_SOCKET" -u"$MYSQL_USER")
  else
    MYSQL_BASE_ARGS+=(-h"$MYSQL_HOST" -P"$MYSQL_PORT" -u"$MYSQL_USER")
  fi
fi
MYSQL_BASE_ARGS+=(--connect-timeout="$CONNECT_TIMEOUT" --default-character-set=utf8mb4)

# Cleanup sensitive env on exit; ensure HTML file is properly closed on interruption
cleanup() {
  unset MYSQL_PWD 2>/dev/null || true
}

# Graceful interruption handler: close HTML tags so file is still parseable
interrupt_handler() {
  # If OUTPUT_FILE is set and exists but is incomplete, close it gracefully
  if [[ -n "${OUTPUT_FILE:-}" && -f "$OUTPUT_FILE" ]]; then
    # Check if file lacks closing </body></html>
    if ! grep -q "</html>" "$OUTPUT_FILE" 2>/dev/null; then
      {
        echo "</div>"  # close any open section
        echo "<div class=\"footer\">"
        echo "  <p class=\"warn\">⚠ 采集被中断，数据可能不完整</p>"
        echo "  <p>本报告由 mysql_check.sh v${SCRIPT_VERSION:-3.5} 生成 | 采集时间: ${COLLECT_TIME:-unknown}</p>"
        echo "</div>"
        echo "</body></html>"
      } >> "$OUTPUT_FILE"
    fi
  fi
  cleanup
  exit 1
}
trap interrupt_handler INT TERM HUP
trap cleanup EXIT

# 执行 SQL 返回纯文本（带超时保护）
run_sql() {
  local sql="$1"
  timeout "$SQL_TIMEOUT" mysql "${MYSQL_BASE_ARGS[@]}" -N -s -e "$sql" 2>/dev/null
}

# 执行 SQL 返回纯文本（错误信息保留，用于调试）
run_sql_verbose() {
  local sql="$1"
  timeout "$SQL_TIMEOUT" mysql "${MYSQL_BASE_ARGS[@]}" -N -s -e "$sql"
}

# 执行 SQL 返回 HTML 表格（带超时保护）
run_sql_html() {
  local sql="$1"
  timeout "$SQL_TIMEOUT" mysql "${MYSQL_BASE_ARGS[@]}" --html -e "$sql" 2>/dev/null
}

# 安全执行：失败时输出占位信息而非静默忽略
safe_sql_html() {
  local sql="$1"
  local fallback_msg="${2:-无法获取数据}"
  local result
  result=$(run_sql_html "$sql" 2>/dev/null) || true
  if [[ -n "$result" && "$result" == *"<T"* ]]; then
    echo "$result"
  else
    echo "<p>${fallback_msg}</p>"
  fi
}

# ── 测试连接 ─────────────────────────────────────────────────
info "测试 MySQL 连接..."
MYSQL_VERSION=$(run_sql "SELECT VERSION();") || {
  CONN_ERR=$(run_sql_verbose "SELECT VERSION();" 2>&1)
  err "MySQL 连接失败，请检查连接信息"
  err "详细: ${CONN_ERR}"
  exit 1
}
ok "连接成功! MySQL ${MYSQL_VERSION}"

# 判断 MySQL 主版本号
MYSQL_MAJOR=$(echo "$MYSQL_VERSION" | grep -oP '^\d+' || echo "5")
MYSQL_MINOR=$(echo "$MYSQL_VERSION" | grep -oP '^\d+\.\d+' | grep -oP '\d+$' || echo "7")
IS_MYSQL8=false
if [[ "$MYSQL_MAJOR" -ge 8 ]]; then
  IS_MYSQL8=true
fi

# 如果使用 login-path，需要从数据库获取实际连接信息
if [[ -n "$LOGIN_PATH" ]]; then
  MYSQL_HOST=$(run_sql "SELECT @@hostname" 2>/dev/null || echo "$LOGIN_PATH")
  MYSQL_PORT=$(run_sql "SELECT @@port" 2>/dev/null || echo "3306")
fi

# ── 开始采集 ─────────────────────────────────────────────────
info "开始采集数据..."

# ── HTML 构建 ─────────────────────────────────────────────────
# 生成 HTML 开头
html_header() {
  cat <<'HTMLEOF'
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="generator" content="mysql_check.sh">
HTMLEOF
  echo "<meta name=\"version\" content=\"${SCRIPT_VERSION}\">"
  echo "<meta name=\"db-type\" content=\"mysql\">"
  echo "<meta name=\"collect-time\" content=\"${COLLECT_TIME}\">"
  echo "<meta name=\"instance-name\" content=\"${INSTANCE_NAME}\">"
  echo "<meta name=\"instance-host\" content=\"${MYSQL_HOST}\">"
  echo "<meta name=\"instance-port\" content=\"${MYSQL_PORT}\">"
  echo "<meta name=\"mysql-version\" content=\"${MYSQL_VERSION}\">"
  cat <<'HTMLEOF'
<title>MySQL 数据库巡检报告</title>
<style>
  body { font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif; margin: 20px; background: #f5f5f5; color: #333; }
  h1 { color: #1565C0; border-bottom: 2px solid #1565C0; padding-bottom: 8px; }
  h2 { color: #1976D2; margin-top: 30px; border-left: 4px solid #1976D2; padding-left: 10px; }
  table { border-collapse: collapse; width: 100%; margin: 10px 0 20px 0; background: #fff; }
  th { background: #1565C0; color: #fff; padding: 8px 12px; text-align: left; font-size: 14px; }
  td { border: 1px solid #ddd; padding: 6px 12px; font-size: 13px; }
  tr:nth-child(even) { background: #f9f9f9; }
  .section { margin: 20px 0; padding: 15px; background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .meta-info { background: #e3f2fd; padding: 15px; border-radius: 8px; margin-bottom: 20px; }
  .meta-info td { border: 1px solid #bbdefb; }
  .warn { color: #e65100; font-weight: bold; }
  .critical { color: #c62828; font-weight: bold; }
  .ok { color: #2e7d32; }
  .footer { text-align: center; color: #999; font-size: 12px; margin-top: 40px; padding: 20px; border-top: 1px solid #eee; }
</style>
</head>
<body>
<h1>MySQL 数据库巡检报告</h1>
HTMLEOF
}

# 写一个带 ID 的 section 开始
section_start() {
  local id="$1"
  local title="$2"
  echo "<div class=\"section\" id=\"${id}\">"
  echo "<h2>${title}</h2>"
}

section_end() {
  echo "</div>"
}

# 手动构建一个 key-value 表格（不依赖 mysql --html）
kv_table_start() {
  echo "<table><tr><th>项目</th><th>值</th></tr>"
}

kv_table_row() {
  echo "<tr><td>$1</td><td>$2</td></tr>"
}

kv_table_end() {
  echo "</table>"
}

# ── 开始写入 HTML ─────────────────────────────────────────────
SECTION_TOTAL=20
SECTION_NUM=0

next_section() {
  SECTION_NUM=$((SECTION_NUM + 1))
  info "  [${SECTION_NUM}/${SECTION_TOTAL}] $1 ✓" >&2
}

{
  html_header

  # ================================================================
  # 1. 基础信息
  # ================================================================
  section_start "basic_info" "1. 基础信息"

  INSTANCE_UPTIME=$(run_sql "
    SELECT DATE_FORMAT(
      DATE_SUB(NOW(), INTERVAL variable_value SECOND),
      '%Y-%m-%d %H:%i:%s'
    ) FROM performance_schema.global_status
    WHERE variable_name = 'Uptime'" || echo "N/A")

  PLATFORM_OS=$(run_sql "
    SELECT variable_value FROM performance_schema.global_variables
    WHERE variable_name = 'version_compile_os'" || echo "N/A")

  PLATFORM_MACHINE=$(run_sql "
    SELECT variable_value FROM performance_schema.global_variables
    WHERE variable_name = 'version_compile_machine'" || echo "N/A")

  DB_HOSTNAME=$(run_sql "
    SELECT variable_value FROM performance_schema.global_variables
    WHERE variable_name = 'hostname'" || echo "N/A")

  CHARSET_DB=$(run_sql "SHOW VARIABLES LIKE 'character_set_database'" | awk '{print $2}' || echo "N/A")
  CHARSET_SERVER=$(run_sql "SHOW VARIABLES LIKE 'character_set_server'" | awk '{print $2}' || echo "N/A")
  COLLATION=$(run_sql "SHOW VARIABLES LIKE 'collation_server'" | awk '{print $2}' || echo "N/A")
  BASEDIR=$(run_sql "SHOW VARIABLES LIKE 'basedir'" | awk '{print $2}' || echo "N/A")
  DATADIR=$(run_sql "SHOW VARIABLES LIKE 'datadir'" | awk '{print $2}' || echo "N/A")

  kv_table_start
  kv_table_row "实例名称" "$INSTANCE_NAME"
  kv_table_row "服务器地址" "${MYSQL_HOST}:${MYSQL_PORT}"
  kv_table_row "MySQL 版本" "$MYSQL_VERSION"
  kv_table_row "服务器主机名" "$DB_HOSTNAME"
  kv_table_row "实例启动时间" "$INSTANCE_UPTIME"
  kv_table_row "编译平台" "${PLATFORM_OS} (${PLATFORM_MACHINE})"
  kv_table_row "数据库字符集" "$CHARSET_DB"
  kv_table_row "服务器字符集" "$CHARSET_SERVER"
  kv_table_row "排序规则" "$COLLATION"
  kv_table_row "安装目录" "$BASEDIR"
  kv_table_row "数据目录" "$DATADIR"
  kv_table_row "采集时间" "$COLLECT_TIME"
  kv_table_end

  section_end
  next_section "基础信息"

  # ================================================================
  # 2. 连接配置
  # ================================================================
  section_start "connection_config" "2. 连接配置"
  safe_sql_html "
    SELECT
      v.Variable_name AS '参数',
      v.Variable_value AS '值'
    FROM (
      SELECT 'max_connections' AS Variable_name,
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='max_connections') AS Variable_value
      UNION ALL SELECT 'Threads_connected',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Threads_connected')
      UNION ALL SELECT 'Threads_running',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Threads_running')
      UNION ALL SELECT 'Max_used_connections',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Max_used_connections')
      UNION ALL SELECT 'interactive_timeout',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='interactive_timeout')
      UNION ALL SELECT 'wait_timeout',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='wait_timeout')
      UNION ALL SELECT 'back_log',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='back_log')
      UNION ALL SELECT 'max_allowed_packet',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='max_allowed_packet')
      UNION ALL SELECT 'skip_name_resolve',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='skip_name_resolve')
      UNION ALL SELECT 'open_files_limit',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='open_files_limit')
    ) v;
  "
  section_end
  next_section "连接配置"

  # ================================================================
  # 3. 内存配置
  # ================================================================
  section_start "memory_config" "3. 内存配置"
  safe_sql_html "
    SELECT
      v.Variable_name AS '参数',
      v.Variable_value AS '值'
    FROM (
      SELECT 'innodb_buffer_pool_size' AS Variable_name,
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_buffer_pool_size') AS Variable_value
      UNION ALL SELECT 'innodb_buffer_pool_instances',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_buffer_pool_instances')
      UNION ALL SELECT 'sort_buffer_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='sort_buffer_size')
      UNION ALL SELECT 'join_buffer_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='join_buffer_size')
      UNION ALL SELECT 'thread_cache_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='thread_cache_size')
      UNION ALL SELECT 'tmp_table_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='tmp_table_size')
      UNION ALL SELECT 'max_heap_table_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='max_heap_table_size')
      UNION ALL SELECT 'read_buffer_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='read_buffer_size')
      UNION ALL SELECT 'read_rnd_buffer_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='read_rnd_buffer_size')
      UNION ALL SELECT 'innodb_io_capacity',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_io_capacity')
      UNION ALL SELECT 'thread_stack',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='thread_stack')
      UNION ALL SELECT 'key_buffer_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='key_buffer_size')
      UNION ALL SELECT 'binlog_cache_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='binlog_cache_size')
      UNION ALL SELECT 'innodb_sort_buffer_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_sort_buffer_size')
      UNION ALL SELECT 'table_open_cache',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='table_open_cache')
      UNION ALL SELECT 'table_definition_cache',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='table_definition_cache')
    ) v;
  "
  section_end
  next_section "内存配置"

  # ================================================================
  # 4. 日志配置
  # ================================================================
  section_start "log_config" "4. 日志配置"
  safe_sql_html "
    SELECT
      v.Variable_name AS '参数',
      v.Variable_value AS '值'
    FROM (
      SELECT 'slow_query_log' AS Variable_name,
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='slow_query_log') AS Variable_value
      UNION ALL SELECT 'slow_query_log_file',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='slow_query_log_file')
      UNION ALL SELECT 'long_query_time',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='long_query_time')
      UNION ALL SELECT 'log_bin',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='log_bin')
      UNION ALL SELECT 'binlog_format',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='binlog_format')
      UNION ALL SELECT 'expire_logs_days',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='expire_logs_days')
      UNION ALL SELECT 'binlog_expire_logs_seconds',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='binlog_expire_logs_seconds')
      UNION ALL SELECT 'innodb_log_file_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_log_file_size')
      UNION ALL SELECT 'innodb_log_buffer_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_log_buffer_size')
      UNION ALL SELECT 'innodb_flush_log_at_trx_commit',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_flush_log_at_trx_commit')
      UNION ALL SELECT 'sync_binlog',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='sync_binlog')
    ) v;
  "
  section_end
  next_section "日志配置"

  # ================================================================
  # 5. InnoDB 配置
  # ================================================================
  section_start "innodb_config" "5. InnoDB 配置"
  safe_sql_html "
    SELECT
      v.Variable_name AS '参数',
      v.Variable_value AS '值'
    FROM (
      SELECT 'innodb_file_per_table' AS Variable_name,
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_file_per_table') AS Variable_value
      UNION ALL SELECT 'innodb_open_files',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_open_files')
      UNION ALL SELECT 'innodb_thread_concurrency',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_thread_concurrency')
      UNION ALL SELECT 'innodb_flush_method',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_flush_method')
      UNION ALL SELECT 'innodb_doublewrite',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_doublewrite')
      UNION ALL SELECT 'innodb_read_io_threads',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_read_io_threads')
      UNION ALL SELECT 'innodb_write_io_threads',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_write_io_threads')
      UNION ALL SELECT 'innodb_checksum_algorithm',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_checksum_algorithm')
      UNION ALL SELECT 'innodb_numa_interleave',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_numa_interleave')
      UNION ALL SELECT 'innodb_max_dirty_pages_pct',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_max_dirty_pages_pct')
      UNION ALL SELECT 'innodb_io_capacity_max',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_io_capacity_max')
      UNION ALL SELECT 'innodb_adaptive_hash_index',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_adaptive_hash_index')
      UNION ALL SELECT 'innodb_change_buffering',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_change_buffering')
      UNION ALL SELECT 'innodb_page_size',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_page_size')
      UNION ALL SELECT 'innodb_log_files_in_group',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_log_files_in_group')
    ) v;
  "
  section_end
  next_section "InnoDB 配置"

  # ================================================================
  # 6. 性能指标
  # ================================================================
  section_start "performance" "6. 性能指标"

  # 6.1 查询统计
  # Note: Use IFNULL to handle cases where Com_* variables are not visible
  # in performance_schema (e.g., when performance-schema-consumer-global_status
  # is disabled or instruments are not enabled for statement/com/%)
  echo "<h3>6.1 查询统计</h3>"
  safe_sql_html "
    SELECT
      v.Variable_name AS '指标',
      v.Variable_value AS '值'
    FROM (
      SELECT 'Queries' AS Variable_name,
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Queries'), '0') AS Variable_value
      UNION ALL SELECT 'Questions',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Questions'), '0')
      UNION ALL SELECT 'Slow_queries',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Slow_queries'), '0')
      UNION ALL SELECT 'Com_select',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_select'), '0')
      UNION ALL SELECT 'Com_insert',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_insert'), '0')
      UNION ALL SELECT 'Com_update',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_update'), '0')
      UNION ALL SELECT 'Com_delete',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_delete'), '0')
      UNION ALL SELECT 'Com_commit',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_commit'), '0')
      UNION ALL SELECT 'Com_rollback',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_rollback'), '0')
      UNION ALL SELECT 'Sort_merge_passes',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Sort_merge_passes'), '0')
      UNION ALL SELECT 'Sort_scan',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Sort_scan'), '0')
      UNION ALL SELECT 'Sort_range',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Sort_range'), '0')
      UNION ALL SELECT 'Sort_rows',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Sort_rows'), '0')
      UNION ALL SELECT 'Select_full_join',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Select_full_join'), '0')
      UNION ALL SELECT 'Select_full_range_join',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Select_full_range_join'), '0')
      UNION ALL SELECT 'Select_scan',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Select_scan'), '0')
      UNION ALL SELECT 'Select_range',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Select_range'), '0')
      UNION ALL SELECT 'Select_range_check',
             IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Select_range_check'), '0')
    ) v;
  "

  # 6.2 InnoDB 性能
  echo "<h3>6.2 InnoDB 性能</h3>"
  safe_sql_html "
    SELECT
      v.Variable_name AS '指标',
      v.Variable_value AS '值'
    FROM (
      SELECT 'Innodb_buffer_pool_read_requests' AS Variable_name,
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_read_requests') AS Variable_value
      UNION ALL SELECT 'Innodb_buffer_pool_reads',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_reads')
      UNION ALL SELECT 'Innodb_row_lock_waits',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_row_lock_waits')
      UNION ALL SELECT 'Innodb_row_lock_time_avg',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_row_lock_time_avg')
      UNION ALL SELECT 'Innodb_data_reads',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_data_reads')
      UNION ALL SELECT 'Innodb_data_writes',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_data_writes')
      UNION ALL SELECT 'Innodb_pages_read',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_pages_read')
      UNION ALL SELECT 'Innodb_pages_written',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_pages_written')
      UNION ALL SELECT 'Innodb_buffer_pool_pages_dirty',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_pages_dirty')
      UNION ALL SELECT 'Innodb_buffer_pool_pages_total',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_pages_total')
      UNION ALL SELECT 'Innodb_buffer_pool_pages_free',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_pages_free')
      UNION ALL SELECT 'Innodb_buffer_pool_read_ahead',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_read_ahead')
      UNION ALL SELECT 'Innodb_buffer_pool_read_ahead_evicted',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_read_ahead_evicted')
      UNION ALL SELECT 'Innodb_log_waits',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_log_waits')
      UNION ALL SELECT 'Innodb_log_writes',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_log_writes')
      UNION ALL SELECT 'Innodb_buffer_pool_wait_free',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_wait_free')
    ) v;
  "

  # 6.3 表锁 & 临时表 & 连接 & 流量
  echo "<h3>6.3 锁 / 临时表 / 连接 / 流量</h3>"
  safe_sql_html "
    SELECT
      v.Variable_name AS '指标',
      v.Variable_value AS '值'
    FROM (
      SELECT 'Table_locks_immediate' AS Variable_name,
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Table_locks_immediate') AS Variable_value
      UNION ALL SELECT 'Table_locks_waited',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Table_locks_waited')
      UNION ALL SELECT 'Created_tmp_tables',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Created_tmp_tables')
      UNION ALL SELECT 'Created_tmp_disk_tables',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Created_tmp_disk_tables')
      UNION ALL SELECT 'Connections',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Connections')
      UNION ALL SELECT 'Aborted_clients',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Aborted_clients')
      UNION ALL SELECT 'Aborted_connects',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Aborted_connects')
      UNION ALL SELECT 'Threads_created',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Threads_created')
      UNION ALL SELECT 'Bytes_received',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Bytes_received')
      UNION ALL SELECT 'Bytes_sent',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Bytes_sent')
      UNION ALL SELECT 'Opened_tables',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Opened_tables')
      UNION ALL SELECT 'Open_tables',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Open_tables')
      UNION ALL SELECT 'Opened_files',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Opened_files')
      UNION ALL SELECT 'Table_open_cache_hits',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Table_open_cache_hits')
      UNION ALL SELECT 'Table_open_cache_misses',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Table_open_cache_misses')
      UNION ALL SELECT 'Table_open_cache_overflows',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Table_open_cache_overflows')
      UNION ALL SELECT 'Open_table_definitions',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Open_table_definitions')
      UNION ALL SELECT 'Opened_table_definitions',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Opened_table_definitions')
      UNION ALL SELECT 'Binlog_cache_disk_use',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Binlog_cache_disk_use')
      UNION ALL SELECT 'Binlog_cache_use',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Binlog_cache_use')
      UNION ALL SELECT 'Key_reads',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Key_reads')
      UNION ALL SELECT 'Key_read_requests',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Key_read_requests')
      UNION ALL SELECT 'Key_writes',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Key_writes')
      UNION ALL SELECT 'Key_write_requests',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Key_write_requests')
    ) v;
  "
  section_end
  next_section "性能指标"

  # ================================================================
  # 6.5 实时采样指标 (v3.4 — Phase 6.1)
  # 两次快照间隔 SAMPLE_INTERVAL 秒，计算 QPS/TPS/IOPS
  # ================================================================
  section_start "sampled_metrics" "6.5 实时采样指标 (${SAMPLE_INTERVAL}s 采样)"

  info "  采样中 (${SAMPLE_INTERVAL}s)..." >&2
  # ── 第一次采样 ──
  SNAP1=$(run_sql "
    SELECT
      IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Queries'), 0),
      IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_select'), 0),
      IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_insert'), 0),
      IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_update'), 0),
      IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_delete'), 0),
      IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_commit'), 0),
      IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_rollback'), 0),
      IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_data_reads'), 0),
      IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_data_writes'), 0),
      IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_read_requests'), 0),
      IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_reads'), 0);
  " 2>/dev/null || echo "")

  if [[ -n "$SNAP1" ]]; then
    read -r S1_QUERIES S1_SELECT S1_INSERT S1_UPDATE S1_DELETE S1_COMMIT S1_ROLLBACK S1_DREADS S1_DWRITES S1_BPREQ S1_BPREADS <<< "$SNAP1"
    # 将 NULL 值替换为 0，防止算术运算报错
    S1_QUERIES=${S1_QUERIES//NULL/0}; S1_SELECT=${S1_SELECT//NULL/0}; S1_INSERT=${S1_INSERT//NULL/0}
    S1_UPDATE=${S1_UPDATE//NULL/0}; S1_DELETE=${S1_DELETE//NULL/0}; S1_COMMIT=${S1_COMMIT//NULL/0}
    S1_ROLLBACK=${S1_ROLLBACK//NULL/0}; S1_DREADS=${S1_DREADS//NULL/0}; S1_DWRITES=${S1_DWRITES//NULL/0}
    S1_BPREQ=${S1_BPREQ//NULL/0}; S1_BPREADS=${S1_BPREADS//NULL/0}

    sleep "$SAMPLE_INTERVAL"

    # ── 第二次采样 ──
    SNAP2=$(run_sql "
      SELECT
        IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Queries'), 0),
        IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_select'), 0),
        IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_insert'), 0),
        IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_update'), 0),
        IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_delete'), 0),
        IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_commit'), 0),
        IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_rollback'), 0),
        IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_data_reads'), 0),
        IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_data_writes'), 0),
        IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_read_requests'), 0),
        IFNULL((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_reads'), 0);
    " 2>/dev/null || echo "")

    if [[ -n "$SNAP2" ]]; then
      read -r S2_QUERIES S2_SELECT S2_INSERT S2_UPDATE S2_DELETE S2_COMMIT S2_ROLLBACK S2_DREADS S2_DWRITES S2_BPREQ S2_BPREADS <<< "$SNAP2"
      # 将 NULL 值替换为 0，防止算术运算报错
      S2_QUERIES=${S2_QUERIES//NULL/0}; S2_SELECT=${S2_SELECT//NULL/0}; S2_INSERT=${S2_INSERT//NULL/0}
      S2_UPDATE=${S2_UPDATE//NULL/0}; S2_DELETE=${S2_DELETE//NULL/0}; S2_COMMIT=${S2_COMMIT//NULL/0}
      S2_ROLLBACK=${S2_ROLLBACK//NULL/0}; S2_DREADS=${S2_DREADS//NULL/0}; S2_DWRITES=${S2_DWRITES//NULL/0}
      S2_BPREQ=${S2_BPREQ//NULL/0}; S2_BPREADS=${S2_BPREADS//NULL/0}

      # ── 计算 delta ──
      DELTA_QPS=$(( (S2_QUERIES - S1_QUERIES) / SAMPLE_INTERVAL ))
      DELTA_SELECT=$(( (S2_SELECT - S1_SELECT) / SAMPLE_INTERVAL ))
      DELTA_WRITE=$(( (S2_INSERT + S2_UPDATE + S2_DELETE - S1_INSERT - S1_UPDATE - S1_DELETE) / SAMPLE_INTERVAL ))
      DELTA_TPS=$(( (S2_COMMIT + S2_ROLLBACK - S1_COMMIT - S1_ROLLBACK) / SAMPLE_INTERVAL ))
      DELTA_IOREAD=$(( (S2_DREADS - S1_DREADS) / SAMPLE_INTERVAL ))
      DELTA_IOWRITE=$(( (S2_DWRITES - S1_DWRITES) / SAMPLE_INTERVAL ))

      # Buffer Pool 实时命中率
      BP_REQ_DELTA=$((S2_BPREQ - S1_BPREQ))
      BP_READS_DELTA=$((S2_BPREADS - S1_BPREADS))
      if [[ $BP_REQ_DELTA -gt 0 ]]; then
        BP_HIT_RT=$(awk "BEGIN {printf \"%.2f\", (1 - ${BP_READS_DELTA}/${BP_REQ_DELTA}) * 100}")
      else
        BP_HIT_RT="N/A"
      fi

      kv_table_start
      kv_table_row "采样间隔" "${SAMPLE_INTERVAL} 秒"
      kv_table_row "QPS (采样)" "$DELTA_QPS"
      kv_table_row "TPS (采样)" "$DELTA_TPS"
      kv_table_row "读 QPS (Com_select)" "$DELTA_SELECT"
      kv_table_row "写 QPS (Insert+Update+Delete)" "$DELTA_WRITE"
      kv_table_row "InnoDB 读 IOPS" "$DELTA_IOREAD"
      kv_table_row "InnoDB 写 IOPS" "$DELTA_IOWRITE"
      kv_table_row "Buffer Pool 实时命中率" "${BP_HIT_RT}%"
      kv_table_end
    else
      echo "<p>第二次采样失败</p>"
    fi
  else
    echo "<p>采样失败（performance_schema 不可用）</p>"
  fi

  section_end
  next_section "实时采样指标"

  # ================================================================
  # 7. 数据库信息
  # ================================================================
  section_start "database_info" "7. 数据库信息"

  echo "<h3>7.1 数据库大小</h3>"
  safe_sql_html "
    SELECT
      table_schema AS '数据库',
      COALESCE(SUM(table_rows), 0) AS '行数',
      ROUND(COALESCE(SUM(data_length), 0) / 1024 / 1024, 2) AS '数据大小(MB)',
      ROUND(COALESCE(SUM(index_length), 0) / 1024 / 1024, 2) AS '索引大小(MB)'
    FROM information_schema.TABLES
    WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
    GROUP BY table_schema
    ORDER BY SUM(data_length) DESC;
  " "无用户数据库"

  echo "<h3>7.2 当前进程列表 (Top ${PROCESSLIST_LIMIT})</h3>"
  # MySQL 8.0+ 优先使用 performance_schema.processlist，fallback 到 information_schema
  if [[ "$IS_MYSQL8" == true ]]; then
    safe_sql_html "
      SELECT Id AS 'ID', User AS '用户', Host AS '主机',
             IFNULL(db, '') AS '数据库', Command AS '命令',
             Time AS '时间(s)', IFNULL(State, '') AS '状态'
      FROM performance_schema.processlist
      ORDER BY Time DESC
      LIMIT ${PROCESSLIST_LIMIT};
    " "$(safe_sql_html "
      SELECT Id AS 'ID', User AS '用户', Host AS '主机',
             IFNULL(db, '') AS '数据库', Command AS '命令',
             Time AS '时间(s)', IFNULL(State, '') AS '状态'
      FROM information_schema.PROCESSLIST
      ORDER BY Time DESC
      LIMIT ${PROCESSLIST_LIMIT};
    " "无法获取进程列表")"
  else
    safe_sql_html "
      SELECT Id AS 'ID', User AS '用户', Host AS '主机',
             IFNULL(db, '') AS '数据库', Command AS '命令',
             Time AS '时间(s)', IFNULL(State, '') AS '状态'
      FROM information_schema.PROCESSLIST
      ORDER BY Time DESC
      LIMIT ${PROCESSLIST_LIMIT};
    " "无法获取进程列表"
  fi

  echo "<h3>7.3 存储引擎</h3>"
  safe_sql_html "
    SELECT Engine AS '引擎', Support AS '支持状态', Comment AS '说明'
    FROM information_schema.ENGINES
    WHERE Support != 'NO'
    ORDER BY Support DESC, Engine;
  "

  section_end
  next_section "数据库信息"

  # ================================================================
  # 8. 安全信息
  # ================================================================
  section_start "security" "8. 安全信息"

  echo "<h3>8.1 数据库用户</h3>"
  safe_sql_html "
    SELECT user AS '用户名', host AS '主机', Grant_priv AS '授权权限',
           plugin AS '认证插件', account_locked AS '账户锁定'
    FROM mysql.user
    WHERE user NOT IN ('mysql.infoschema','mysql.session','mysql.sys')
    ORDER BY user;
  " "无法获取用户信息（可能权限不足）"

  echo "<h3>8.2 复制状态</h3>"
  # MySQL 8.0.22+ uses SHOW REPLICA STATUS; fallback to SHOW SLAVE STATUS
  SLAVE_STATUS=$(run_sql_html "SHOW REPLICA STATUS;" 2>/dev/null || run_sql_html "SHOW SLAVE STATUS;" 2>/dev/null || true)
  if [[ -n "$SLAVE_STATUS" && "$SLAVE_STATUS" == *"<TABLE"* ]]; then
    echo "$SLAVE_STATUS"
  else
    echo "<p>未配置主从复制，或无权限查看</p>"
  fi

  echo "<h3>8.3 复制配置</h3>"
  safe_sql_html "
    SELECT
      v.Variable_name AS '参数',
      v.Variable_value AS '值'
    FROM (
      SELECT 'server_id' AS Variable_name,
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='server_id') AS Variable_value
      UNION ALL SELECT 'gtid_mode',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='gtid_mode')
      UNION ALL SELECT 'enforce_gtid_consistency',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='enforce_gtid_consistency')
    ) v;
  "

  section_end
  next_section "安全信息"

  # ================================================================
  # 9. InnoDB 深度诊断
  # ================================================================
  section_start "innodb_deep" "9. InnoDB 深度诊断"

  echo "<h3>9.1 InnoDB 状态摘要</h3>"
  # Extract key metrics from SHOW ENGINE INNODB STATUS
  INNODB_STATUS=$(run_sql "SHOW ENGINE INNODB STATUS\G" 2>/dev/null || echo "")
  if [[ -n "$INNODB_STATUS" ]]; then
    HISTORY_LIST=$(echo "$INNODB_STATUS" | grep -oP 'History list length \K[0-9]+' 2>/dev/null || echo "N/A")
    LOG_SEQ=$(echo "$INNODB_STATUS" | grep -oP 'Log sequence number\s+\K[0-9]+' 2>/dev/null || echo "N/A")
    PAGES_DIRTY=$(echo "$INNODB_STATUS" | grep -oP 'Modified db pages\s+\K[0-9]+' 2>/dev/null || echo "N/A")
    PENDING_READS=$(echo "$INNODB_STATUS" | grep -oP 'Pending normal aio reads:\s*\[?\K[0-9]+' 2>/dev/null || echo "N/A")
    PENDING_WRITES=$(echo "$INNODB_STATUS" | grep -oP 'Pending normal aio writes:\s*\[?\K[0-9]+' 2>/dev/null || echo "N/A")

    kv_table_start
    kv_table_row "History List Length (undo 未清理事务)" "$HISTORY_LIST"
    kv_table_row "Log Sequence Number" "$LOG_SEQ"
    kv_table_row "脏页数量" "$PAGES_DIRTY"
    kv_table_row "Pending Reads" "$PENDING_READS"
    kv_table_row "Pending Writes" "$PENDING_WRITES"
    kv_table_end

    # 9.2 最近死锁
    echo "<h3>9.2 最近死锁信息</h3>"
    DEADLOCK_SECTION=$(echo "$INNODB_STATUS" | sed -n '/LATEST DETECTED DEADLOCK/,/^---/p' 2>/dev/null || echo "")
    if [[ -n "$DEADLOCK_SECTION" && "$DEADLOCK_SECTION" != *"---"* ]] || [[ "$DEADLOCK_SECTION" == *"TRANSACTION"* ]]; then
      # Escape for HTML
      DEADLOCK_HTML=$(echo "$DEADLOCK_SECTION" | head -50 | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
      echo "<pre style=\"background:#fff3e0;padding:10px;border-radius:4px;font-size:12px;overflow-x:auto;max-height:300px\">${DEADLOCK_HTML}</pre>"
    else
      echo "<p class=\"ok\">未检测到死锁</p>"
    fi
  else
    echo "<p>无法获取 InnoDB Status（可能权限不足）</p>"
  fi

  # 9.3 长事务
  echo "<h3>9.3 长事务 (>60s)</h3>"
  safe_sql_html "
    SELECT trx_id AS 'TrxID',
           trx_state AS '状态',
           TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS '持续(s)',
           trx_rows_locked AS '锁定行',
           trx_rows_modified AS '修改行',
           LEFT(trx_query, 100) AS 'SQL(截断)'
    FROM information_schema.INNODB_TRX
    WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 60
    ORDER BY trx_started
    LIMIT 20;
  " "无长事务或无权限查看"

  # 9.4 当前锁等待（MySQL 8.0+ 使用 data_lock_waits，5.7 fallback）
  echo "<h3>9.4 当前锁等待</h3>"
  if [[ "$IS_MYSQL8" == true ]]; then
    safe_sql_html "
      SELECT
        w.REQUESTING_ENGINE_TRANSACTION_ID AS '等待TrxID',
        r.trx_state AS '等待状态',
        w.BLOCKING_ENGINE_TRANSACTION_ID AS '阻塞TrxID',
        b.trx_state AS '阻塞状态',
        TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS '等待(s)',
        LEFT(r.trx_query, 80) AS '等待SQL'
      FROM performance_schema.data_lock_waits w
      JOIN information_schema.INNODB_TRX r
        ON w.REQUESTING_ENGINE_TRANSACTION_ID = r.trx_id
      JOIN information_schema.INNODB_TRX b
        ON w.BLOCKING_ENGINE_TRANSACTION_ID = b.trx_id
      LIMIT 20;
    " "无锁等待或无权限查看"
  else
    safe_sql_html "
      SELECT
        r.trx_id AS '等待TrxID',
        r.trx_state AS '等待状态',
        b.trx_id AS '阻塞TrxID',
        b.trx_state AS '阻塞状态',
        TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS '等待(s)',
        LEFT(r.trx_query, 80) AS '等待SQL'
      FROM information_schema.INNODB_LOCK_WAITS w
      JOIN information_schema.INNODB_TRX r ON w.requesting_trx_id = r.trx_id
      JOIN information_schema.INNODB_TRX b ON w.blocking_trx_id = b.trx_id
      LIMIT 20;
    " "无锁等待或无权限查看"
  fi

  section_end
  next_section "InnoDB 深度诊断"

  # ================================================================
  # 10. 表空间分析
  # ================================================================
  section_start "table_analysis" "10. 表空间分析"

  echo "<h3>10.1 Top ${TOP_TABLES_LIMIT} 大表</h3>"
  safe_sql_html "
    SELECT
      t.table_schema AS '库',
      t.table_name AS '表',
      t.engine AS '引擎',
      COALESCE(t.table_rows, 0) AS '行数',
      ROUND(COALESCE(t.data_length, 0) / 1024 / 1024, 2) AS '数据(MB)',
      ROUND(COALESCE(t.index_length, 0) / 1024 / 1024, 2) AS '索引(MB)',
      ROUND(COALESCE(t.data_free, 0) / 1024 / 1024, 2) AS '碎片(MB)',
      IFNULL(t.create_options, '') AS '选项'
    FROM information_schema.TABLES t
    WHERE t.table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
      AND t.table_type = 'BASE TABLE'
    ORDER BY (t.data_length + t.index_length) DESC
    LIMIT ${TOP_TABLES_LIMIT};
  "

  echo "<h3>10.2 无主键表</h3>"
  safe_sql_html "
    SELECT t.table_schema AS '库', t.table_name AS '表',
           t.engine AS '引擎', COALESCE(t.table_rows, 0) AS '行数'
    FROM information_schema.TABLES t
    LEFT JOIN information_schema.TABLE_CONSTRAINTS tc
      ON t.table_schema = tc.table_schema
      AND t.table_name = tc.table_name
      AND tc.constraint_type = 'PRIMARY KEY'
    WHERE t.table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
      AND t.table_type = 'BASE TABLE'
      AND tc.constraint_name IS NULL
    ORDER BY t.table_rows DESC
    LIMIT 50;
  "

  echo "<h3>10.3 高碎片率表 (碎片 > 30%)</h3>"
  safe_sql_html "
    SELECT table_schema AS '库', table_name AS '表',
           ROUND(data_length / 1024 / 1024, 2) AS '数据(MB)',
           ROUND(data_free / 1024 / 1024, 2) AS '碎片(MB)',
           ROUND(data_free / (data_length + data_free) * 100, 1) AS '碎片率(%)'
    FROM information_schema.TABLES
    WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
      AND table_type = 'BASE TABLE'
      AND data_length > 0
      AND data_free / (data_length + data_free) > 0.3
    ORDER BY data_free DESC
    LIMIT 20;
  "

  echo "<h3>10.4 未使用的索引</h3>"
  safe_sql_html "
    SELECT s.object_schema AS '库', s.object_name AS '表', s.index_name AS '索引',
           ROUND(COALESCE(
             (SELECT SUM(stat_value) * @@innodb_page_size / 1024 / 1024
              FROM mysql.innodb_index_stats
              WHERE database_name = s.object_schema
                AND table_name = s.object_name
                AND index_name = s.index_name
                AND stat_name = 'size'), 0), 2) AS '大小(MB)'
    FROM performance_schema.table_io_waits_summary_by_index_usage s
    WHERE s.index_name IS NOT NULL
      AND s.index_name != 'PRIMARY'
      AND s.object_schema NOT IN ('information_schema','performance_schema','mysql','sys')
      AND s.count_star = 0
    ORDER BY s.object_schema, s.object_name
    LIMIT 30;
  " "无法获取（需要 performance_schema 启用）"

  echo "<h3>10.5 冗余索引</h3>"
  safe_sql_html "
    SELECT a.table_schema AS '库', a.table_name AS '表',
           a.index_name AS '冗余索引',
           a.column_name AS '冗余索引列',
           b.index_name AS '覆盖索引',
           b.column_name AS '覆盖索引列'
    FROM (
      SELECT table_schema, table_name, index_name,
             GROUP_CONCAT(column_name ORDER BY seq_in_index) AS column_name
      FROM information_schema.STATISTICS
      WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
      GROUP BY table_schema, table_name, index_name
    ) a
    JOIN (
      SELECT table_schema, table_name, index_name,
             GROUP_CONCAT(column_name ORDER BY seq_in_index) AS column_name
      FROM information_schema.STATISTICS
      WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
      GROUP BY table_schema, table_name, index_name
    ) b
    ON a.table_schema = b.table_schema
      AND a.table_name = b.table_name
      AND a.index_name != b.index_name
      AND LOCATE(a.column_name, b.column_name) = 1
      AND a.column_name != b.column_name
    LIMIT 30;
  " "无冗余索引"

  echo "<h3>10.6 表级 IO 热点 Top 10</h3>"
  safe_sql_html "
    SELECT
      OBJECT_SCHEMA AS '库',
      OBJECT_NAME AS '表',
      COUNT_READ AS '读次数',
      COUNT_WRITE AS '写次数',
      COUNT_FETCH AS 'Fetch次数',
      COUNT_INSERT AS 'Insert次数',
      COUNT_UPDATE AS 'Update次数',
      COUNT_DELETE AS 'Delete次数',
      ROUND(SUM_TIMER_WAIT / 1000000000000, 2) AS '总耗时(s)'
    FROM performance_schema.table_io_waits_summary_by_table
    WHERE OBJECT_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
    ORDER BY SUM_TIMER_WAIT DESC
    LIMIT 10;
  " "performance_schema 未启用或无数据"

  section_end
  next_section "表空间分析"

  # ================================================================
  # 11. 等待事件
  # ================================================================
  section_start "wait_events" "11. 等待事件 (Top 20)"

  safe_sql_html "
    SELECT event_name AS '等待事件',
           count_star AS '总次数',
           ROUND(sum_timer_wait / 1000000000000, 4) AS '总等待(s)',
           ROUND(avg_timer_wait / 1000000000, 4) AS '平均等待(ms)'
    FROM performance_schema.events_waits_summary_global_by_event_name
    WHERE count_star > 0
      AND event_name NOT LIKE 'idle%'
    ORDER BY sum_timer_wait DESC
    LIMIT 20;
  " "无法获取等待事件（需要 performance_schema 启用）"

  section_end
  next_section "等待事件"

  # ================================================================
  # 12. SSL/TLS 安全状态 (v3.0 新增)
  # ================================================================
  section_start "ssl_status" "12. SSL/TLS 安全状态"

  echo "<h3>12.1 SSL 配置</h3>"
  safe_sql_html "
    SELECT
      v.Variable_name AS '参数',
      v.Variable_value AS '值'
    FROM (
      SELECT 'have_ssl' AS Variable_name,
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='have_ssl') AS Variable_value
      UNION ALL SELECT 'have_openssl',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='have_openssl')
      UNION ALL SELECT 'ssl_ca',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='ssl_ca')
      UNION ALL SELECT 'ssl_cert',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='ssl_cert')
      UNION ALL SELECT 'ssl_key',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='ssl_key')
      UNION ALL SELECT 'require_secure_transport',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='require_secure_transport')
      UNION ALL SELECT 'tls_version',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='tls_version')
    ) v;
  " "无法获取 SSL 配置"

  echo "<h3>12.2 当前连接 SSL 使用情况</h3>"
  safe_sql_html "
    SELECT
      IFNULL(ssl_version, '未加密') AS 'SSL版本',
      COUNT(*) AS '连接数'
    FROM performance_schema.threads t
    JOIN performance_schema.tls_channel_status tcs ON 1=0
    UNION ALL
    SELECT
      CASE WHEN variable_value != '' THEN variable_value ELSE '未加密' END AS 'SSL版本',
      1 AS '连接数'
    FROM performance_schema.session_status
    WHERE variable_name = 'Ssl_version'
    LIMIT 1;
  " "$(
    # Fallback: 只展示当前会话 SSL 状态
    kv_table_start
    SSL_VER=$(run_sql "SHOW STATUS LIKE 'Ssl_version'" | awk '{print $2}' || echo "")
    SSL_CIPHER=$(run_sql "SHOW STATUS LIKE 'Ssl_cipher'" | awk '{print $2}' || echo "")
    if [[ -n "$SSL_VER" && "$SSL_VER" != "" ]]; then
      kv_table_row "当前连接 SSL 版本" "$SSL_VER"
      kv_table_row "当前连接 Cipher" "$SSL_CIPHER"
    else
      kv_table_row "当前连接" "未使用 SSL 加密"
    fi
    kv_table_end
  )"

  section_end
  next_section "SSL/TLS 安全状态"

  # ================================================================
  # 13. 密码策略与认证安全 (v3.0 新增)
  # ================================================================
  section_start "password_policy" "13. 密码策略与认证安全"

  echo "<h3>13.1 密码策略 (validate_password)</h3>"
  # MySQL 8.0 uses component, 5.7 uses plugin
  VALIDATE_PW=$(run_sql "
    SELECT VARIABLE_VALUE FROM performance_schema.global_variables
    WHERE VARIABLE_NAME LIKE 'validate_password%policy'" | head -1 || echo "")
  if [[ -n "$VALIDATE_PW" ]]; then
    safe_sql_html "
      SELECT VARIABLE_NAME AS '参数', VARIABLE_VALUE AS '值'
      FROM performance_schema.global_variables
      WHERE VARIABLE_NAME LIKE 'validate_password%'
      ORDER BY VARIABLE_NAME;
    "
  else
    echo "<p>validate_password 插件/组件未启用</p>"
  fi

  echo "<h3>13.2 使用旧认证插件的用户</h3>"
  safe_sql_html "
    SELECT user AS '用户名', host AS '主机', plugin AS '认证插件'
    FROM mysql.user
    WHERE plugin IN ('mysql_old_password', 'mysql_native_password')
      AND user NOT IN ('mysql.infoschema','mysql.session','mysql.sys')
    ORDER BY user;
  " "所有用户均使用安全认证插件"

  echo "<h3>13.3 无密码用户</h3>"
  safe_sql_html "
    SELECT user AS '用户名', host AS '主机', plugin AS '认证插件'
    FROM mysql.user
    WHERE authentication_string = '' OR authentication_string IS NULL
    ORDER BY user;
  " "无法检测或所有用户均已设置密码"

  section_end
  next_section "密码策略与认证安全"

  # ================================================================
  # 14. auto_increment 溢出预警 (v3.0 新增)
  # ================================================================
  section_start "autoinc_check" "14. auto_increment 使用率"

  safe_sql_html "
    SELECT
      t.TABLE_SCHEMA AS '库',
      t.TABLE_NAME AS '表',
      c.COLUMN_TYPE AS '列类型',
      t.AUTO_INCREMENT AS '当前值',
      CASE
        WHEN c.COLUMN_TYPE LIKE 'tinyint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 255, 127)
        WHEN c.COLUMN_TYPE LIKE 'smallint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 65535, 32767)
        WHEN c.COLUMN_TYPE LIKE 'mediumint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 16777215, 8388607)
        WHEN c.COLUMN_TYPE LIKE 'int%' AND c.COLUMN_TYPE NOT LIKE 'bigint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 4294967295, 2147483647)
        WHEN c.COLUMN_TYPE LIKE 'bigint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 18446744073709551615, 9223372036854775807)
        ELSE 0
      END AS '最大值',
      ROUND(
        t.AUTO_INCREMENT /
        CASE
          WHEN c.COLUMN_TYPE LIKE 'tinyint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 255, 127)
          WHEN c.COLUMN_TYPE LIKE 'smallint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 65535, 32767)
          WHEN c.COLUMN_TYPE LIKE 'mediumint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 16777215, 8388607)
          WHEN c.COLUMN_TYPE LIKE 'int%' AND c.COLUMN_TYPE NOT LIKE 'bigint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 4294967295, 2147483647)
          WHEN c.COLUMN_TYPE LIKE 'bigint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 18446744073709551615, 9223372036854775807)
          ELSE 1
        END * 100, 2) AS '使用率(%)'
    FROM information_schema.TABLES t
    JOIN information_schema.COLUMNS c
      ON t.TABLE_SCHEMA = c.TABLE_SCHEMA
      AND t.TABLE_NAME = c.TABLE_NAME
      AND c.EXTRA LIKE '%auto_increment%'
    WHERE t.TABLE_SCHEMA NOT IN ('information_schema','performance_schema','mysql','sys')
      AND t.AUTO_INCREMENT IS NOT NULL
    HAVING \`使用率(%)\` > 30
    ORDER BY \`使用率(%)\` DESC
    LIMIT 30;
  " "所有 auto_increment 列使用率正常"

  section_end
  next_section "auto_increment 使用率"

  # ================================================================
  # 15. 长查询 (v3.0 新增)
  # ================================================================
  section_start "long_queries" "15. 长查询 (>${LONG_QUERY_SECONDS}s)"

  if [[ "$IS_MYSQL8" == true ]]; then
    safe_sql_html "
      SELECT
        PROCESSLIST_ID AS 'ID',
        PROCESSLIST_USER AS '用户',
        PROCESSLIST_DB AS '数据库',
        PROCESSLIST_COMMAND AS '命令',
        PROCESSLIST_TIME AS '时间(s)',
        PROCESSLIST_STATE AS '状态',
        LEFT(PROCESSLIST_INFO, 120) AS 'SQL(截断)'
      FROM performance_schema.threads
      WHERE PROCESSLIST_COMMAND NOT IN ('Sleep','Daemon','Binlog Dump')
        AND PROCESSLIST_TIME > ${LONG_QUERY_SECONDS}
        AND PROCESSLIST_INFO IS NOT NULL
        AND TYPE = 'FOREGROUND'
      ORDER BY PROCESSLIST_TIME DESC
      LIMIT 20;
    " "无长查询"
  else
    safe_sql_html "
      SELECT Id AS 'ID', User AS '用户', db AS '数据库',
             Command AS '命令', Time AS '时间(s)',
             State AS '状态', LEFT(Info, 120) AS 'SQL(截断)'
      FROM information_schema.PROCESSLIST
      WHERE Command NOT IN ('Sleep','Daemon','Binlog Dump')
        AND Time > ${LONG_QUERY_SECONDS}
        AND Info IS NOT NULL
      ORDER BY Time DESC
      LIMIT 20;
    " "无长查询"
  fi

  section_end
  next_section "长查询"

  # ================================================================
  # 16. 复制深度诊断 (v3.1 新增)
  # ================================================================
  section_start "replication_deep" "16. 复制深度诊断"

  # 16.1 主库状态 (SHOW MASTER STATUS / SHOW BINARY LOG STATUS for 8.2+)
  echo "<h3>16.1 主库 Binlog 状态</h3>"
  MASTER_STATUS=$(run_sql_html "SHOW BINARY LOG STATUS;" 2>/dev/null || run_sql_html "SHOW MASTER STATUS;" 2>/dev/null || true)
  if [[ -n "$MASTER_STATUS" && "$MASTER_STATUS" == *"<T"* ]]; then
    echo "$MASTER_STATUS"
  else
    echo "<p>非主库角色，或无权限查看 (需要 REPLICATION CLIENT 权限)</p>"
  fi

  # 16.2 已连接从库列表 (SHOW REPLICAS / SHOW SLAVE HOSTS)
  echo "<h3>16.2 已连接从库</h3>"
  REPLICAS=$(run_sql_html "SHOW REPLICAS;" 2>/dev/null || run_sql_html "SHOW SLAVE HOSTS;" 2>/dev/null || true)
  if [[ -n "$REPLICAS" && "$REPLICAS" == *"<T"* ]]; then
    echo "$REPLICAS"
  else
    echo "<p>无已连接从库，或非主库角色</p>"
  fi

  # 16.3 从库复制状态详情 (已有 8.2 的基础，这里做更完整的采集)
  echo "<h3>16.3 从库复制状态</h3>"
  # 多通道复制: SHOW REPLICA STATUS 返回所有 channel
  SLAVE_DETAIL=$(run_sql_html "SHOW REPLICA STATUS;" 2>/dev/null || run_sql_html "SHOW SLAVE STATUS;" 2>/dev/null || true)
  if [[ -n "$SLAVE_DETAIL" && "$SLAVE_DETAIL" == *"<T"* ]]; then
    echo "$SLAVE_DETAIL"
  else
    echo "<p>未配置从库复制</p>"
  fi

  # 16.4 半同步复制状态
  echo "<h3>16.4 半同步复制</h3>"
  # Check source-side (master) semi-sync
  SEMISYNC_SOURCE=$(run_sql "
    SELECT VARIABLE_NAME FROM performance_schema.global_variables
    WHERE VARIABLE_NAME LIKE 'rpl_semi_sync_source%' OR VARIABLE_NAME LIKE 'rpl_semi_sync_master%'
    LIMIT 1;" 2>/dev/null || echo "")
  if [[ -n "$SEMISYNC_SOURCE" ]]; then
    safe_sql_html "
      SELECT VARIABLE_NAME AS '参数', VARIABLE_VALUE AS '值'
      FROM performance_schema.global_variables
      WHERE VARIABLE_NAME LIKE 'rpl_semi_sync_source%'
         OR VARIABLE_NAME LIKE 'rpl_semi_sync_master%'
      ORDER BY VARIABLE_NAME;
    " "无法获取半同步参数"

    echo "<h4>半同步状态指标</h4>"
    safe_sql_html "
      SELECT VARIABLE_NAME AS '指标', VARIABLE_VALUE AS '值'
      FROM performance_schema.global_status
      WHERE VARIABLE_NAME LIKE 'Rpl_semi_sync_source%'
         OR VARIABLE_NAME LIKE 'Rpl_semi_sync_master%'
      ORDER BY VARIABLE_NAME;
    " "无法获取半同步状态"
  else
    # Check replica-side semi-sync
    SEMISYNC_REPLICA=$(run_sql "
      SELECT VARIABLE_NAME FROM performance_schema.global_variables
      WHERE VARIABLE_NAME LIKE 'rpl_semi_sync_replica%' OR VARIABLE_NAME LIKE 'rpl_semi_sync_slave%'
      LIMIT 1;" 2>/dev/null || echo "")
    if [[ -n "$SEMISYNC_REPLICA" ]]; then
      safe_sql_html "
        SELECT VARIABLE_NAME AS '参数', VARIABLE_VALUE AS '值'
        FROM performance_schema.global_variables
        WHERE VARIABLE_NAME LIKE 'rpl_semi_sync_replica%'
           OR VARIABLE_NAME LIKE 'rpl_semi_sync_slave%'
        ORDER BY VARIABLE_NAME;
      " "无法获取半同步参数"
    else
      echo "<p>半同步复制插件未安装</p>"
    fi
  fi

  # 16.5 Group Replication / InnoDB Cluster 状态
  echo "<h3>16.5 Group Replication</h3>"
  GR_ENABLED=$(run_sql "
    SELECT VARIABLE_VALUE FROM performance_schema.global_variables
    WHERE VARIABLE_NAME = 'group_replication_group_name';" 2>/dev/null || echo "")
  if [[ -n "$GR_ENABLED" && "$GR_ENABLED" != "" ]]; then
    echo "<h4>集群成员状态</h4>"
    safe_sql_html "
      SELECT
        MEMBER_ID AS '成员ID',
        MEMBER_HOST AS '主机',
        MEMBER_PORT AS '端口',
        MEMBER_STATE AS '状态',
        MEMBER_ROLE AS '角色',
        MEMBER_VERSION AS '版本'
      FROM performance_schema.replication_group_members;
    " "无法获取 GR 成员信息"

    echo "<h4>集群统计</h4>"
    safe_sql_html "
      SELECT
        COUNT(*) AS '成员总数',
        SUM(CASE WHEN MEMBER_STATE = 'ONLINE' THEN 1 ELSE 0 END) AS 'ONLINE',
        SUM(CASE WHEN MEMBER_STATE = 'RECOVERING' THEN 1 ELSE 0 END) AS 'RECOVERING',
        SUM(CASE WHEN MEMBER_STATE NOT IN ('ONLINE','RECOVERING') THEN 1 ELSE 0 END) AS '异常'
      FROM performance_schema.replication_group_members;
    " "无法获取 GR 统计"

    echo "<h4>GR 配置</h4>"
    safe_sql_html "
      SELECT VARIABLE_NAME AS '参数', VARIABLE_VALUE AS '值'
      FROM performance_schema.global_variables
      WHERE VARIABLE_NAME LIKE 'group_replication%'
      ORDER BY VARIABLE_NAME;
    " "无法获取 GR 配置"
  else
    echo "<p>未启用 Group Replication</p>"
  fi

  # 16.6 复制过滤器
  echo "<h3>16.6 复制过滤器</h3>"
  safe_sql_html "
    SELECT VARIABLE_NAME AS '参数', VARIABLE_VALUE AS '值'
    FROM performance_schema.global_variables
    WHERE VARIABLE_NAME IN (
      'replicate_do_db', 'replicate_ignore_db',
      'replicate_do_table', 'replicate_ignore_table',
      'replicate_wild_do_table', 'replicate_wild_ignore_table',
      'binlog_do_db', 'binlog_ignore_db'
    )
    AND VARIABLE_VALUE != ''
    ORDER BY VARIABLE_NAME;
  " "未配置复制过滤器"

  # 16.7 GTID 信息
  echo "<h3>16.7 GTID 信息</h3>"
  GTID_MODE=$(run_sql "SELECT @@gtid_mode;" 2>/dev/null || echo "OFF")
  if [[ "$GTID_MODE" == "ON" ]]; then
    safe_sql_html "
      SELECT
        v.Variable_name AS '参数',
        v.Variable_value AS '值'
      FROM (
        SELECT 'gtid_mode' AS Variable_name, @@gtid_mode AS Variable_value
        UNION ALL SELECT 'enforce_gtid_consistency', @@enforce_gtid_consistency
        UNION ALL SELECT 'gtid_executed (count)',
          CAST(
            (LENGTH(@@global.gtid_executed) - LENGTH(REPLACE(@@global.gtid_executed, ':', ''))) + 1
          AS CHAR)
        UNION ALL SELECT 'gtid_purged (count)',
          CAST(
            CASE WHEN @@global.gtid_purged = '' THEN 0
            ELSE (LENGTH(@@global.gtid_purged) - LENGTH(REPLACE(@@global.gtid_purged, ':', ''))) + 1
            END
          AS CHAR)
      ) v;
    " "无法获取 GTID 信息"

    # GTID executed & purged full values (truncated for display)
    GTID_EXEC=$(run_sql "SELECT @@global.gtid_executed;" 2>/dev/null || echo "")
    GTID_PURG=$(run_sql "SELECT @@global.gtid_purged;" 2>/dev/null || echo "")
    kv_table_start
    kv_table_row "gtid_executed" "$(echo "$GTID_EXEC" | head -c 200)"
    kv_table_row "gtid_purged" "$(echo "$GTID_PURG" | head -c 200)"
    kv_table_end
  else
    echo "<p>GTID 未启用 (gtid_mode = ${GTID_MODE})</p>"
  fi

  # 16.8 复制通道状态 (多源复制, MySQL 8.0+)
  echo "<h3>16.8 复制通道</h3>"
  if [[ "$IS_MYSQL8" == true ]]; then
    safe_sql_html "
      SELECT
        c.CHANNEL_NAME AS '通道',
        c.SERVICE_STATE AS 'IO状态',
        a.SERVICE_STATE AS 'SQL状态',
        TIMESTAMPDIFF(SECOND, c.LAST_HEARTBEAT_TIMESTAMP, NOW()) AS '心跳延迟(s)',
        c.RECEIVED_TRANSACTION_SET AS '已接收GTID集'
      FROM performance_schema.replication_connection_status c
      LEFT JOIN performance_schema.replication_applier_status a
        ON c.CHANNEL_NAME = a.CHANNEL_NAME
      ORDER BY c.CHANNEL_NAME;
    " "无复制通道或 performance_schema 未启用"
  else
    echo "<p>多源复制通道视图需要 MySQL 8.0+</p>"
  fi

  section_end
  next_section "复制深度诊断"

  # ================================================================
  # 17. 备份检查
  # ================================================================
  section_start "backup_check" "17. 备份检查"

  # 17.1 Binlog 备份补充配置
  # log_bin / binlog_format / sync_binlog / expire_logs_days / binlog_expire_logs_seconds
  # 已在 section 4（日志配置）中采集，此处仅采集备份专属参数
  echo "<h3>17.1 Binlog 备份补充配置</h3>"
  safe_sql_html "
    SHOW VARIABLES WHERE Variable_name IN ('max_binlog_size', 'binlog_cache_size', 'binlog_row_image');
  " "无法获取 Binlog 备份配置"

  # 17.2 Binlog 文件列表
  echo "<h3>17.2 Binlog 文件列表</h3>"
  safe_sql_html "SHOW BINARY LOGS;" "Binlog 未启用或无权限查看"

  # 17.3 InnoDB 持久化补充配置
  # innodb_flush_log_at_trx_commit / innodb_doublewrite / innodb_log_file_size
  # 已在 section 4/5 中采集，此处仅采集备份专属参数
  echo "<h3>17.3 InnoDB 持久化补充配置</h3>"
  safe_sql_html "
    SHOW VARIABLES WHERE Variable_name IN ('innodb_redo_log_capacity', 'innodb_log_files_in_group', 'innodb_data_file_path');
  " "无法获取 InnoDB 持久化配置"

  # 17.4 备份相关状态指标
  echo "<h3>17.4 备份相关状态指标</h3>"
  safe_sql_html "
    SHOW GLOBAL STATUS WHERE Variable_name IN ('Binlog_cache_disk_use', 'Binlog_cache_use', 'Com_flush', 'Innodb_os_log_written');
  " "无法获取备份状态指标"

  # 17.5 最近一次备份事件（event_scheduler 备份任务检查）
  echo "<h3>17.5 备份计划任务</h3>"
  safe_sql_html "
    SELECT
      EVENT_SCHEMA AS '数据库',
      EVENT_NAME AS '事件名',
      STATUS AS '状态',
      EVENT_TYPE AS '类型',
      INTERVAL_VALUE AS '间隔值',
      INTERVAL_FIELD AS '间隔单位',
      LAST_EXECUTED AS '最后执行时间',
      STARTS AS '开始时间'
    FROM information_schema.EVENTS
    WHERE EVENT_NAME LIKE '%backup%'
       OR EVENT_NAME LIKE '%bak%'
       OR EVENT_NAME LIKE '%dump%'
    ORDER BY LAST_EXECUTED DESC;
  " "未检测到备份相关的定时事件"

  section_end
  next_section "备份检查"

  # ================================================================
  # 18. 系统资源
  # ================================================================
  section_start "system_info" "18. 系统资源"

  if [[ "$COLLECT_SYSTEM" == "yes" ]]; then
    echo "<h3>18.1 主机信息</h3>"
    SYS_HOSTNAME=$(hostname 2>/dev/null || echo "N/A")
    SYS_PLATFORM=$(uname -a 2>/dev/null || echo "N/A")
    SYS_UPTIME=$(uptime 2>/dev/null || echo "N/A")

    kv_table_start
    kv_table_row "主机名" "$SYS_HOSTNAME"
    kv_table_row "系统平台" "$SYS_PLATFORM"
    kv_table_row "系统运行时间" "$SYS_UPTIME"
    kv_table_end

    # CPU
    echo "<h3>18.2 CPU 信息</h3>"
    if command -v nproc &>/dev/null; then
      CPU_LOGICAL=$(nproc 2>/dev/null || echo "N/A")
    else
      CPU_LOGICAL=$(grep -c processor /proc/cpuinfo 2>/dev/null || echo "N/A")
    fi
    CPU_PHYSICAL=$(grep "physical id" /proc/cpuinfo 2>/dev/null | sort -u | wc -l || echo "N/A")
    CPU_CORES=$(grep "cpu cores" /proc/cpuinfo 2>/dev/null | head -1 | awk -F: '{print $2}' | tr -d ' ' || echo "N/A")
    CPU_MODEL=$(grep "model name" /proc/cpuinfo 2>/dev/null | head -1 | awk -F: '{print $2}' | sed 's/^ //' || echo "N/A")

    # CPU usage - 1 second sample
    if command -v mpstat &>/dev/null; then
      CPU_USAGE=$(mpstat 1 1 2>/dev/null | tail -1 | awk '{printf "%.1f", 100-$NF}' || echo "N/A")
    elif [[ -f /proc/stat ]]; then
      read -r _ user1 nice1 sys1 idle1 _ < /proc/stat
      sleep 1
      read -r _ user2 nice2 sys2 idle2 _ < /proc/stat
      total=$((user2+nice2+sys2+idle2 - user1-nice1-sys1-idle1))
      idle=$((idle2 - idle1))
      if [[ $total -gt 0 ]]; then
        CPU_USAGE=$(awk "BEGIN {printf \"%.1f\", (1-${idle}/${total})*100}")
      else
        CPU_USAGE="N/A"
      fi
    else
      CPU_USAGE="N/A"
    fi

    kv_table_start
    kv_table_row "CPU 型号" "$CPU_MODEL"
    kv_table_row "物理 CPU 数" "$CPU_PHYSICAL"
    kv_table_row "每 CPU 核心数" "$CPU_CORES"
    kv_table_row "逻辑核心数" "$CPU_LOGICAL"
    kv_table_row "CPU 使用率" "${CPU_USAGE}%"
    kv_table_end

    # Load Average (6.3)
    echo "<h3>18.2.0 Load Average</h3>"
    if [[ -f /proc/loadavg ]]; then
      LOAD_AVG=$(awk '{print $1, $2, $3}' /proc/loadavg 2>/dev/null || echo "N/A N/A N/A")
      LOAD_1=$(echo "$LOAD_AVG" | awk '{print $1}')
      LOAD_5=$(echo "$LOAD_AVG" | awk '{print $2}')
      LOAD_15=$(echo "$LOAD_AVG" | awk '{print $3}')
      kv_table_start
      kv_table_row "Load Average (1min)" "$LOAD_1"
      kv_table_row "Load Average (5min)" "$LOAD_5"
      kv_table_row "Load Average (15min)" "$LOAD_15"
      kv_table_end
    else
      echo "<p>无法获取 Load Average</p>"
    fi

    # NUMA topology
    echo "<h3>18.2.1 NUMA 信息</h3>"
    if command -v numactl &>/dev/null; then
      NUMA_NODES=$(numactl --hardware 2>/dev/null | grep "available:" | awk '{print $2}' || echo "N/A")
      NUMA_DETAIL=$(numactl --hardware 2>/dev/null | grep -E "^node [0-9]+ (cpus|size|free)" || echo "")

      kv_table_start
      kv_table_row "NUMA 节点数" "$NUMA_NODES"
      if [[ -n "$NUMA_DETAIL" ]]; then
        while IFS= read -r line; do
          kv_table_row "NUMA" "$line"
        done <<< "$NUMA_DETAIL"
      fi
      kv_table_end
    else
      echo "<p>numactl 未安装，跳过 NUMA 信息采集</p>"
    fi

    # Memory
    echo "<h3>18.3 内存信息</h3>"
    if command -v free &>/dev/null; then
      MEM_INFO=$(free -b 2>/dev/null)
      MEM_TOTAL=$(echo "$MEM_INFO" | awk '/Mem:/ {printf "%.2f", $2/1024/1024/1024}')
      MEM_USED=$(echo "$MEM_INFO" | awk '/Mem:/ {printf "%.2f", $3/1024/1024/1024}')
      MEM_AVAIL=$(echo "$MEM_INFO" | awk '/Mem:/ {printf "%.2f", $7/1024/1024/1024}')
      MEM_PCT=$(echo "$MEM_INFO" | awk '/Mem:/ {printf "%.1f", $3/$2*100}')
      SWAP_TOTAL=$(echo "$MEM_INFO" | awk '/Swap:/ {printf "%.2f", $2/1024/1024/1024}')
      SWAP_USED=$(echo "$MEM_INFO" | awk '/Swap:/ {printf "%.2f", $3/1024/1024/1024}')
      SWAP_PCT=$(echo "$MEM_INFO" | awk '/Swap:/ {if($2>0) printf "%.1f", $3/$2*100; else print "0.0"}')

      kv_table_start
      kv_table_row "总内存" "${MEM_TOTAL} GB"
      kv_table_row "已使用" "${MEM_USED} GB"
      kv_table_row "可用内存" "${MEM_AVAIL} GB"
      kv_table_row "使用率" "${MEM_PCT}%"
      kv_table_row "Swap 总量" "${SWAP_TOTAL} GB"
      kv_table_row "Swap 已使用" "${SWAP_USED} GB"
      kv_table_row "Swap 使用率" "${SWAP_PCT}%"
      kv_table_end
    else
      echo "<p>无法获取内存信息</p>"
    fi

    # Disk
    echo "<h3>18.4 磁盘信息</h3>"
    if command -v df &>/dev/null; then
      echo "<table><tr><th>设备</th><th>挂载点</th><th>总容量</th><th>已用</th><th>可用</th><th>使用率</th></tr>"
      df -h 2>/dev/null | grep -v "tmpfs\|devtmpfs\|overlay\|^Filesystem" | while IFS= read -r line; do
        device=$(echo "$line" | awk '{print $1}')
        mount=$(echo "$line" | awk '{print $6}')
        total=$(echo "$line" | awk '{print $2}')
        used=$(echo "$line" | awk '{print $3}')
        avail=$(echo "$line" | awk '{print $4}')
        pct=$(echo "$line" | awk '{print $5}')

        # 标红高使用率
        pct_num=${pct%\%}
        if [[ "$pct_num" =~ ^[0-9]+$ ]] && [[ "$pct_num" -ge 90 ]]; then
          echo "<tr><td>${device}</td><td>${mount}</td><td>${total}</td><td>${used}</td><td>${avail}</td><td><font color=\"red\"><b>${pct}</b></font></td></tr>"
        elif [[ "$pct_num" =~ ^[0-9]+$ ]] && [[ "$pct_num" -ge 80 ]]; then
          echo "<tr><td>${device}</td><td>${mount}</td><td>${total}</td><td>${used}</td><td>${avail}</td><td><font color=\"orange\"><b>${pct}</b></font></td></tr>"
        else
          echo "<tr><td>${device}</td><td>${mount}</td><td>${total}</td><td>${used}</td><td>${avail}</td><td>${pct}</td></tr>"
        fi
      done
      echo "</table>"
    else
      echo "<p>无法获取磁盘信息</p>"
    fi

    # 18.5 磁盘 IO 统计 (iostat — Phase 6.2)
    echo "<h3>18.5 磁盘 IO 统计</h3>"
    if command -v iostat &>/dev/null; then
      # 采样 2 次（第一次是启动以来均值，第二次是增量），取第二次
      IOSTAT_RAW=$(iostat -x -d 2 2 2>/dev/null || echo "")
      if [[ -n "$IOSTAT_RAW" ]]; then
        # 获取第二段 Device 行之后的数据
        SECOND_BLOCK=$(echo "$IOSTAT_RAW" | awk 'BEGIN{n=0} /^Device/{n++} n==2{print}')
        if [[ -n "$SECOND_BLOCK" ]]; then
          echo "<table><tr><th>设备</th><th>r/s</th><th>w/s</th><th>rMB/s</th><th>wMB/s</th><th>await(ms)</th><th>r_await</th><th>w_await</th><th>%util</th></tr>"
          echo "$SECOND_BLOCK" | grep -v "^Device\|^$" | while read -r line; do
            dev=$(echo "$line" | awk '{print $1}')
            [[ -z "$dev" ]] && continue
            rs=$(echo "$line" | awk '{print $4}')
            ws=$(echo "$line" | awk '{print $5}')
            rmb=$(echo "$line" | awk '{printf "%.2f", $6/1024}' 2>/dev/null || echo "$line" | awk '{print $6}')
            wmb=$(echo "$line" | awk '{printf "%.2f", $7/1024}' 2>/dev/null || echo "$line" | awk '{print $7}')
            await=$(echo "$line" | awk '{print $10}')
            rawait=$(echo "$line" | awk '{print $11}')
            wawait=$(echo "$line" | awk '{print $12}')
            util=$(echo "$line" | awk '{print $NF}')
            # util > 80% 标红
            util_num=${util%.*}
            if [[ "$util_num" =~ ^[0-9]+$ ]] && [[ "$util_num" -ge 80 ]]; then
              echo "<tr><td>${dev}</td><td>${rs}</td><td>${ws}</td><td>${rmb}</td><td>${wmb}</td><td>${await}</td><td>${rawait}</td><td>${wawait}</td><td><font color=\"red\"><b>${util}%</b></font></td></tr>"
            else
              echo "<tr><td>${dev}</td><td>${rs}</td><td>${ws}</td><td>${rmb}</td><td>${wmb}</td><td>${await}</td><td>${rawait}</td><td>${wawait}</td><td>${util}%</td></tr>"
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
      echo "<p>iostat 未安装（需要 sysstat 包），跳过磁盘 IO 采集</p>"
    fi

    # 18.6 mysqld 进程资源 (Phase 6.4)
    echo "<h3>18.6 mysqld 进程资源</h3>"
    MYSQL_PID=$(pgrep -x mysqld 2>/dev/null || pgrep -x mariadbd 2>/dev/null || echo "")
    if [[ -n "$MYSQL_PID" ]]; then
      VmRSS=$(grep VmRSS /proc/$MYSQL_PID/status 2>/dev/null | awk '{printf "%.2f", $2/1024/1024}' || echo "N/A")
      VmSize=$(grep VmSize /proc/$MYSQL_PID/status 2>/dev/null | awk '{printf "%.2f", $2/1024/1024}' || echo "N/A")
      VmSwap=$(grep VmSwap /proc/$MYSQL_PID/status 2>/dev/null | awk '{printf "%.2f", $2/1024/1024}' || echo "N/A")
      ProcThreads=$(grep Threads /proc/$MYSQL_PID/status 2>/dev/null | awk '{print $2}' || echo "N/A")

      kv_table_start
      kv_table_row "PID" "$MYSQL_PID"
      kv_table_row "RSS (物理内存)" "${VmRSS} GB"
      kv_table_row "VSZ (虚拟内存)" "${VmSize} GB"
      kv_table_row "Swap 占用" "${VmSwap} GB"
      kv_table_row "线程数" "$ProcThreads"
      kv_table_end
    else
      echo "<p>未找到 mysqld/mariadbd 进程（可能运行在容器或远程）</p>"
    fi

    # 18.7 虚拟内存统计 (vmstat — Phase 6.8)
    echo "<h3>18.7 虚拟内存统计</h3>"
    if [[ -f /proc/vmstat ]]; then
      PGFAULT=$(grep "^pgfault " /proc/vmstat 2>/dev/null | awk '{print $2}' || echo "N/A")
      PGMAJFAULT=$(grep "^pgmajfault " /proc/vmstat 2>/dev/null | awk '{print $2}' || echo "N/A")
      PSWPIN=$(grep "^pswpin " /proc/vmstat 2>/dev/null | awk '{print $2}' || echo "N/A")
      PSWPOUT=$(grep "^pswpout " /proc/vmstat 2>/dev/null | awk '{print $2}' || echo "N/A")

      kv_table_start
      kv_table_row "页面错误(minor)" "$PGFAULT"
      kv_table_row "页面错误(major)" "$PGMAJFAULT"
      kv_table_row "Swap In (页)" "$PSWPIN"
      kv_table_row "Swap Out (页)" "$PSWPOUT"
      kv_table_end
    else
      echo "<p>/proc/vmstat 不可用</p>"
    fi

    # 18.8 网络接口统计 (Phase 6.9)
    echo "<h3>18.8 网络接口统计</h3>"
    if [[ -f /proc/net/dev ]]; then
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

    # 18.9 TCP 连接状态 (Phase 6.10)
    echo "<h3>18.9 TCP 连接状态</h3>"
    if command -v ss &>/dev/null; then
      kv_table_start
      # Count TCP states
      ESTABLISHED=$(ss -tan state established 2>/dev/null | tail -n +2 | wc -l || echo "0")
      TIME_WAIT=$(ss -tan state time-wait 2>/dev/null | tail -n +2 | wc -l || echo "0")
      CLOSE_WAIT=$(ss -tan state close-wait 2>/dev/null | tail -n +2 | wc -l || echo "0")
      LISTEN=$(ss -tln 2>/dev/null | tail -n +2 | wc -l || echo "0")
      SYN_RECV=$(ss -tan state syn-recv 2>/dev/null | tail -n +2 | wc -l || echo "0")
      TOTAL_TCP=$(ss -tan 2>/dev/null | tail -n +2 | wc -l || echo "0")
      kv_table_row "总 TCP 连接" "$TOTAL_TCP"
      kv_table_row "ESTABLISHED" "$ESTABLISHED"
      kv_table_row "TIME_WAIT" "$TIME_WAIT"
      kv_table_row "CLOSE_WAIT" "$CLOSE_WAIT"
      kv_table_row "LISTEN" "$LISTEN"
      kv_table_row "SYN_RECV" "$SYN_RECV"
      kv_table_end
    else
      echo "<p>ss 命令不可用</p>"
    fi
  else
    echo "<p>已跳过系统信息采集 (使用 --no-system 参数)</p>"
  fi

  section_end
  next_section "系统资源"

  # ================================================================
  # 19. 专家级调优指标 (v3.5 — MySQLTuner 对标)
  # Handler 统计：读取模式分析，评估索引利用率
  # ================================================================
  section_start "expert_tuning" "19. 专家级调优指标"

  echo "<h3>19.1 Handler 读取模式统计</h3>"
  safe_sql_html "
    SELECT
      v.Variable_name AS '指标',
      v.Variable_value AS '值'
    FROM (
      SELECT 'Handler_read_key' AS Variable_name,
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Handler_read_key') AS Variable_value
      UNION ALL SELECT 'Handler_read_rnd_next',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Handler_read_rnd_next')
      UNION ALL SELECT 'Handler_read_first',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Handler_read_first')
      UNION ALL SELECT 'Handler_read_rnd',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Handler_read_rnd')
      UNION ALL SELECT 'Handler_read_next',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Handler_read_next')
      UNION ALL SELECT 'Handler_read_prev',
             (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Handler_read_prev')
    ) v;
  "

  section_end
  next_section "专家级调优指标"

  # ================================================================
  # 页脚
  # ================================================================
  echo "<div class=\"footer\">"
  echo "  <p>本报告由 mysql_check.sh v${SCRIPT_VERSION} 生成 | 采集时间: ${COLLECT_TIME}</p>"
  echo "  <p>请将此文件上传到 <a href=\"https://ora100.com/oracheck-report\">ora100.com</a> 生成专业 Word 巡检报告</p>"
  echo "</div>"
  echo "</body></html>"

} > "$OUTPUT_FILE"

ok "采集完成！"
echo ""
echo "=================================================="
echo "  实例: ${INSTANCE_NAME} (${MYSQL_HOST}:${MYSQL_PORT})"
echo "  版本: MySQL ${MYSQL_VERSION}"
echo "  输出: ${OUTPUT_FILE}"
echo ""
echo "  请将文件上传到 ora100.com 生成专业巡检报告"
echo "=================================================="
