<#
.SYNOPSIS
    PostgreSQL 数据库巡检采集脚本 — 生成 HTML 格式的巡检数据文件

.DESCRIPTION
    在 Windows 服务器上运行，通过 psql.exe 采集 PostgreSQL 版本、连接、配置、
    性能、复制、锁等信息，输出结构化 HTML 文件供 ora100.com 巡检报告平台解析。

.PARAMETER Host
    PostgreSQL 主机地址（默认 localhost）

.PARAMETER Port
    端口号（默认 5432）

.PARAMETER User
    用户名（默认 postgres）

.PARAMETER Database
    数据库名（默认 postgres）

.PARAMETER InstanceName
    实例名称（默认 PG_Server）

.PARAMETER Output
    输出文件路径（默认 pg_check_<InstanceName>_<timestamp>.html）

.PARAMETER PSQLPath
    psql.exe 完整路径（默认从 PATH 查找）

.PARAMETER Password
    密码（留空则依赖 PGPASSWORD 环境变量或 ~/.pgpass）

.EXAMPLE
    .\pg_check.ps1

    .\pg_check.ps1 -Host "192.168.1.100" -Port 5432 -User "postgres" -Database "mydb"

    .\pg_check.ps1 -Host "db.example.com" -Password "secret" -InstanceName "PG_Prod"
#>

param(
    [string]$Host = "localhost",
    [int]$Port = 5432,
    [string]$User = "postgres",
    [string]$Database = "postgres",
    [string]$InstanceName = "PG_Server",
    [string]$Output = "",
    [string]$PSQLPath = "",
    [string]$Password = ""
)

$ErrorActionPreference = "Continue"
$ScriptVersion = "1.0"
$CollectTime = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$SectionTotal = 13
$SectionCurrent = 0

# --- Locate psql.exe ---
if (-not $PSQLPath) {
    $PSQLPath = (Get-Command psql.exe -ErrorAction SilentlyContinue).Source
}
if (-not $PSQLPath) {
    # Try common PostgreSQL install paths
    $pgPaths = @(
        "C:\Program Files\PostgreSQL\*\bin\psql.exe",
        "C:\Program Files (x86)\PostgreSQL\*\bin\psql.exe"
    )
    foreach ($pattern in $pgPaths) {
        $found = Get-Item $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { $PSQLPath = $found.FullName; break }
    }
}
if (-not $PSQLPath) {
    Write-Host "ERROR: psql.exe not found. Install PostgreSQL client or specify -PSQLPath." -ForegroundColor Red
    exit 1
}

# --- Default output path ---
if (-not $Output) {
    $safeInstance = $InstanceName -replace '[:\\\/]', '_'
    $Output = Join-Path (Get-Location) "pg_check_${safeInstance}_${Timestamp}.html"
}

# --- Set PGPASSWORD if provided ---
$PasswordSet = $false
if ($Password) {
    $env:PGPASSWORD = $Password
    $PasswordSet = $true
}

# --- Cleanup on exit ---
$script:PGPasswordWasSet = $PasswordSet
Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    if ($script:PGPasswordWasSet) { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
} -ErrorAction SilentlyContinue

# ============================================================================
# Helper: Execute psql query and return trimmed output
# ============================================================================
function Invoke-PgQuery {
    param(
        [string]$Query,
        [string]$PSQLExe = $PSQLPath,
        [string]$PgHost = $Host,
        [int]$PgPort = $Port,
        [string]$PgUser = $User,
        [string]$PgDatabase = $Database,
        [int]$Timeout = 30
    )
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $PSQLExe
        $psi.Arguments = "-h $PgHost -p $PgPort -U $PgUser -d $PgDatabase -t -A -c `"$Query`""
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8

        $proc = [System.Diagnostics.Process]::Start($psi)
        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        $proc.WaitForExit([Math]::Min($Timeout * 1000, 60000))

        if ($proc.HasExited -eq $false) {
            $proc.Kill()
            Write-Host "  [WARN] Query timed out after ${Timeout}s" -ForegroundColor Yellow
            return $null
        }

        if ($proc.ExitCode -ne 0 -and [string]::IsNullOrWhiteSpace($stdout)) {
            Write-Host "  [WARN] psql error: $($stderr.Trim())" -ForegroundColor Yellow
            return $null
        }

        return $stdout.Trim()
    } catch {
        Write-Host "  [WARN] Query failed: $($_.Exception.Message)" -ForegroundColor Yellow
        return $null
    }
}

# ============================================================================
# Helper: Execute psql query and return raw rows (pipe-delimited for parsing)
# ============================================================================
function Invoke-PgQueryRows {
    param(
        [string]$Query,
        [string]$Separator = "|",
        [int]$Timeout = 30
    )
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $PSQLPath
        $psi.Arguments = "-h $Host -p $Port -U $User -d $Database -t -A --field-separator=`"$Separator`" -c `"$Query`""
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8

        $proc = [System.Diagnostics.Process]::Start($psi)
        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        $proc.WaitForExit([Math]::Min($Timeout * 1000, 60000))

        if ($proc.HasExited -eq $false) {
            $proc.Kill()
            return $null
        }

        if ($proc.ExitCode -ne 0 -and [string]::IsNullOrWhiteSpace($stdout)) {
            return $null
        }

        $lines = $stdout -split "`n" | Where-Object { $_.Trim() -ne "" }
        return $lines
    } catch {
        Write-Host "  [WARN] Query failed: $($_.Exception.Message)" -ForegroundColor Yellow
        return $null
    }
}

