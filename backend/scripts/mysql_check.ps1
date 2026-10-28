<#
.SYNOPSIS
    MySQL 数据库巡检采集脚本 — 生成 HTML 格式的巡检数据文件

.DESCRIPTION
    在 Windows 服务器上运行，自动采集 MySQL 版本、连接、内存、日志、InnoDB、性能、
    复制、锁、备份等信息，输出结构化 HTML 文件供 ora100.com 巡检报告平台解析。

.PARAMETER Host
    MySQL 主机地址（默认 localhost）

.PARAMETER Port
    MySQL 端口（默认 3306）

.PARAMETER User
    MySQL 用户名（默认 root）

.PARAMETER Password
    MySQL 密码（留空则交互输入）

.PARAMETER InstanceName
    实例名称（默认 MySQL_Server）

.PARAMETER Output
    输出文件路径（默认当前目录自动命名）

.PARAMETER MySQLPath
    mysql.exe 路径（默认从 PATH 自动查找）

.EXAMPLE
    .\mysql_check.ps1 -User root -Password "xxx"

.EXAMPLE
    .\mysql_check.ps1 -Host 192.168.1.100 -User dba -Password "xxx" -InstanceName "生产库"
#>

param(
    [string]$Host_ = "localhost",
    [int]$Port = 3306,
    [string]$User = "root",
    [string]$Password = "",
    [string]$InstanceName = "MySQL_Server",
    [string]$Output = "",
    [string]$MySQLPath = ""
)

$ErrorActionPreference = "Continue"
$ScriptVersion = "1.0"
$CollectTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# ── 查找 mysql.exe ──────────────────────────────────────────────
function Find-MySql {
    if ($MySQLPath -and (Test-Path $MySQLPath)) { return $MySQLPath }
    $found = Get-Command mysql.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $paths = @(
        "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe",
        "C:\Program Files\MySQL\MySQL Server 8.4\bin\mysql.exe",
        "C:\Program Files\MySQL\MySQL Server 5.7\bin\mysql.exe",
        "C:\ProgramData\MySQL\MySQL Server 8.0\bin\mysql.exe",
        "C:\mysql\bin\mysql.exe"
    )
    foreach ($p in $paths) { if (Test-Path $p) { return $p } }
    Write-Host "  [ERROR] 未找到 mysql.exe，请通过 -MySQLPath 参数指定" -ForegroundColor Red
    exit 1
}

$MySqlExe = Find-MySql
Write-Host "  mysql.exe: $MySqlExe" -ForegroundColor Gray

# ── 输出文件 ────────────────────────────────────────────────────
if (-not $Output) {
    $ts = Get-Date -Format "yyyyMMdd_HHmmss"
    $Output = Join-Path (Get-Location) "mysql_check_${InstanceName}_${ts}.html"
}

# ── 密码处理 ────────────────────────────────────────────────────
if (-not $Password) {
    $sec = Read-Host "  MySQL 密码" -AsSecureString
    $Password = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}
$env:MYSQL_PWD = $Password
Register-EngineEvent PowerShell.Exiting -Action { Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue }

# ── 连接测试 ────────────────────────────────────────────────────
Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host " MySQL 巡检采集脚本 v$ScriptVersion" -ForegroundColor Cyan
Write-Host " 目标: ${Host_}:${Port}" -ForegroundColor Cyan
Write-Host " 输出: $Output" -ForegroundColor Cyan
Write-Host "============================================`n" -ForegroundColor Cyan

