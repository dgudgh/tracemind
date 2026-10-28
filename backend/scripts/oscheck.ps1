#Requires -Version 3.0
#==============================================================#
# File       :   OS Health Check (Windows)
# Desc       :   Oracle Database OS Health Check script for Windows
# Version    :   1.0.0
# Author     :   Lucifer (ora100.com)
# Copyright (C) 2021-2100 Pengcheng Liu
#==============================================================#
# Output encoding: UTF-8 without BOM
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

#==============================================================#
#                       Global Variables                        #
#==============================================================#
$scripts_dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$hostname = $env:COMPUTERNAME
$date = Get-Date -Format "yyyyMMdd"
$result_dir = Join-Path $scripts_dir "dbcheck_${hostname}_${date}"
$filename = Join-Path $result_dir "oscheck_${hostname}_${date}.txt"
$tarname = Join-Path $scripts_dir "dbcheck_${hostname}_${date}.zip"

# Detect Oracle services
$dbusers = @()
try {
    $oraServices = Get-Service -Name "OracleService*" -ErrorAction SilentlyContinue
    if ($oraServices) {
        $dbusers = $oraServices | ForEach-Object { $_.Name -replace '^OracleService', '' }
    }
} catch {}

# Detect ORACLE_HOME and ORACLE_SID from environment or registry
if (-not $env:ORACLE_HOME) {
    try {
        $regPath = "HKLM:\SOFTWARE\Oracle\KEY_*"
        $oraKey = Get-ItemProperty -Path $regPath -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($oraKey -and $oraKey.ORACLE_HOME) {
            $env:ORACLE_HOME = $oraKey.ORACLE_HOME
        }
    } catch {}
}
$ORACLE_HOME = $env:ORACLE_HOME

# Detect GI_HOME from registry or CRS process
$GI_HOME = ""
try {
    $crsProc = Get-Process -Name "crsd" -ErrorAction SilentlyContinue
    if ($crsProc) {
        $GI_HOME = Split-Path (Split-Path $crsProc.Path) -Parent
    }
} catch {}

#==============================================================#
#                       Helper Functions                        #
#==============================================================#
function Write-Section {
    param(
        [string]$Key,
        [string]$Value
    )
    # Output format must match oscheck.sh: key on first line, value on subsequent lines,
    # separated by blank lines between sections.
    "$Key"
    if ($Value) {
        "$Value"
    }
    ""  # blank line separator
}

function Write-NodeSection {
    param(
        [string]$Key,
        [string]$Value
    )
    # Node-format: key, then "** hostname:", then header line, then data lines
    "$Key"
    "** ${hostname}:"
    if ($Value) {
        "$Value"
    }
    ""
}

function Get-OracleVersion {
    try {
        $sqlplusPath = Join-Path $ORACLE_HOME "bin\sqlplus.exe"
        if (Test-Path $sqlplusPath) {
            $ver = & $sqlplusPath -v 2>$null | Select-String 'SQL' | ForEach-Object {
                if ($_ -match '(\d+\.\d+)') { $Matches[1] }
            }
            return $ver
        }
    } catch {}
    return ""
}

function Invoke-SqlPlus {
    param([string]$Sql)
    try {
        $sqlplusPath = Join-Path $ORACLE_HOME "bin\sqlplus.exe"
        if (Test-Path $sqlplusPath) {
            $result = $Sql | & $sqlplusPath -S "/ as sysdba" 2>$null
            return ($result | Out-String).Trim()
        }
    } catch {}
    return ""
}