# ============================================================================
# Helper: Build HTML key-value table
# ============================================================================
function ConvertTo-KvHtmlTable {
    param([hashtable]$Data)
    if (-not $Data -or $Data.Count -eq 0) { return "<p>无数据</p>" }
    $html = "<table><tr><th>项目</th><th>值</th></tr>"
    foreach ($key in $Data.Keys) {
        $k = [System.Web.HttpUtility]::HtmlEncode([string]$key)
        $raw = $Data[$key]
        $val = if ($null -ne $raw) {
            [System.Web.HttpUtility]::HtmlEncode([string]$raw)
        } else { "" }
        $html += "<tr><td>$k</td><td>$val</td></tr>"
    }
    $html += "</table>"
    return $html
}

# ============================================================================
# Helper: Build HTML data table from psql output rows + headers
# ============================================================================
function ConvertTo-HtmlTableFromRows {
    param(
        [string[]]$Headers,
        [string[]]$Rows,
        [string]$Separator = "|",
        [string]$EmptyMessage = "无数据"
    )
    if (-not $Rows -or $Rows.Count -eq 0) {
        return "<p>$EmptyMessage</p>"
    }
    $html = "<table><tr>"
    foreach ($h in $Headers) {
        $html += "<th>$([System.Web.HttpUtility]::HtmlEncode($h))</th>"
    }
    $html += "</tr>"
    foreach ($line in $Rows) {
        $parts = $line.Split($Separator)
        $html += "<tr>"
        for ($i = 0; $i -lt $Headers.Count; $i++) {
            $val = if ($i -lt $parts.Count) { $parts[$i].Trim() } else { "" }
            $val = [System.Web.HttpUtility]::HtmlEncode($val)
            $html += "<td>$val</td>"
        }
        $html += "</tr>"
    }
    $html += "</table>"
    return $html
}

# ============================================================================
# Helper: Safe query with fallback message
# ============================================================================
function Invoke-PgSafeHtmlTable {
    param(
        [string]$Query,
        [string[]]$Headers,
        [string]$EmptyMessage = "无数据",
        [string]$Separator = "|"
    )
    $rows = Invoke-PgQueryRows -Query $Query -Separator $Separator
    if (-not $rows -or $rows.Count -eq 0) {
        return "<p>$EmptyMessage</p>"
    }
    return ConvertTo-HtmlTableFromRows -Headers $Headers -Rows $rows -EmptyMessage $EmptyMessage -Separator $Separator
}

# ============================================================================
# Helper: Next section progress
# ============================================================================
function Write-NextSection {
    param([string]$Name)
    $script:SectionCurrent++
    Write-Host "[$script:SectionCurrent/$SectionTotal] $Name" -ForegroundColor Blue
}

# Load System.Web for HtmlEncode
Add-Type -AssemblyName System.Web

# ============================================================================
# Banner
# ============================================================================
Write-Host "============================================" -ForegroundColor Cyan
Write-Host " PostgreSQL 巡检采集脚本 v$ScriptVersion" -ForegroundColor Cyan
Write-Host " 目标: ${Host}:${Port}/${Database}" -ForegroundColor Cyan
Write-Host " 用户: $User" -ForegroundColor Cyan
Write-Host " 输出: $Output" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# ============================================================================
# Connection test
# ============================================================================
Write-Host "`n测试连接..." -ForegroundColor Cyan -NoNewline
$pgVersion = Invoke-PgQuery -Query "SELECT version();"
if (-not $pgVersion) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host "无法连接到 PostgreSQL，请检查连接参数和认证配置。" -ForegroundColor Red
    if ($PasswordSet) { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
    exit 1
}

# Parse PG version short
$pgVersionShort = "unknown"
if ($pgVersion -match 'PostgreSQL\s+([\d]+\.[\d]+)') {
    $pgVersionShort = $Matches[1]
}
$pgVersionMajor = 0
if ($pgVersionShort -match '^(\d+)') {
    $pgVersionMajor = [int]$Matches[1]
}

Write-Host " 成功" -ForegroundColor Green -NoNewline
Write-Host " (PostgreSQL $pgVersionShort)" -ForegroundColor Green
Write-Host ""

# ============================================================================
# Section 1: 基本信息
# ============================================================================
Write-NextSection "基本信息"