function Invoke-MySqlQuery {
    param([string]$Query, [int]$Timeout = 30)
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $MySqlExe
        $psi.Arguments = "-h$Host_ -P$Port -u$User --connect-timeout=10 --default-character-set=utf8mb4 -N -s -e `"$Query`""
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $stdout = $p.StandardOutput.ReadToEnd()
        $stderr = $p.StandardError.ReadToEnd()
        $p.WaitForExit(($Timeout * 1000))
        if ($p.ExitCode -ne 0 -and $stderr) {
            Write-Host "  [WARN] $($stderr.Trim())" -ForegroundColor Yellow
        }
        return $stdout.Trim()
    } catch {
        Write-Host "  [WARN] Query failed: $($_.Exception.Message)" -ForegroundColor Yellow
        return ""
    }
}

function Invoke-MySqlQueryHtml {
    param([string]$Query, [int]$Timeout = 30)
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $MySqlExe
        $psi.Arguments = "-h$Host_ -P$Port -u$User --connect-timeout=10 --default-character-set=utf8mb4 --html -e `"$Query`""
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $stdout = $p.StandardOutput.ReadToEnd()
        $p.WaitForExit(($Timeout * 1000))
        return $stdout.Trim()
    } catch {
        return ""
    }
}

function Safe-MySqlHtmlTable {
    param([string]$Query, [string]$Fallback = "无法获取数据")
    $result = Invoke-MySqlQueryHtml -Query $Query
    if ($result -and $result -match "<TABLE") { return $result }
    return "<p>$Fallback</p>"
}

function ConvertTo-KvHtmlTable {
    param([hashtable]$Data)
    if (-not $Data -or $Data.Count -eq 0) { return "<p>无数据</p>" }
    $html = "<table><tr><th>项目</th><th>值</th></tr>"
    foreach ($key in $Data.Keys) {
        $v = if ($Data[$key]) { [System.Web.HttpUtility]::HtmlEncode([string]$Data[$key]) } else { "" }
        $html += "<tr><td>$([System.Web.HttpUtility]::HtmlEncode($key))</td><td>$v</td></tr>"
    }
    $html += "</table>"
    return $html
}

Add-Type -AssemblyName System.Web