#==============================================================#
#                        OS Info Check                         #
#==============================================================#
function Get-OSInfo {
    $sb = New-Object System.Text.StringBuilder

    # os_type - platform identifier for report template conditional rendering
    [void]$sb.AppendLine("os_type")
    [void]$sb.AppendLine("Windows")
    [void]$sb.AppendLine()

    # rac
    $israc = "NO"
    try {
        $crsService = Get-Service -Name "OracleGICluster*" -ErrorAction SilentlyContinue
        if (-not $crsService) {
            $crsService = Get-Service -Name "CRSService*" -ErrorAction SilentlyContinue
        }
        if ($crsService -and $crsService.Status -eq 'Running') {
            $israc = "YES"
        }
    } catch {}
    [void]$sb.AppendLine("rac")
    [void]$sb.AppendLine($israc)
    [void]$sb.AppendLine()

    # osversion
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    [void]$sb.AppendLine("osversion")
    [void]$sb.AppendLine("$($os.Caption) $($os.Version)")
    [void]$sb.AppendLine()

    # kernel
    [void]$sb.AppendLine("kernel")
    [void]$sb.AppendLine("$($os.Version) Build $($os.BuildNumber)")
    [void]$sb.AppendLine()

    # cpu
    $cpuInfo = Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1
    [void]$sb.AppendLine("cpu")
    [void]$sb.AppendLine($cpuInfo.Name)
    [void]$sb.AppendLine()

    # cpuasge (CPU usage %)
    $cpuLoad = (Get-CimInstance -ClassName Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    if ($null -eq $cpuLoad) { $cpuLoad = 0 }
    [void]$sb.AppendLine("cpuasge")
    [void]$sb.AppendLine([math]::Round($cpuLoad, 1).ToString())
    [void]$sb.AppendLine()

    # memtotal (GB)
    $totalMemGB = [math]::Round($os.TotalVisibleMemorySize / 1048576, 1)
    [void]$sb.AppendLine("memtotal")
    [void]$sb.AppendLine($totalMemGB.ToString())
    [void]$sb.AppendLine()

    # memusage (%)
    $freeMemKB = $os.FreePhysicalMemory
    $totalMemKB = $os.TotalVisibleMemorySize
    $memUsage = [math]::Round(($totalMemKB - $freeMemKB) / $totalMemKB * 100, 1)
    [void]$sb.AppendLine("memusage")
    [void]$sb.AppendLine($memUsage.ToString())
    [void]$sb.AppendLine()

    # swap (total virtual/page file in GB)
    $pageFile = Get-CimInstance -ClassName Win32_PageFileUsage -ErrorAction SilentlyContinue
    $swapTotalMB = 0
    $swapUsedMB = 0
    if ($pageFile) {
        $swapTotalMB = ($pageFile | Measure-Object -Property AllocatedBaseSize -Sum).Sum
        $swapUsedMB = ($pageFile | Measure-Object -Property CurrentUsage -Sum).Sum
    }
    $swapTotalGB = [math]::Round($swapTotalMB / 1024, 1)
    [void]$sb.AppendLine("swap")
    [void]$sb.AppendLine($swapTotalGB.ToString())
    [void]$sb.AppendLine()

    # swapusage (%)
    $swapUsage = 0
    if ($swapTotalMB -gt 0) {
        $swapUsage = [math]::Round($swapUsedMB / $swapTotalMB * 100, 1)
    }
    [void]$sb.AppendLine("swapusage")
    [void]$sb.AppendLine($swapUsage.ToString())
    [void]$sb.AppendLine()

    # loadaverage - Windows doesn't have load average, use CPU queue length as proxy
    $perfOS = Get-CimInstance -ClassName Win32_PerfFormattedData_PerfOS_System -ErrorAction SilentlyContinue
    $queueLen = if ($perfOS) { $perfOS.ProcessorQueueLength } else { 0 }
    [void]$sb.AppendLine("loadaverage")
    [void]$sb.AppendLine("ProcessorQueueLength: $queueLen")
    [void]$sb.AppendLine()

    # upday
    $bootTime = $os.LastBootUpTime
    $uptime = (Get-Date) - $bootTime
    $uptimeStr = "$([int]$uptime.TotalDays) days, $($uptime.Hours):$($uptime.Minutes.ToString('00'))"
    [void]$sb.AppendLine("upday")
    [void]$sb.AppendLine($uptimeStr)
    [void]$sb.AppendLine()

    # time
    [void]$sb.AppendLine("time")
    [void]$sb.AppendLine((Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
    [void]$sb.AppendLine()

    # hosts
    $hostsPath = "$env:SystemRoot\System32\drivers\etc\hosts"
    $hostsContent = ""
    if (Test-Path $hostsPath) {
        $hostsContent = (Get-Content $hostsPath -ErrorAction SilentlyContinue |
            Where-Object { $_ -and $_ -notmatch '^\s*#' -and $_ -notmatch '^\s*$' }) -join "`n"
    }
    [void]$sb.AppendLine("hosts")
    if ($hostsContent) { [void]$sb.AppendLine($hostsContent) }
    [void]$sb.AppendLine()

    # sysctl - Windows equivalent: relevant registry/system settings
    [void]$sb.AppendLine("sysctl")
    try {
        $tcpParams = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" -ErrorAction SilentlyContinue
        if ($tcpParams) {
            $keys = @("TcpTimedWaitDelay", "MaxUserPort", "TcpNumConnections", "KeepAliveTime", "KeepAliveInterval")
            foreach ($k in $keys) {
                $val = $tcpParams.$k
                if ($null -ne $val) {
                    [void]$sb.AppendLine("$k = $val")
                }
            }
        }
        # Memory management settings
        $mmParams = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management" -ErrorAction SilentlyContinue
        if ($mmParams) {
            $mmKeys = @("LargeSystemCache", "PagingFiles")
            foreach ($k in $mmKeys) {
                $val = $mmParams.$k
                if ($null -ne $val) {
                    if ($val -is [array]) { $val = $val -join ", " }
                    [void]$sb.AppendLine("$k = $val")
                }
            }
        }
    } catch {}
    [void]$sb.AppendLine()

    # limits - Windows doesn't have /etc/security/limits.conf
    [void]$sb.AppendLine("limits")
    try {
        # Show Oracle service account privileges if available
        $oraUser = $env:USERNAME
        [void]$sb.AppendLine("Current user: $oraUser")
        # Check lock pages in memory privilege
        $privOutput = & whoami /priv 2>$null
        if ($privOutput) {
            $lockPages = $privOutput | Select-String "SeLockMemoryPrivilege"
            if ($lockPages) {
                [void]$sb.AppendLine($lockPages.ToString().Trim())
            }
            $manageVolume = $privOutput | Select-String "SeManageVolumePrivilege"
            if ($manageVolume) {
                [void]$sb.AppendLine($manageVolume.ToString().Trim())
            }
        }
    } catch {}
    [void]$sb.AppendLine()

    # diskusage - must output node-based format: "** hostname:" then header then data rows
    # parse_txt_file splits each line by whitespace, then skips row[0] (header).
    # Template expects 7 columns: i.0=Filesystem i.1=Type i.2=Size i.3=Used i.4=Avail i.5=Use% i.6=Mounted
    # row[5] is compared with DISK_USAGE_THRESHOLD (e.g. "90%"), so Use% must be like "60%"
    [void]$sb.AppendLine("diskusage")
    [void]$sb.AppendLine("** ${hostname}:")
    [void]$sb.AppendLine("Filesystem Type Size Used Avail Use% Mounted")
    try {
        $disks = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DriveType=3"
        foreach ($d in $disks) {
            $sizeGB = [math]::Round($d.Size / 1GB, 1)
            $freeGB = [math]::Round($d.FreeSpace / 1GB, 1)
            $usedGB = [math]::Round(($d.Size - $d.FreeSpace) / 1GB, 1)
            $usePct = if ($d.Size -gt 0) { [math]::Round(($d.Size - $d.FreeSpace) / $d.Size * 100, 0).ToString() + "%" } else { "0%" }
            $fs = if ($d.FileSystem) { $d.FileSystem } else { "Unknown" }
            $mount = "$($d.DeviceID)\"
            $volName = if ($d.VolumeName) { $d.VolumeName } else { $mount }
            # Use single-space separation; parse_txt_file uses line.split() which handles any whitespace
            [void]$sb.AppendLine("$volName $fs ${sizeGB}G ${usedGB}G ${freeGB}G $usePct $mount")
        }
    } catch {
        [void]$sb.AppendLine("N/A")
    }
    [void]$sb.AppendLine()

    # inode - Windows NTFS doesn't have inodes; output placeholder in node format
    [void]$sb.AppendLine("inode")
    [void]$sb.AppendLine("** ${hostname}:")
    [void]$sb.AppendLine("Filesystem Type Inodes IUsed IFree IUse% Mounted")
    try {
        $disks = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DriveType=3"
        foreach ($d in $disks) {
            $mount = "$($d.DeviceID)\"
            $fs = if ($d.FileSystem) { $d.FileSystem } else { "Unknown" }
            $volName = if ($d.VolumeName) { $d.VolumeName } else { $mount }
            [void]$sb.AppendLine("$volName $fs - - - - $mount")
        }
    } catch {
        [void]$sb.AppendLine("N/A")
    }
    [void]$sb.AppendLine()

    # meminfo - parse_txt_file uses re.split(r"\s{2,}") so need at least 2 spaces between key:value
    # Linux format: "MemTotal:      16384000 kB" → split → ["MemTotal:", "16384000 kB"]
    # Template iterates {%tr for i in meminfo%} with {{i.0}} {{i.1}}
    [void]$sb.AppendLine("meminfo")
    try {
        $memTotalKB = $os.TotalVisibleMemorySize
        $memFreeKB = $os.FreePhysicalMemory
        $memAvailKB = $memFreeKB
        $swapTotalKBi = [math]::Round($swapTotalMB * 1024)
        $swapFreeKBi = [math]::Round(($swapTotalMB - $swapUsedMB) * 1024)
        [void]$sb.AppendLine("MemTotal:  $memTotalKB kB")
        [void]$sb.AppendLine("MemFree:  $memFreeKB kB")
        [void]$sb.AppendLine("MemAvailable:  $memAvailKB kB")
        [void]$sb.AppendLine("SwapTotal:  $swapTotalKBi kB")
        [void]$sb.AppendLine("SwapFree:  $swapFreeKBi kB")
    } catch {
        [void]$sb.AppendLine("N/A")
    }
    [void]$sb.AppendLine()

    # freemem - format must match `free -k` output for parse_txt_file
    # CRITICAL: parse_txt_file uses re.split(r"\s{2,}") to split columns
    # so columns must be separated by AT LEAST 2 spaces.
    # After split: Mem: row[1]=total, row[2]=used, row[3]=free (checked vs LOW_MEMORY_THRESHOLD_KB)
    # After split: Swap: row[1]=total, row[2]=used (checked vs HIGH_SWAP_THRESHOLD_KB)
    [void]$sb.AppendLine("freemem")
    try {
        $memUsedKB = $totalMemKB - $freeMemKB
        $cached = 0
        try {
            $perfMem = Get-CimInstance -ClassName Win32_PerfFormattedData_PerfOS_Memory -ErrorAction SilentlyContinue
            if ($perfMem) { $cached = [math]::Round($perfMem.CacheBytes / 1024) }
        } catch {}
        [void]$sb.AppendLine("              total        used        free      shared  buff/cache   available")
        [void]$sb.AppendLine(("Mem:  {0,12}  {1,12}  {2,12}  {3,12}  {4,12}  {5,12}" -f $totalMemKB, $memUsedKB, $freeMemKB, 0, $cached, $freeMemKB))
        $swapUsedKB = [math]::Round($swapUsedMB * 1024)
        $swapFreeKB = [math]::Round(($swapTotalMB - $swapUsedMB) * 1024)
        $swapTotalKB = [math]::Round($swapTotalMB * 1024)
        [void]$sb.AppendLine(("Swap:  {0,12}  {1,12}  {2,12}" -f $swapTotalKB, $swapUsedKB, $swapFreeKB))
    } catch {
        [void]$sb.AppendLine("N/A")
    }
    [void]$sb.AppendLine()

    # thp - Windows doesn't have THP; report as disabled (safe default)
    [void]$sb.AppendLine("thp")
    [void]$sb.AppendLine("always madvise [never]")
    [void]$sb.AppendLine()

    # crontab - Windows scheduled tasks for Oracle
    [void]$sb.AppendLine("crontab")
    try {
        $tasks = Get-ScheduledTask -ErrorAction SilentlyContinue |
            Where-Object { $_.TaskName -match "(?i)oracle|rman|backup|dbcheck" -and $_.State -ne "Disabled" }
        if ($tasks) {
            foreach ($t in $tasks) {
                [void]$sb.AppendLine("$($t.TaskName)  $($t.State)  $($t.TaskPath)")
            }
        }
    } catch {}
    [void]$sb.AppendLine()

    return $sb.ToString()
}

#==============================================================#
#                        Patch Info                            #
#==============================================================#
function Get-PatchInfo {
    $sb = New-Object System.Text.StringBuilder
    $dbver = Get-OracleVersion
    if ($dbver -and $dbver -ne "10.2") {
        $opatchPath = Join-Path $ORACLE_HOME "OPatch\opatch.bat"
        if (Test-Path $opatchPath) {
            [void]$sb.AppendLine("dbpatch")
            try {
                $patches = & $opatchPath lspatches 2>$null
                $patches = ($patches | Where-Object { $_ -and $_ -notmatch '^\s*$' } |
                    Select-Object -SkipLast 1) -join "`n"
                if ($patches) { [void]$sb.AppendLine($patches) }
            } catch {}
            [void]$sb.AppendLine()
        }
        if ($GI_HOME) {
            $giOpatch = Join-Path $GI_HOME "OPatch\opatch.bat"
            if (Test-Path $giOpatch) {
                [void]$sb.AppendLine("gipatch")
                try {
                    $patches = & $giOpatch lspatches 2>$null
                    $patches = ($patches | Where-Object { $_ -and $_ -notmatch '^\s*$' } |
                        Select-Object -SkipLast 1) -join "`n"
                    if ($patches) { [void]$sb.AppendLine($patches) }
                } catch {}
                [void]$sb.AppendLine()
            }
        }
    }
    return $sb.ToString()
}

#==============================================================#
#                       Listener Info                          #
#==============================================================#
function Get-ListenerInfo {
    $sb = New-Object System.Text.StringBuilder
    $lsnrctlPath = Join-Path $ORACLE_HOME "bin\lsnrctl.exe"

    [void]$sb.AppendLine("lsnrctl")
    if (Test-Path $lsnrctlPath) {
        try {
            $status = & $lsnrctlPath status listener 2>$null
            $status = ($status | Where-Object { $_ -and $_ -notmatch '^\s*$' }) -join "`n"
            if ($status) { [void]$sb.AppendLine($status) }
        } catch {}
    }
    [void]$sb.AppendLine()

    [void]$sb.AppendLine("sqlnet")
    $sqlnetPath = Join-Path $ORACLE_HOME "network\admin\sqlnet.ora"
    if (Test-Path $sqlnetPath) {
        $content = Get-Content $sqlnetPath -ErrorAction SilentlyContinue |
            Where-Object { $_ -and $_ -notmatch '^\s*#' -and $_ -notmatch '^\s*$' }
        if ($content) { [void]$sb.AppendLine(($content -join "`n")) }
    }
    [void]$sb.AppendLine()

    return $sb.ToString()
}

#==============================================================#
#                         DB Report                            #
#==============================================================#
function Get-DBReport {
    param([string[]]$SIDs)
    $dbver = Get-OracleVersion
    foreach ($SID in $SIDs) {
        $env:ORACLE_SID = $SID
        Write-Host "`n#==============================================================#"
        Write-Host "检查数据库实例：$SID"
        Write-Host "#==============================================================#`n"

        $sqlFile = ""
        switch -Wildcard ($dbver) {
            "10.2" { $sqlFile = "dbcheck10g.sql" }
            "11.1" { $sqlFile = "dbcheck_noncdb.sql" }
            "11.2" { $sqlFile = "dbcheck_noncdb.sql" }
            "12.1" { $sqlFile = "dbcheck_cdb_or_noncdb" }
            "12.2" { $sqlFile = "dbcheck_cdb_or_noncdb" }
            "18.*" { $sqlFile = "dbcheck_cdb_or_noncdb" }
            "19.*" { $sqlFile = "dbcheck_cdb_or_noncdb" }
            "21.*" { $sqlFile = "dbcheck_cdb_or_noncdb" }
            "23.*" { $sqlFile = "dbcheck_cdb_or_noncdb" }
            "26.*" { $sqlFile = "dbcheck_cdb_or_noncdb" }
            default {
                Write-Host "不支持的数据库版本: $dbver"
                return
            }
        }
        # 12c+ 需要检测 CDB 模式
        if ($sqlFile -eq "dbcheck_cdb_or_noncdb") {
            $isCDB = Invoke-SqlPlus "SET HEADING OFF FEEDBACK OFF PAGESIZE 0`nSELECT CDB FROM V`$DATABASE;`nEXIT;"
            $isCDB = $isCDB.Trim()
            if ($isCDB -eq "YES") {
                $sqlFile = "dbcheck_cdb.sql"
            } else {
                $sqlFile = "dbcheck_noncdb.sql"
            }
        }
        $sqlPath = Join-Path $scripts_dir $sqlFile
        if ($sqlFile -and (Test-Path $sqlPath)) {
            $sqlplusPath = Join-Path $ORACLE_HOME "bin\sqlplus.exe"
            Push-Location $scripts_dir
            & $sqlplusPath -S "/ as sysdba" "@$sqlFile" 2>$null
            Pop-Location
        } else {
            Write-Host "数据库巡检脚本 $sqlFile 未找到，请上传至 $scripts_dir 目录下!"
        }
    }
}

#==============================================================#
#                        Compress Log                          #
#==============================================================#
function Compress-LogFile {
    try {
        # Move HTML reports into result dir
        $htmlFiles = Get-ChildItem -Path $scripts_dir -Filter "dbcheck_*html" -ErrorAction SilentlyContinue
        foreach ($f in $htmlFiles) {
            Move-Item $f.FullName $result_dir -Force -ErrorAction SilentlyContinue
        }

        # Check if Compress-Archive is available (PowerShell 5.0+)
        if (-not (Get-Command Compress-Archive -ErrorAction SilentlyContinue)) {
            Write-Host "警告: Compress-Archive 不可用 (需要 PowerShell 5.0+)" -ForegroundColor Yellow
            Write-Host "巡检结果已保存在: $result_dir" -ForegroundColor Green
            Write-Host "请手动压缩此目录后上传" -ForegroundColor Yellow
            return
        }

        # Create zip archive
        Compress-Archive -Path (Join-Path $result_dir "*") -DestinationPath $tarname -Force
        if (Test-Path $tarname) {
            $zipSize = (Get-Item $tarname).Length / 1KB
            Write-Host "`n压缩包位置: $tarname" -ForegroundColor Green
            Write-Host "压缩包大小: $([math]::Round($zipSize, 1)) KB" -ForegroundColor Cyan
        } else {
            Write-Host "创建压缩包失败：文件未生成" -ForegroundColor Red
            Write-Host "请手动压缩目录: $result_dir" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "创建压缩包失败：$($_.Exception.Message)" -ForegroundColor Red
        Write-Host "请手动压缩目录: $result_dir" -ForegroundColor Yellow
    }
}

#==============================================================#
#                          Logo Print                          #
#==============================================================#
function Show-Logo {
    Write-Host @"

   ███████                             ██         ██      ██                    ██   ██   ██        ██████  ██                      ██
  ██░░░░░██                           ░██        ░██     ░██                   ░██  ░██  ░██       ██░░░░██░██                     ░██
 ██     ░░██ ██████  ██████    █████  ░██  █████ ░██     ░██  █████   ██████   ░██ ██████░██      ██    ░░ ░██       █████   █████ ░██  ██
░██      ░██░░██░░█ ░░░░░░██  ██░░░██ ░██ ██░░░██░██████████ ██░░░██ ░░░░░░██  ░██░░░██░ ░██████ ░██       ░██████  ██░░░██ ██░░░██░██ ██
░██      ░██ ░██ ░   ███████ ░██  ░░  ░██░███████░██░░░░░░██░███████  ███████  ░██  ░██  ░██░░░██░██       ░██░░░██░███████░██  ░░ ░████
░░██     ██  ░██    ██░░░░██ ░██   ██ ░██░██░░░░ ░██     ░██░██░░░░  ██░░░░██  ░██  ░██  ░██  ░██░░██    ██░██  ░██░██░░░░ ░██   ██░██░██
 ░░███████  ░███   ░░████████░░█████  ███░░██████░██     ░██░░██████░░████████ ███  ░░██ ░██  ░██ ░░██████ ░██  ░██░░██████░░█████ ░██░░██
  ░░░░░░░   ░░░     ░░░░░░░░  ░░░░░  ░░░  ░░░░░░ ░░      ░░  ░░░░░░  ░░░░░░░░ ░░░    ░░  ░░   ░░   ░░░░░░  ░░   ░░  ░░░░░░  ░░░░░  ░░  ░░

"@
}

#==============================================================#
#                        Usage / Help                          #
#==============================================================#
function Show-Help {
    Write-Host ""
    Write-Host "用法: oscheck.ps1 [-o <实例名>]"
    Write-Host "选项:"
    Write-Host "  -o    Oracle数据库实例名称，用于一次巡检多个实例，示例: -o orcl,luciferdb"
    Write-Host "  -h    显示帮助信息"
    Write-Host ""
}

#==============================================================#
#                        Pre-check                             #
#==============================================================#
function Initialize-Check {
    param([string]$ORACLE_SIDS_Param)

    $script:ORACLE_SIDS = $ORACLE_SIDS_Param

    if (-not $script:ORACLE_SIDS) {
        $script:ORACLE_SIDS = $env:ORACLE_SID
    }
    if (-not $script:ORACLE_SIDS) {
        Write-Host "ORACLE_SID 未设置，请通过 -o 参数指定或者设置环境变量 ORACLE_SID！" -ForegroundColor Red
        exit 1
    }

    # Verify each SID has a running service
    foreach ($SID in ($script:ORACLE_SIDS -split ',')) {
        $svcName = "OracleService$SID"
        $svc = Get-Service -Name $svcName -ErrorAction SilentlyContinue
        if (-not $svc -or $svc.Status -ne 'Running') {
            Write-Host "巡检数据库 [ $SID ] 服务未启动，请先启动数据库实例或者检查 [ -o ] 参数值 $($script:ORACLE_SIDS) 是否正确!" -ForegroundColor Red
            exit 1
        }
    }

    # Rename glogin.sql to avoid interference
    $gloginPath = Join-Path $ORACLE_HOME "sqlplus\admin\glogin.sql"
    if (Test-Path $gloginPath) {
        Rename-Item $gloginPath "$gloginPath.oraginal" -Force -ErrorAction SilentlyContinue
    }

    # Prepare result directory
    if (Test-Path $result_dir) { Remove-Item $result_dir -Recurse -Force }
    New-Item -ItemType Directory -Path $result_dir -Force | Out-Null

    # Get database version
    $script:dbver = Get-OracleVersion

    # Set NLS_LANG
    try {
        $nlsResult = Invoke-SqlPlus "SET HEADING OFF ECHO OFF TERMOUT OFF FEED OFF`nSELECT 'AMERICAN_AMERICA.'||property_value FROM database_properties WHERE property_name = 'NLS_CHARACTERSET';`nEXIT;"
        if ($nlsResult) { $env:NLS_LANG = $nlsResult.Trim() }
    } catch {}

    # RAC detection
    $script:israc = "NO"
    try {
        $crsService = Get-Service -Name "OracleGICluster*" -ErrorAction SilentlyContinue
        if (-not $crsService) {
            $crsService = Get-Service -Name "CRSService*" -ErrorAction SilentlyContinue
        }
        if ($crsService -and $crsService.Status -eq 'Running') {
            $script:israc = "YES"
        }
    } catch {}
}

function Restore-GLogin {
    $gloginPath = Join-Path $ORACLE_HOME "sqlplus\admin\glogin.sql"
    $origPath = "$gloginPath.oraginal"
    if (Test-Path $origPath) {
        Rename-Item $origPath $gloginPath -Force -ErrorAction SilentlyContinue
    }
}

#==============================================================#
#                            Main                              #
#==============================================================#
function Main {
    param(
        [string]$OracleSIDs = "",
        [switch]$Help
    )

    Show-Logo

    if ($Help) {
        Show-Help
        return
    }

    Initialize-Check -ORACLE_SIDS_Param $OracleSIDs

    Write-Host "`n#==============================================================#"
    Write-Host "Oracle数据库主机检查"
    Write-Host "#==============================================================#`n"

    Write-Host "收集主机 OS 层信息 ..." -ForegroundColor Cyan
    $osInfo = Get-OSInfo
    # Use .NET StreamWriter to write UTF-8 without BOM (Python's utf-8 codec doesn't skip BOM)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($filename, $osInfo, $utf8NoBom)

    Write-Host "收集数据库补丁信息 ..." -ForegroundColor Cyan
    $patchInfo = Get-PatchInfo
    [System.IO.File]::AppendAllText($filename, $patchInfo, $utf8NoBom)

    Write-Host "收集数据库监听信息 ..." -ForegroundColor Cyan
    $lsnrInfo = Get-ListenerInfo
    [System.IO.File]::AppendAllText($filename, $lsnrInfo, $utf8NoBom)

    # Run DB report (SQL checks)
    $sidArray = $script:ORACLE_SIDS -split ','
    Get-DBReport -SIDs $sidArray

    Compress-LogFile
    Restore-GLogin
}

# Parse command-line arguments
$oParam = ""
$showHelp = $false
for ($i = 0; $i -lt $args.Count; $i++) {
    switch ($args[$i]) {
        "-o" {
            if ($i + 1 -lt $args.Count) {
                $oParam = $args[$i + 1]
                $i++
            } else {
                Write-Host "参数 [ -o ] 的值为空，请检查！" -ForegroundColor Red
                exit 1
            }
        }
        "-h" { $showHelp = $true }
        "--help" { $showHelp = $true }
        default {
            Write-Host "脚本传参错误，请检查参数 [ $($args[$i]) ], 执行 powershell oscheck.ps1 -h 可以获得更多帮助！" -ForegroundColor Red
            exit 1
        }
    }
}

Main -OracleSIDs $oParam -Help:$showHelp