$pgStartup = Invoke-PgQuery -Query "SELECT pg_postmaster_start_time()::text;"
$pgUptime = Invoke-PgQuery -Query "SELECT (now() - pg_postmaster_start_time())::text;"
$pgDatadir = Invoke-PgQuery -Query "SHOW data_directory;"
$pgConfigFile = Invoke-PgQuery -Query "SHOW config_file;"
$pgIsRecovery = Invoke-PgQuery -Query "SELECT pg_is_in_recovery()::text;"
$pgClusterName = Invoke-PgQuery -Query "SHOW cluster_name;"
$pgServerEncoding = Invoke-PgQuery -Query "SHOW server_encoding;"
$pgMaxConn = Invoke-PgQuery -Query "SHOW max_connections;"
$pgSuperuserReserved = Invoke-PgQuery -Query "SHOW superuser_reserved_connections;"

$section1Html = ConvertTo-KvHtmlTable @{
    "实例名称" = $InstanceName
    "服务器地址" = "${Host}:${Port}"
    "数据库名" = $Database
    "采集时间" = $CollectTime
    "PostgreSQL 版本" = $pgVersionShort
    "完整版本" = $pgVersion
    "启动时间" = $pgStartup
    "运行时长" = $pgUptime
    "数据目录" = $pgDatadir
    "配置文件" = $pgConfigFile
    "服务器编码" = $pgServerEncoding
    "恢复模式" = $pgIsRecovery
    "集群名称" = $pgClusterName
}

# ============================================================================
# Section 2: 连接配置
# ============================================================================
Write-NextSection "连接配置"

$pgCurrentConn = Invoke-PgQuery -Query "SELECT count(*) FROM pg_stat_activity;"
$connPct = "N/A"
if ($pgMaxConn -and [int]$pgMaxConn -gt 0) {
    $connPct = [math]::Round([int]$pgCurrentConn * 100 / [int]$pgMaxConn, 1)
}

$section2KvHtml = ConvertTo-KvHtmlTable @{
    "最大连接数 (max_connections)" = $pgMaxConn
    "超级用户预留 (superuser_reserved_connections)" = $pgSuperuserReserved
    "当前连接数" = $pgCurrentConn
    "连接使用率" = "${connPct}%"
}

$section2StateHtml = Invoke-PgSafeHtmlTable -Query @"
SELECT state AS "状态",
       count(*) AS "数量",
       round(100.0 * count(*) / greatest(sum(count(*)) over(), 1), 1) AS "占比(%)"
FROM pg_stat_activity
WHERE pid <> pg_backend_pid()
GROUP BY state
ORDER BY count(*) DESC;
"@ -Headers @("状态", "数量", "占比(%)") -EmptyMessage "无法获取连接状态"

$section2UserHtml = Invoke-PgSafeHtmlTable -Query @"
SELECT usename AS "用户",
       count(*) AS "连接数",
       count(*) FILTER (WHERE state = 'active') AS "活跃",
       count(*) FILTER (WHERE state = 'idle') AS "空闲"
FROM pg_stat_activity
WHERE pid <> pg_backend_pid()
GROUP BY usename
ORDER BY count(*) DESC
LIMIT 15;
"@ -Headers @("用户", "连接数", "活跃", "空闲") -EmptyMessage "无法获取用户连接分布"

$section2Html = $section2KvHtml + "<h3>连接状态分布</h3>" + $section2StateHtml + "<h3>按用户连接分布</h3>" + $section2UserHtml

# ============================================================================
# Section 3: 内存配置
# ============================================================================
Write-NextSection "内存配置"