# 测试连接
Write-Host "[INFO] 测试 MySQL 连接..." -ForegroundColor Cyan
$MySqlVersion = Invoke-MySqlQuery -Query "SELECT VERSION()"
if (-not $MySqlVersion -or $MySqlVersion -match "ERROR|Connect") {
    Write-Host "[ERROR] MySQL 连接失败: $MySqlVersion" -ForegroundColor Red
    Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "[OK] 连接成功! MySQL $MySqlVersion`n" -ForegroundColor Green

$IsMySql8 = $false
if ($MySqlVersion -match "^(\d+)") {
    if ([int]$Matches[1] -ge 8) { $IsMySql8 = $true }
}

# ── HTML 构建 ────────────────────────────────────────────────────
$htmlParts = @()

$htmlParts += @"
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="generator" content="mysql_check.ps1">
<meta name="version" content="$ScriptVersion">
<meta name="db-type" content="mysql">
<meta name="collect-time" content="$CollectTime">
<meta name="instance-name" content="$InstanceName">
<meta name="instance-host" content="$Host_">
<meta name="instance-port" content="$Port">
<meta name="mysql-version" content="$MySqlVersion">
<title>MySQL 数据库巡检报告</title>
<style>
body { font-family: "Microsoft YaHei","PingFang SC",Arial,sans-serif; margin:20px; background:#f5f5f5; color:#333; }
h1 { color:#1565C0; border-bottom:2px solid #1565C0; padding-bottom:8px; }
h2 { color:#1976D2; margin-top:30px; border-left:4px solid #1976D2; padding-left:10px; }
h3 { color:#1E88E5; margin-top:15px; }
table { border-collapse:collapse; width:100%; margin:10px 0 20px 0; background:#fff; }
th { background:#1565C0; color:#fff; padding:8px 12px; text-align:left; font-size:14px; }
td { border:1px solid #ddd; padding:6px 12px; font-size:13px; }
tr:nth-child(even) { background:#f9f9f9; }
.section { margin:20px 0; padding:15px; background:#fff; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,.1); }
.warn { color:#e65100; font-weight:bold; }
.critical { color:#c62828; font-weight:bold; }
.ok { color:#2e7d32; }
.footer { text-align:center; color:#999; font-size:12px; margin-top:40px; padding:20px; border-top:1px solid #eee; }
</style>
</head>
<body>
<h1>MySQL 数据库巡检报告</h1>
<p>实例: $InstanceName | 地址: ${Host_}:${Port} | 版本: $MySqlVersion | 采集时间: $CollectTime</p>
"@

$SECTION_TOTAL = 20
$SECTION_NUM = 0

function Next-Section([string]$Name) {
    $script:SECTION_NUM++
    Write-Host "  [$($script:SECTION_NUM)/$SECTION_TOTAL] $Name ✓" -ForegroundColor Green
}

# ── 1. 基础信息 ────────────────────────────────────────────────
Write-Host "[1/$SECTION_TOTAL] 基础信息..." -ForegroundColor Green
$htmlParts += '<div class="section" id="basic_info"><h2>1. 基础信息</h2>'

$uptime = Invoke-MySqlQuery "SELECT DATE_FORMAT(DATE_SUB(NOW(), INTERVAL (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE variable_name='Uptime') SECOND),'%Y-%m-%d %H:%i:%s')"
$hostname = Invoke-MySqlQuery "SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE variable_name='hostname'"
$charDb = Invoke-MySqlQuery "SHOW VARIABLES LIKE 'character_set_database'" | ForEach-Object { ($_ -split "`t")[1] }
$charSvr = Invoke-MySqlQuery "SHOW VARIABLES LIKE 'character_set_server'" | ForEach-Object { ($_ -split "`t")[1] }
$collation = Invoke-MySqlQuery "SHOW VARIABLES LIKE 'collation_server'" | ForEach-Object { ($_ -split "`t")[1] }
$basedir = Invoke-MySqlQuery "SHOW VARIABLES LIKE 'basedir'" | ForEach-Object { ($_ -split "`t")[1] }
$datadir = Invoke-MySqlQuery "SHOW VARIABLES LIKE 'datadir'" | ForEach-Object { ($_ -split "`t")[1] }

$htmlParts += ConvertTo-KvHtmlTable @{
    "实例名称" = $InstanceName
    "服务器地址" = "${Host_}:${Port}"
    "MySQL 版本" = $MySqlVersion
    "服务器主机名" = $hostname
    "实例启动时间" = $uptime
    "数据库字符集" = $charDb
    "服务器字符集" = $charSvr
    "排序规则" = $collation
    "安装目录" = $basedir
    "数据目录" = $datadir
    "采集时间" = $CollectTime
}
$htmlParts += "</div>"
Next-Section "基础信息"

# ── 2. 连接配置 ────────────────────────────────────────────────
Write-Host "[2/$SECTION_TOTAL] 连接配置..." -ForegroundColor Green
$htmlParts += '<div class="section" id="connection_config"><h2>2. 连接配置</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT v.Variable_name AS '参数', v.Variable_value AS '值' FROM (SELECT 'max_connections' AS Variable_name, (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='max_connections') AS Variable_value UNION ALL SELECT 'Threads_connected', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Threads_connected') UNION ALL SELECT 'Threads_running', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Threads_running') UNION ALL SELECT 'Max_used_connections', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Max_used_connections') UNION ALL SELECT 'interactive_timeout', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='interactive_timeout') UNION ALL SELECT 'wait_timeout', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='wait_timeout') UNION ALL SELECT 'back_log', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='back_log') UNION ALL SELECT 'max_allowed_packet', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='max_allowed_packet') UNION ALL SELECT 'skip_name_resolve', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='skip_name_resolve') UNION ALL SELECT 'open_files_limit', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='open_files_limit')) v;"
$htmlParts += "</div>"
Next-Section "连接配置"

# ── 3. 内存配置 ────────────────────────────────────────────────
Write-Host "[3/$SECTION_TOTAL] 内存配置..." -ForegroundColor Green
$htmlParts += '<div class="section" id="memory_config"><h2>3. 内存配置</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT v.Variable_name AS '参数', v.Variable_value AS '值' FROM (SELECT 'innodb_buffer_pool_size' AS Variable_name, (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_buffer_pool_size') AS Variable_value UNION ALL SELECT 'innodb_buffer_pool_instances', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_buffer_pool_instances') UNION ALL SELECT 'sort_buffer_size', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='sort_buffer_size') UNION ALL SELECT 'join_buffer_size', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='join_buffer_size') UNION ALL SELECT 'thread_cache_size', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='thread_cache_size') UNION ALL SELECT 'tmp_table_size', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='tmp_table_size') UNION ALL SELECT 'max_heap_table_size', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='max_heap_table_size') UNION ALL SELECT 'read_buffer_size', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='read_buffer_size') UNION ALL SELECT 'read_rnd_buffer_size', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='read_rnd_buffer_size') UNION ALL SELECT 'innodb_io_capacity', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_io_capacity') UNION ALL SELECT 'thread_stack', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='thread_stack') UNION ALL SELECT 'key_buffer_size', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='key_buffer_size') UNION ALL SELECT 'table_open_cache', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='table_open_cache') UNION ALL SELECT 'table_definition_cache', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='table_definition_cache')) v;"
$htmlParts += "</div>"
Next-Section "内存配置"

# ── 4. 日志配置 ────────────────────────────────────────────────
Write-Host "[4/$SECTION_TOTAL] 日志配置..." -ForegroundColor Green
$htmlParts += '<div class="section" id="log_config"><h2>4. 日志配置</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT v.Variable_name AS '参数', v.Variable_value AS '值' FROM (SELECT 'slow_query_log' AS Variable_name, (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='slow_query_log') AS Variable_value UNION ALL SELECT 'slow_query_log_file', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='slow_query_log_file') UNION ALL SELECT 'long_query_time', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='long_query_time') UNION ALL SELECT 'log_bin', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='log_bin') UNION ALL SELECT 'binlog_format', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='binlog_format') UNION ALL SELECT 'expire_logs_days', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='expire_logs_days') UNION ALL SELECT 'binlog_expire_logs_seconds', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='binlog_expire_logs_seconds') UNION ALL SELECT 'innodb_log_file_size', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_log_file_size') UNION ALL SELECT 'innodb_log_buffer_size', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_log_buffer_size') UNION ALL SELECT 'innodb_flush_log_at_trx_commit', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_flush_log_at_trx_commit') UNION ALL SELECT 'sync_binlog', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='sync_binlog')) v;"
$htmlParts += "</div>"
Next-Section "日志配置"

# ── 5. InnoDB 配置 ────────────────────────────────────────────
Write-Host "[5/$SECTION_TOTAL] InnoDB 配置..." -ForegroundColor Green
$htmlParts += '<div class="section" id="innodb_config"><h2>5. InnoDB 配置</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT v.Variable_name AS '参数', v.Variable_value AS '值' FROM (SELECT 'innodb_file_per_table' AS Variable_name, (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_file_per_table') AS Variable_value UNION ALL SELECT 'innodb_open_files', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_open_files') UNION ALL SELECT 'innodb_thread_concurrency', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_thread_concurrency') UNION ALL SELECT 'innodb_flush_method', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_flush_method') UNION ALL SELECT 'innodb_doublewrite', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_doublewrite') UNION ALL SELECT 'innodb_read_io_threads', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_read_io_threads') UNION ALL SELECT 'innodb_write_io_threads', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_write_io_threads') UNION ALL SELECT 'innodb_checksum_algorithm', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_checksum_algorithm') UNION ALL SELECT 'innodb_page_size', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='innodb_page_size')) v;"
$htmlParts += "</div>"
Next-Section "InnoDB 配置"