$memSettings = @(
    'shared_buffers', 'work_mem', 'maintenance_work_mem', 'effective_cache_size',
    'huge_pages', 'shared_preload_libraries', 'max_worker_processes',
    'max_parallel_workers_per_gather', 'max_parallel_workers',
    'effective_io_concurrency', 'random_page_cost'
)
$memSql = 'SELECT name AS "参数", setting AS "当前值", unit AS "单位", source AS "来源", short_desc AS "说明" FROM pg_settings WHERE name IN (''' + ($memSettings -join "','") + ''') ORDER BY name;'

$section3Html = Invoke-PgSafeHtmlTable -Query $memSql -Headers @("参数", "当前值", "单位", "来源", "说明") -EmptyMessage "无法获取内存配置"

# ============================================================================
# Section 4: WAL 配置
# ============================================================================
Write-NextSection "WAL 配置"

$walSettings = @(
    'wal_level', 'max_wal_size', 'min_wal_size', 'wal_buffers',
    'wal_sync_method', 'wal_compression', 'full_page_writes',
    'archive_mode', 'archive_command', 'max_wal_senders',
    'hot_standby', 'wal_receiver_timeout'
)
$walSql = 'SELECT name AS "参数", setting AS "当前值", unit AS "单位", source AS "来源", short_desc AS "说明" FROM pg_settings WHERE name IN (''' + ($walSettings -join "','") + ''') ORDER BY name;'

$section4Html = Invoke-PgSafeHtmlTable -Query $walSql -Headers @("参数", "当前值", "单位", "来源", "说明") -EmptyMessage "无法获取 WAL 配置"

# ============================================================================
# Section 5: 检查点配置
# ============================================================================
Write-NextSection "检查点配置"

$ckptSettings = @(
    'checkpoint_completion_target', 'checkpoint_warning', 'checkpoint_timeout',
    'checkpoint_flush_after', 'wal_writer_delay', 'wal_writer_flush_after',
    'commit_delay', 'commit_siblings'
)
$ckptSql = 'SELECT name AS "参数", setting AS "当前值", unit AS "单位", source AS "来源", short_desc AS "说明" FROM pg_settings WHERE name IN (''' + ($ckptSettings -join "','") + ''') ORDER BY name;'

$section5Html = Invoke-PgSafeHtmlTable -Query $ckptSql -Headers @("参数", "当前值", "单位", "来源", "说明") -EmptyMessage "无法获取检查点配置"

# BGWriter stats
if ($pgVersionMajor -ge 17) {
    $bgSql = @"
SELECT c.num_timed AS "定时检查点",
       c.num_requested AS "请求检查点",
       c.buffers_written AS "检查点写缓冲",
       b.buffers_clean AS "后台清理缓冲",
       b.buffers_alloc AS "分配缓冲"
FROM pg_stat_checkpointer c
CROSS JOIN pg_stat_bgwriter b;
"@
} else {
    $bgSql = @"
SELECT checkpoints_timed AS "定时检查点",
       checkpoints_req AS "请求检查点",
       buffers_checkpoint AS "检查点写缓冲",
       buffers_clean AS "后台清理缓冲",
       buffers_backend AS "后端直接写",
       buffers_alloc AS "分配缓冲"
FROM pg_stat_bgwriter;
"@
}
$section5BgHtml = Invoke-PgSafeHtmlTable -Query $bgSql -Headers @("定时检查点", "请求检查点", "检查点写缓冲", "后台清理缓冲", "后端直接写", "分配缓冲") -EmptyMessage "无法获取 BGWriter 统计"
$section5Html += "<h3>BGWriter 统计</h3>" + $section5BgHtml

# ============================================================================
# Section 6: 性能统计
# ============================================================================
Write-NextSection "性能统计"

# Database transaction stats
$perfTxSql = @"
SELECT datname AS "数据库",
       xact_commit AS "提交事务",
       xact_rollback AS "回滚事务",
       CASE WHEN xact_commit + xact_rollback > 0
            THEN round(100.0 * xact_rollback / (xact_commit + xact_rollback), 2)
            ELSE 0
       END AS "回滚率(%)",
       tup_returned AS "返回元组",
       tup_fetched AS "获取元组",
       tup_inserted AS "插入元组",
       tup_updated AS "更新元组",
       tup_deleted AS "删除元组",
       conflicts AS "冲突次数",
       deadlocks AS "死锁次数"
FROM pg_stat_database
WHERE datname NOT LIKE 'template%'
ORDER BY xact_commit + xact_rollback DESC;
"@

$section6Html = Invoke-PgSafeHtmlTable -Query $perfTxSql -Headers @("数据库", "提交事务", "回滚事务", "回滚率(%)", "返回元组", "获取元组", "插入元组", "更新元组", "删除元组", "冲突次数", "死锁次数") -EmptyMessage "无法获取性能统计"

# Cache hit ratio
$cacheHitSql = @"
SELECT datname AS "数据库",
       blks_read AS "物理读",
       blks_hit AS "缓存命中",
       CASE WHEN blks_read + blks_hit > 0
            THEN round(100.0 * blks_hit / (blks_read + blks_hit), 2)
            ELSE 0
       END AS "命中率(%)"
FROM pg_stat_database
WHERE datname NOT LIKE 'template%'
ORDER BY blks_read + blks_hit DESC;
"@
$section6CacheHtml = Invoke-PgSafeHtmlTable -Query $cacheHitSql -Headers @("数据库", "物理读", "缓存命中", "命中率(%)") -EmptyMessage "无法获取缓存命中率"
$section6Html += "<h3>缓存命中率</h3>" + $section6CacheHtml

# ============================================================================
# Section 7: 表统计 (Top 20)
# ============================================================================
Write-NextSection "表统计 (Top 20)"

$topTablesSql = @"
SELECT schemaname AS "模式",
       relname AS "表名",
       n_live_tup AS "活跃行数",
       n_dead_tup AS "死元组数",
       CASE WHEN n_live_tup + n_dead_tup > 0
            THEN round(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 1)
            ELSE 0
       END AS "死元组率(%)",
       to_char(last_vacuum, 'YYYY-MM-DD HH24:MI') AS "上次VACUUM",
       to_char(last_autovacuum, 'YYYY-MM-DD HH24:MI') AS "上次自动VACUUM",
       to_char(last_analyze, 'YYYY-MM-DD HH24:MI') AS "上次ANALYZE"
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC
LIMIT 20;
"@

$section7Html = Invoke-PgSafeHtmlTable -Query $topTablesSql -Headers @("模式", "表名", "活跃行数", "死元组数", "死元组率(%)", "上次VACUUM", "上次自动VACUUM", "上次ANALYZE") -EmptyMessage "无法获取表统计"

# Table sizes
$tableSizeSql = @"
SELECT schemaname AS "模式",
       relname AS "表名",
       pg_size_pretty(pg_total_relation_size(relid)) AS "总大小",
       pg_size_pretty(pg_relation_size(relid)) AS "数据大小",
       pg_size_pretty(pg_indexes_size(relid::regclass)) AS "索引大小"
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 20;
"@
$section7SizeHtml = Invoke-PgSafeHtmlTable -Query $tableSizeSql -Headers @("模式", "表名", "总大小", "数据大小", "索引大小") -EmptyMessage "无法获取表大小"
$section7Html += "<h3>表大小 (Top 20)</h3>" + $section7SizeHtml

# Index usage
$indexUsageSql = @"
SELECT schemaname AS "模式",
       relname AS "表名",
       idx_scan AS "索引扫描次数",
       seq_scan AS "顺序扫描次数",
       CASE WHEN idx_scan + seq_scan > 0
            THEN round(100.0 * idx_scan / (idx_scan + seq_scan), 1)
            ELSE 0
       END AS "索引扫描率(%)"
FROM pg_stat_user_tables
WHERE seq_scan + idx_scan > 100
ORDER BY seq_scan DESC
LIMIT 20;
"@
$section7IndexHtml = Invoke-PgSafeHtmlTable -Query $indexUsageSql -Headers @("模式", "表名", "索引扫描次数", "顺序扫描次数", "索引扫描率(%)") -EmptyMessage "无法获取索引扫描率"
$section7Html += "<h3>索引使用率</h3>" + $section7IndexHtml

# ============================================================================
# Section 8: 活动进程 (Processlist)
# ============================================================================
Write-NextSection "活动进程"

$processlistSql = @"
SELECT pid AS "PID",
       usename AS "用户",
       datname AS "数据库",
       application_name AS "应用",
       client_addr::text AS "客户端地址",
       state AS "状态",
       wait_event_type AS "等待类型",
       wait_event AS "等待事件",
       left(query, 120) AS "SQL",
       to_char(query_start, 'HH24:MI:SS') AS "查询开始",
       to_char(state_change, 'HH24:MI:SS') AS "状态变更"
FROM pg_stat_activity
WHERE pid <> pg_backend_pid()
ORDER BY state, pid
LIMIT 50;
"@

$section8Html = Invoke-PgSafeHtmlTable -Query $processlistSql -Headers @("PID", "用户", "数据库", "应用", "客户端地址", "状态", "等待类型", "等待事件", "SQL", "查询开始", "状态变更") -EmptyMessage "无法获取活动进程"

# ============================================================================
# Section 9: 长查询 (> 30s)
# ============================================================================
Write-NextSection "长查询 (>30s)"

$longQuerySql = @"
SELECT pid AS "PID",
       usename AS "用户",
       datname AS "数据库",
       state AS "状态",
       round(extract(epoch FROM (now() - query_start)))::int AS "运行秒数",
       wait_event_type AS "等待类型",
       wait_event AS "等待事件",
       left(query, 200) AS "SQL"
FROM pg_stat_activity
WHERE state <> 'idle'
  AND pid <> pg_backend_pid()
  AND query_start < now() - interval '30 seconds'
ORDER BY query_start ASC
LIMIT 50;
"@

$section9Html = Invoke-PgSafeHtmlTable -Query $longQuerySql -Headers @("PID", "用户", "数据库", "状态", "运行秒数", "等待类型", "等待事件", "SQL") -EmptyMessage "无长查询"

# ============================================================================
# Section 10: 锁信息
# ============================================================================
Write-NextSection "锁信息"

$lockOverviewSql = @"
SELECT mode AS "锁模式",
       count(*) AS "数量",
       count(*) FILTER (WHERE granted) AS "已授予",
       count(*) FILTER (WHERE NOT granted) AS "等待中"
FROM pg_locks
GROUP BY mode
ORDER BY count(*) DESC;
"@

$section10OverviewHtml = Invoke-PgSafeHtmlTable -Query $lockOverviewSql -Headers @("锁模式", "数量", "已授予", "等待中") -EmptyMessage "无法获取锁信息"

$lockWaitSql = @"
SELECT blocked.pid AS "阻塞PID",
       blocked_activity.usename AS "阻塞用户",
       blocked.mode AS "请求锁模式",
       blocking.pid AS "持锁PID",
       blocking_activity.usename AS "持锁用户",
       blocking_activity.state AS "持锁状态",
       left(blocking_activity.query, 120) AS "持锁SQL"
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
"@

$section10WaitHtml = Invoke-PgSafeHtmlTable -Query $lockWaitSql -Headers @("阻塞PID", "阻塞用户", "请求锁模式", "持锁PID", "持锁用户", "持锁状态", "持锁SQL") -EmptyMessage "无锁等待"
$section10Html = "<h3>锁概览</h3>" + $section10OverviewHtml + "<h3>锁等待详情</h3>" + $section10WaitHtml

# ============================================================================
# Section 11: 复制状态
# ============================================================================
Write-NextSection "复制状态"

$pgWalLevel = Invoke-PgQuery -Query "SHOW wal_level;"
$pgArchiveMode = Invoke-PgQuery -Query "SHOW archive_mode;"
$pgMaxWalSenders = Invoke-PgQuery -Query "SHOW max_wal_senders;"

$section11KvHtml = ConvertTo-KvHtmlTable @{
    "wal_level" = $pgWalLevel
    "archive_mode" = $pgArchiveMode
    "max_wal_senders" = $pgMaxWalSenders
    "恢复模式" = $pgIsRecovery
}

# Replication slots
$repSlotSql = @"
SELECT slot_name AS "插槽名",
       plugin AS "插件",
       slot_type AS "类型",
       active::text AS "活跃",
       restart_lsn::text AS "重启LSN"
FROM pg_replication_slots
ORDER BY slot_name;
"@
$section11SlotHtml = Invoke-PgSafeHtmlTable -Query $repSlotSql -Headers @("插槽名", "插件", "类型", "活跃", "重启LSN") -EmptyMessage "无复制插槽"

# Primary or standby
if ($pgIsRecovery -eq "f" -or $pgIsRecovery -eq "false") {
    $repWalsenderSql = @"
SELECT pid AS "PID",
       usename AS "用户",
       client_addr::text AS "客户端",
       state AS "状态",
       sent_lsn::text AS "已发送LSN",
       replay_lsn::text AS "已回放LSN",
       CASE WHEN sent_lsn IS NOT NULL AND replay_lsn IS NOT NULL
            THEN pg_wal_lsn_diff(sent_lsn, replay_lsn)::text
            ELSE 'N/A'
       END AS "延迟(字节)",
       sync_state AS "同步状态"
FROM pg_stat_replication
ORDER BY client_addr;
"@
    $section11RepHtml = Invoke-PgSafeHtmlTable -Query $repWalsenderSql -Headers @("PID", "用户", "客户端", "状态", "已发送LSN", "已回放LSN", "延迟(字节)", "同步状态") -EmptyMessage "无活跃复制连接"
    $section11Html = $section11KvHtml + "<h3>复制插槽</h3>" + $section11SlotHtml + "<h3>WAL 发送进程（主库）</h3>" + $section11RepHtml
} else {
    $repWalRecvSql = @"
SELECT pid AS "PID",
       status AS "状态",
       sender_host AS "主库地址",
       sender_port AS "主库端口",
       received_lsn::text AS "已接收LSN",
       latest_end_lsn::text AS "最新LSN"
FROM pg_stat_wal_receiver;
"@
    $section11RepHtml = Invoke-PgSafeHtmlTable -Query $repWalRecvSql -Headers @("PID", "状态", "主库地址", "主库端口", "已接收LSN", "最新LSN") -EmptyMessage "无 WAL 接收进程"
    $section11Html = $section11KvHtml + "<h3>复制插槽</h3>" + $section11SlotHtml + "<h3>WAL 接收进程（备库）</h3>" + $section11RepHtml
}

# ============================================================================
# Section 12: 系统资源 (Windows)
# ============================================================================
Write-NextSection "系统资源"

# OS info
$osInfoHtml = "<table><tr><th>项目</th><th>值</th></tr>"
try {
    $osObj = Get-CimInstance Win32_OperatingSystem
    $csObj = Get-CimInstance Win32_ComputerSystem
    $osInfoHtml += "<tr><td>操作系统</td><td>$([System.Web.HttpUtility]::HtmlEncode($osObj.Caption)) $($osObj.Version)</td></tr>"
    $osInfoHtml += "<tr><td>主机名</td><td>$([System.Web.HttpUtility]::HtmlEncode($csObj.Name))</td></tr>"
    $osInfoHtml += "<tr><td>域</td><td>$([System.Web.HttpUtility]::HtmlEncode($csObj.Domain))</td></tr>"
    $bootTime = $osObj.LastBootUpTime
    $uptimeSpan = (Get-Date) - $bootTime
    $uptimeDays = [math]::Floor($uptimeSpan.TotalDays)
    $uptimeHours = $uptimeSpan.Hours
    $osInfoHtml += "<tr><td>启动时间</td><td>$($bootTime.ToString('yyyy-MM-dd HH:mm:ss'))</td></tr>"
    $osInfoHtml += "<tr><td>运行天数</td><td>${uptimeDays} 天 ${uptimeHours} 小时</td></tr>"
    $osInfoHtml += "<tr><td>系统架构</td><td>$($osObj.OSArchitecture)</td></tr>"
    $osInfoHtml += "<tr><td>逻辑处理器</td><td>$($csObj.NumberOfLogicalProcessors)</td></tr>"
    $osInfoHtml += "<tr><td>物理内存(GB)</td><td>$([math]::Round($csObj.TotalPhysicalMemory / 1GB, 2))</td></tr>"
} catch {
    $osInfoHtml += "<tr><td colspan='2'>采集失败: $([System.Web.HttpUtility]::HtmlEncode($_.Exception.Message))</td></tr>"
}
$osInfoHtml += "</table>"

# CPU usage
$cpuHtml = "<table><tr><th>指标</th><th>值</th></tr>"
try {
    $cpuPct = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $cpuHtml += "<tr><td>CPU 使用率</td><td>${cpuPct}%</td></tr>"
} catch {
    $cpuHtml += "<tr><td>CPU 使用率</td><td>N/A</td></tr>"
}
$cpuHtml += "</table>"

# Memory
$memSysHtml = "<table><tr><th>内存类型</th><th>总大小(GB)</th><th>已用(GB)</th><th>可用(GB)</th><th>使用率(%)</th></tr>"
try {
    $osMem = Get-CimInstance Win32_OperatingSystem
    $totalMem = [math]::Round($osMem.TotalVisibleMemorySize / 1MB, 2)
    $freeMem = [math]::Round($osMem.FreePhysicalMemory / 1MB, 2)
    $usedMem = [math]::Round($totalMem - $freeMem, 2)
    $memPct = if ($totalMem -gt 0) { [math]::Round($usedMem / $totalMem * 100, 1) } else { 0 }
    $memSysHtml += "<tr><td>物理内存</td><td>$totalMem</td><td>$usedMem</td><td>$freeMem</td><td>$memPct</td></tr>"
} catch {
    $memSysHtml += "<tr><td colspan='5'>采集失败</td></tr>"
}
$memSysHtml += "</table>"

# Disk
$diskHtml = "<table><tr><th>挂载点</th><th>总大小(GB)</th><th>可用(GB)</th><th>使用率(%)</th></tr>"
try {
    $disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3"
    foreach ($d in $disks) {
        $totalGB = [math]::Round($d.Size / 1GB, 2)
        $freeGB = [math]::Round($d.FreeSpace / 1GB, 2)
        $usagePct = if ($d.Size -gt 0) { [math]::Round(($d.Size - $d.FreeSpace) / $d.Size * 100, 1) } else { 0 }
        $diskClass = if ($usagePct -ge 90) { 'critical' } elseif ($usagePct -ge 80) { 'warn' } else { 'ok' }
        $diskHtml += "<tr><td>$($d.DeviceID)</td><td>$totalGB</td><td>$freeGB</td><td class=`"$diskClass`">$usagePct</td></tr>"
    }
} catch {
    $diskHtml += "<tr><td colspan='4'>采集失败</td></tr>"
}
$diskHtml += "</table>"

# Power plan
$powerPlanHtml = "<table><tr><th>项目</th><th>值</th></tr>"
try {
    $activePlan = Get-CimInstance -Namespace root\cimv2\power -ClassName Win32_PowerPlan | Where-Object { $_.IsActive -eq $true }
    if ($activePlan) {
        $powerPlanHtml += "<tr><td>当前电源计划</td><td>$($activePlan.ElementName)</td></tr>"
    } else {
        $powerPlanHtml += "<tr><td>当前电源计划</td><td>未知</td></tr>"
    }
} catch {
    $powerPlanHtml += "<tr><td>当前电源计划</td><td>N/A</td></tr>"
}
$powerPlanHtml += "</table>"

$section12Html = "<h3>主机信息</h3>" + $osInfoHtml + "<h3>CPU</h3>" + $cpuHtml + "<h3>内存</h3>" + $memSysHtml + "<h3>磁盘</h3>" + $diskHtml + "<h3>电源计划</h3>" + $powerPlanHtml

# ============================================================================
# Section 13: 用户角色
# ============================================================================
Write-NextSection "用户角色"

$usersSql = @"
SELECT rolname AS "角色",
       rolsuper::text AS "超级用户",
       rolcreaterole::text AS "创建角色",
       rolcreatedb::text AS "创建数据库",
       rolcanlogin::text AS "可登录",
       rolreplication::text AS "复制权限",
       rolconnlimit AS "连接限制",
       to_char(rolvaliduntil, 'YYYY-MM-DD') AS "密码过期"
FROM pg_roles
WHERE rolname NOT LIKE 'pg_%'
ORDER BY rolsuper DESC, rolname;
"@

$section13Html = Invoke-PgSafeHtmlTable -Query $usersSql -Headers @("角色", "超级用户", "创建角色", "创建数据库", "可登录", "复制权限", "连接限制", "密码过期") -EmptyMessage "无法获取用户角色"

# Superusers
$superusersSql = @"
SELECT rolname AS "超级用户",
       rolcanlogin::text AS "可登录",
       rolconnlimit AS "连接限制"
FROM pg_roles
WHERE rolsuper = true
ORDER BY rolname;
"@
$section13SuperHtml = Invoke-PgSafeHtmlTable -Query $superusersSql -Headers @("超级用户", "可登录", "连接限制") -EmptyMessage "无法获取超级用户"

# Extensions
$extensionsSql = @"
SELECT extname AS "扩展名",
       extversion AS "版本",
       n.nspname AS "模式"
FROM pg_extension e
JOIN pg_namespace n ON e.extnamespace = n.oid
ORDER BY extname;
"@
$section13ExtHtml = Invoke-PgSafeHtmlTable -Query $extensionsSql -Headers @("扩展名", "版本", "模式") -EmptyMessage "无法获取扩展列表"
$section13Html += "<h3>超级用户列表</h3>" + $section13SuperHtml + "<h3>已安装扩展</h3>" + $section13ExtHtml

# ============================================================================
# Key parameters (all important pg_settings)
# ============================================================================
Write-Host "[$SectionTotal/$SectionTotal] 关键参数汇总" -ForegroundColor Blue
$SectionCurrent = $SectionTotal

$allSettingsSql = @"
SELECT name AS "参数",
       setting AS "当前值",
       unit AS "单位",
       source AS "来源",
       short_desc AS "说明"
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
"@
$allSettingsHtml = Invoke-PgSafeHtmlTable -Query $allSettingsSql -Headers @("参数", "当前值", "单位", "来源", "说明") -EmptyMessage "无法获取参数配置"

# ============================================================================
# Build full HTML output
# ============================================================================
Write-Host "`n生成 HTML 报告..." -ForegroundColor Cyan

$htmlContent = @"
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="db-type" content="postgresql">
<meta name="generator" content="pg_check.ps1">
<meta name="version" content="$ScriptVersion">
<meta name="collect-time" content="$CollectTime">
<meta name="instance-name" content="$InstanceName">
<meta name="instance-host" content="$Host">
<meta name="instance-port" content="$Port">
<meta name="pg-version" content="$pgVersionShort">
<title>PostgreSQL 巡检报告 - $InstanceName - $CollectTime</title>
<style>
body { font-family: 'Microsoft YaHei', sans-serif; margin: 20px; background: #f5f5f5; }
h1 { color: #336791; border-bottom: 2px solid #336791; padding-bottom: 8px; }
h2 { color: #336791; margin-top: 20px; }
h3 { color: #264d73; font-size: 14px; margin: 14px 0 8px; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; background: #fff; }
th, td { border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 13px; }
th { background: #336791; color: #fff; }
tr:nth-child(even) { background: #f9f9f9; }
.section { margin: 15px 0; padding: 15px; background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.warn { color: #e65100; font-weight: 600; }
.critical { color: #c62828; font-weight: 600; }
.ok { color: #2e7d32; font-weight: 600; }
</style>
</head>
<body>
<h1>PostgreSQL 巡检报告</h1>
<p>实例: $InstanceName | 地址: ${Host}:${Port} | 版本: $pgVersionShort | 采集时间: $CollectTime</p>

<div class="section" id="basic_info">
<h2>1. 基本信息</h2>
$section1Html
</div>

<div class="section" id="connection_config">
<h2>2. 连接配置</h2>
$section2Html
</div>

<div class="section" id="memory_config">
<h2>3. 内存配置</h2>
$section3Html
</div>

<div class="section" id="wal_config">
<h2>4. WAL 配置</h2>
$section4Html
</div>

<div class="section" id="checkpoint_config">
<h2>5. 检查点配置</h2>
$section5Html
</div>

<div class="section" id="performance_stats">
<h2>6. 性能统计</h2>
$section6Html
</div>

<div class="section" id="top_tables">
<h2>7. 表统计 (Top 20)</h2>
$section7Html
</div>

<div class="section" id="processlist">
<h2>8. 活动进程</h2>
$section8Html
</div>

<div class="section" id="long_queries">
<h2>9. 长查询 (&gt;30s)</h2>
$section9Html
</div>

<div class="section" id="lock_info">
<h2>10. 锁信息</h2>
$section10Html
</div>

<div class="section" id="replication">
<h2>11. 复制状态</h2>
$section11Html
</div>

<div class="section" id="system_info">
<h2>12. 系统资源</h2>
$section12Html
</div>

<div class="section" id="users">
<h2>13. 用户角色</h2>
$section13Html
</div>

<div class="section" id="settings_summary">
<h2>附录: 关键参数汇总</h2>
$allSettingsHtml
</div>

<hr>
<p style="color:#999; font-size:12px;">
生成时间: $CollectTime | 脚本: pg_check.ps1 v$ScriptVersion | 平台: ora100.com
</p>
</body>
</html>
"@

$htmlContent | Out-File -FilePath $Output -Encoding UTF8

# Cleanup
if ($PasswordSet) {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}

Write-Host "`n采集完成！报告已保存至: $Output" -ForegroundColor Green
Write-Host "请将此文件上传至 ora100.com/oracheck-report 生成专业巡检报告。" -ForegroundColor Cyan