# ── 6. 性能指标 ────────────────────────────────────────────────
Write-Host "[6/$SECTION_TOTAL] 性能指标..." -ForegroundColor Green
$htmlParts += '<div class="section" id="performance"><h2>6. 性能指标</h2>'
$htmlParts += '<h3>6.1 查询统计</h3>'
$htmlParts += Safe-MySqlHtmlTable "SELECT v.Variable_name AS '指标', v.Variable_value AS '值' FROM (SELECT 'Queries' AS Variable_name, (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Queries') AS Variable_value UNION ALL SELECT 'Questions', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Questions') UNION ALL SELECT 'Slow_queries', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Slow_queries') UNION ALL SELECT 'Com_select', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_select') UNION ALL SELECT 'Com_insert', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_insert') UNION ALL SELECT 'Com_update', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_update') UNION ALL SELECT 'Com_delete', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_delete') UNION ALL SELECT 'Com_commit', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_commit') UNION ALL SELECT 'Com_rollback', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Com_rollback')) v;"

$htmlParts += '<h3>6.2 InnoDB 性能</h3>'
$htmlParts += Safe-MySqlHtmlTable "SELECT v.Variable_name AS '指标', v.Variable_value AS '值' FROM (SELECT 'Innodb_buffer_pool_read_requests' AS Variable_name, (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_read_requests') AS Variable_value UNION ALL SELECT 'Innodb_buffer_pool_reads', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_reads') UNION ALL SELECT 'Innodb_row_lock_waits', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_row_lock_waits') UNION ALL SELECT 'Innodb_row_lock_time_avg', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_row_lock_time_avg') UNION ALL SELECT 'Innodb_data_reads', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_data_reads') UNION ALL SELECT 'Innodb_data_writes', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_data_writes')) v;"

$htmlParts += '<h3>6.3 表缓存</h3>'
$htmlParts += Safe-MySqlHtmlTable "SELECT v.Variable_name AS '指标', v.Variable_value AS '值' FROM (SELECT 'Opened_tables' AS Variable_name, (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Opened_tables') AS Variable_value UNION ALL SELECT 'Open_tables', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Open_tables') UNION ALL SELECT 'Table_open_cache_hits', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Table_open_cache_hits') UNION ALL SELECT 'Table_open_cache_misses', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Table_open_cache_misses')) v;"
$htmlParts += "</div>"
Next-Section "性能指标"

# ── 7. Buffer Pool 命中率 ──────────────────────────────────────
Write-Host "[7/$SECTION_TOTAL] Buffer Pool 命中率..." -ForegroundColor Green
$htmlParts += '<div class="section" id="buffer_pool"><h2>7. Buffer Pool 命中率</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT 'Buffer Pool Read Requests' AS '指标', VARIABLE_VALUE AS '值' FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_read_requests' UNION ALL SELECT 'Buffer Pool Reads (disk)', VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_reads' UNION ALL SELECT 'Buffer Pool Hit Rate (%)', ROUND((1 - (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_reads') / GREATEST((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_read_requests'),1)) * 100, 2);"
$htmlParts += "</div>"
Next-Section "Buffer Pool 命中率"

# ── 8. Top 表 ─────────────────────────────────────────────────
Write-Host "[8/$SECTION_TOTAL] Top 表..." -ForegroundColor Green
$htmlParts += '<div class="section" id="top_tables"><h2>8. Top 20 大表</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT table_schema AS '库', table_name AS '表', table_rows AS '行数', ROUND(COALESCE(data_length,0)/1024/1024,2) AS '数据MB', ROUND(COALESCE(index_length,0)/1024/1024,2) AS '索引MB', ROUND(COALESCE(data_length+index_length,0)/1024/1024,2) AS '总MB', engine AS '引擎' FROM information_schema.TABLES WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','sys') ORDER BY (COALESCE(data_length,0)+COALESCE(index_length,0)) DESC LIMIT 20;"
$htmlParts += "</div>"
Next-Section "Top 表"

# ── 9. Processlist ─────────────────────────────────────────────
Write-Host "[9/$SECTION_TOTAL] Processlist..." -ForegroundColor Green
$htmlParts += '<div class="section" id="processlist"><h2>9. 活动连接 (Top 50)</h2>'
if ($IsMySql8) {
    $htmlParts += Safe-MySqlHtmlTable "SELECT id AS 'ID', user AS '用户', host AS '主机', db AS '库', command AS '命令', time AS '秒', state AS '状态', LEFT(info,200) AS 'SQL' FROM performance_schema.processlist WHERE command != 'Sleep' ORDER BY time DESC LIMIT 50;"
} else {
    $htmlParts += Safe-MySqlHtmlTable "SHOW FULL PROCESSLIST;"
}
$htmlParts += "</div>"
Next-Section "Processlist"

# ── 10. 长查询 ─────────────────────────────────────────────────
Write-Host "[10/$SECTION_TOTAL] 长查询..." -ForegroundColor Green
$htmlParts += '<div class="section" id="long_queries"><h2>10. 长查询 (>300s)</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT id AS 'ID', user AS '用户', host AS '主机', db AS '库', time AS '秒', state AS '状态', LEFT(info,300) AS 'SQL' FROM information_schema.PROCESSLIST WHERE command = 'Sleep' AND time > 300 ORDER BY time DESC LIMIT 20;"
$htmlParts += "</div>"
Next-Section "长查询"

# ── 11. 锁诊断 ─────────────────────────────────────────────────
Write-Host "[11/$SECTION_TOTAL] 锁诊断..." -ForegroundColor Green
$htmlParts += '<div class="section" id="locks"><h2>11. 锁诊断</h2>'
if ($IsMySql8) {
    $htmlParts += Safe-MySqlHtmlTable "SELECT r.trx_id AS '等待事务', r.trx_mysql_thread_id AS '线程', r.trx_query AS '等待SQL', b.trx_id AS '阻塞事务', b.trx_mysql_thread_id AS '阻塞线程', LEFT(b.trx_query,200) AS '阻塞SQL', TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS '等待秒' FROM information_schema.innodb_trx r JOIN information_schema.innodb_trx b ON r.trx_requested_lock_id = b.trx_requested_lock_id WHERE r.trx_state = 'LOCK WAIT' LIMIT 10;"
} else {
    $htmlParts += Safe-MySqlHtmlTable "SELECT * FROM information_schema.INNODB_TRX ORDER BY trx_started LIMIT 20;"
}
$htmlParts += "</div>"
Next-Section "锁诊断"

# ── 12. 复制状态 ───────────────────────────────────────────────
Write-Host "[12/$SECTION_TOTAL] 复制状态..." -ForegroundColor Green
$htmlParts += '<div class="section" id="replication"><h2>12. 复制状态</h2>'
if ($IsMySql8) {
    $htmlParts += Safe-MySqlHtmlTable "SHOW REPLICA STATUS\G" "非从库或无复制配置"
} else {
    $htmlParts += Safe-MySqlHtmlTable "SHOW SLAVE STATUS\G" "非从库或无复制配置"
}
$htmlParts += "</div>"
Next-Section "复制状态"

# ── 13. SSL/TLS ────────────────────────────────────────────────
Write-Host "[13/$SECTION_TOTAL] SSL/TLS..." -ForegroundColor Green
$htmlParts += '<div class="section" id="ssl"><h2>13. SSL/TLS 状态</h2>'
$htmlParts += Safe-MySqlHtmlTable "SHOW VARIABLES LIKE 'have_ssl'; SHOW VARIABLES LIKE 'ssl_ca'; SHOW VARIABLES LIKE 'ssl_cert'; SHOW VARIABLES LIKE 'ssl_key'; SHOW VARIABLES LIKE 'tls_version';"
$htmlParts += "</div>"
Next-Section "SSL/TLS"

# ── 14. Auto Increment ────────────────────────────────────────
Write-Host "[14/$SECTION_TOTAL] Auto Increment..." -ForegroundColor Green
$htmlParts += '<div class="section" id="auto_inc"><h2>14. Auto Increment 使用情况</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT table_schema AS '库', table_name AS '表', auto_increment AS '当前值', ROUND(auto_increment / POWER(2, CASE WHEN data_type='bigint' THEN 63 WHEN data_type='int' THEN 31 WHEN data_type='smallint' THEN 15 ELSE 31 END) * 100, 4) AS '使用率%' FROM information_schema.TABLES WHERE auto_increment IS NOT NULL AND table_schema NOT IN ('information_schema','performance_schema','mysql','sys') ORDER BY auto_increment DESC LIMIT 20;"
$htmlParts += "</div>"
Next-Section "Auto Increment"

# ── 15. 密码策略 ───────────────────────────────────────────────
Write-Host "[15/$SECTION_TOTAL] 密码策略..." -ForegroundColor Green
$htmlParts += '<div class="section" id="password_policy"><h2>15. 密码策略</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT VARIABLE_NAME AS '参数', VARIABLE_VALUE AS '值' FROM performance_schema.global_variables WHERE VARIABLE_NAME IN ('validate_password.policy','validate_password.length','validate_password.mixed_case_count','validate_password.number_count','validate_password.special_char_count','validate_password.check_user_name');"
$htmlParts += "</div>"
Next-Section "密码策略"

# ── 16. 用户列表 ───────────────────────────────────────────────
Write-Host "[16/$SECTION_TOTAL] 用户列表..." -ForegroundColor Green
$htmlParts += '<div class="section" id="users"><h2>16. MySQL 用户</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT user AS '用户', host AS '主机', plugin AS '认证插件', account_locked AS '锁定' FROM mysql.user ORDER BY user;"
$htmlParts += "</div>"
Next-Section "用户列表"

# ── 17. 备份配置 ───────────────────────────────────────────────
Write-Host "[17/$SECTION_TOTAL] 备份配置..." -ForegroundColor Green
$htmlParts += '<div class="section" id="backup_config"><h2>17. 备份相关配置</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT v.Variable_name AS '参数', v.Variable_value AS '值' FROM (SELECT 'log_bin' AS Variable_name, (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='log_bin') AS Variable_value UNION ALL SELECT 'binlog_format', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='binlog_format') UNION ALL SELECT 'log_slave_updates', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='log_slave_updates') UNION ALL SELECT 'gtid_mode', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='gtid_mode') UNION ALL SELECT 'server_id', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME='server_id')) v;"
$htmlParts += "</div>"
Next-Section "备份配置"

# ── 18. 系统资源 ───────────────────────────────────────────────
Write-Host "[18/$SECTION_TOTAL] 系统资源..." -ForegroundColor Green
$htmlParts += '<div class="section" id="system_resources"><h2>18. 系统资源</h2>'

# CPU
$htmlParts += '<h3>CPU</h3><table><tr><th>指标</th><th>值</th></tr>'
try {
    $cpu = Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average
    $htmlParts += "<tr><td>CPU 使用率</td><td>$($cpu.Average)%</td></tr>"
} catch { $htmlParts += "<tr><td>CPU 使用率</td><td>N/A</td></tr>" }
$htmlParts += "</table>"

# Memory
$htmlParts += '<h3>内存</h3><table><tr><th>指标</th><th>值(GB)</th></tr>'
try {
    $os = Get-CimInstance Win32_OperatingSystem
    $totalMem = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
    $freeMem = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
    $usedMem = [math]::Round($totalMem - $freeMem, 2)
    $pct = [math]::Round($usedMem / $totalMem * 100, 1)
    $htmlParts += "<tr><td>总内存</td><td>$totalMem</td></tr>"
    $htmlParts += "<tr><td>已使用</td><td>$usedMem ($pct%)</td></tr>"
    $htmlParts += "<tr><td>可用</td><td>$freeMem</td></tr>"
} catch { $htmlParts += "<tr><td colspan='2'>采集失败</td></tr>" }
$htmlParts += "</table>"

# Disk
$htmlParts += '<h3>磁盘</h3><table><tr><th>盘符</th><th>总大小(GB)</th><th>可用(GB)</th><th>使用率(%)</th></tr>'
try {
    $disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3"
    foreach ($d in $disks) {
        $totalGB = [math]::Round($d.Size / 1GB, 2)
        $freeGB = [math]::Round($d.FreeSpace / 1GB, 2)
        $usedPct = if ($d.Size -gt 0) { [math]::Round(($d.Size - $d.FreeSpace) / $d.Size * 100, 1) } else { 0 }
        $htmlParts += "<tr><td>$($d.DeviceID)</td><td>$totalGB</td><td>$freeGB</td><td>$usedPct</td></tr>"
    }
} catch { $htmlParts += "<tr><td colspan='4'>采集失败</td></tr>" }
$htmlParts += "</table>"
$htmlParts += "</div>"
Next-Section "系统资源"

# ── 19. 专家指标 ───────────────────────────────────────────────
Write-Host "[19/$SECTION_TOTAL] 专家指标..." -ForegroundColor Green
$htmlParts += '<div class="section" id="expert"><h2>19. 专家级调优指标</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT v.Variable_name AS '指标', v.Variable_value AS '值' FROM (SELECT 'Sort_merge_passes' AS Variable_name, (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Sort_merge_passes') AS Variable_value UNION ALL SELECT 'Sort_scan', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Sort_scan') UNION ALL SELECT 'Sort_range', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Sort_range') UNION ALL SELECT 'Sort_rows', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Sort_rows') UNION ALL SELECT 'Select_full_join', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Select_full_join') UNION ALL SELECT 'Select_full_range_join', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Select_full_range_join') UNION ALL SELECT 'Select_scan', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Select_scan') UNION ALL SELECT 'Select_range', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Select_range') UNION ALL SELECT 'Created_tmp_disk_tables', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Created_tmp_disk_tables') UNION ALL SELECT 'Created_tmp_tables', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Created_tmp_tables') UNION ALL SELECT 'Table_locks_waited', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Table_locks_waited') UNION ALL SELECT 'Innodb_log_waits', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_log_waits') UNION ALL SELECT 'Innodb_buffer_pool_wait_free', (SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_wait_free')) v;"
$htmlParts += "</div>"
Next-Section "专家指标"

# ── 20. 平台信息 ───────────────────────────────────────────────
Write-Host "[20/$SECTION_TOTAL] 平台信息..." -ForegroundColor Green
$htmlParts += '<div class="section" id="platform"><h2>20. 平台信息</h2>'
$htmlParts += Safe-MySqlHtmlTable "SELECT variable_name AS '项目', variable_value AS '值' FROM performance_schema.global_variables WHERE variable_name IN ('version_compile_os','version_compile_machine','hostname','version_compile_zlib','version_compile_bison','version_compile_cmake','version_compile_ncurses');"
$htmlParts += "</div>"
Next-Section "平台信息"

# ── Footer ─────────────────────────────────────────────────────
$htmlParts += @"

<div class="footer">
  <p>MySQL 巡检采集脚本 v$ScriptVersion | 采集时间: $CollectTime</p>
  <p>上传到 <a href="https://ora100.com">ora100.com</a> 即可生成专业 Word 巡检报告</p>
</div>
</body>
</html>
"@

# ── 写入文件 ────────────────────────────────────────────────────
$htmlParts -join "`n" | Out-File -FilePath $Output -Encoding UTF8
Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue

Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host " 采集完成!" -ForegroundColor Green
Write-Host " 输出文件: $Output" -ForegroundColor Cyan
Write-Host " 上传到 ora100.com 即可生成专业报告" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
