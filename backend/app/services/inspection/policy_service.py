from __future__ import annotations

import calendar
import logging
from copy import deepcopy
from datetime import datetime, time, timedelta

from sqlalchemy.orm import Session

from app.models.inspection_policy import DBInspectionPolicy
from app.collectors.oracle.oracle_inspection_profile import SCRIPT_CATALOG
from .template_profiles import ORACLE_TEMPLATE_KEYS, ORACLE_PROFILE_LABELS

_LOGGER = logging.getLogger(__name__)


HOST_OSCHECK_CATEGORIES = [
    "host_os",
    "host_cpu",
    "host_memory",
    "host_storage",
    "host_kernel",
    "host_limits",
    "host_scheduler",
    "oracle_runtime",
    "oracle_patch",
    "oracle_listener",
    "rac_cluster",
    "report_archive",
]


HOST_OSCHECK_ITEMS = [
    {"key": "host.os.type", "name": "操作系统类型", "category": "host_os", "enabled": True, "severity": "info"},
    {"key": "host.os.release", "name": "操作系统版本", "category": "host_os", "enabled": True, "severity": "info"},
    {"key": "host.kernel.version", "name": "内核版本", "category": "host_os", "enabled": True, "severity": "info"},
    {"key": "host.time.current", "name": "主机时间", "category": "host_os", "enabled": True, "severity": "info"},
    {"key": "host.network.hosts", "name": "/etc/hosts 配置", "category": "host_os", "enabled": True, "severity": "info"},
    {"key": "host.cpu.model", "name": "CPU 型号", "category": "host_cpu", "enabled": True, "severity": "info"},
    {"key": "host.cpu.used_pct", "name": "CPU 使用率", "category": "host_cpu", "enabled": True, "severity": "warning"},
    {"key": "host.load_average", "name": "系统负载", "category": "host_cpu", "enabled": True, "severity": "warning"},
    {"key": "host.memory.total_gb", "name": "物理内存容量", "category": "host_memory", "enabled": True, "severity": "info"},
    {"key": "host.memory.used_pct", "name": "内存使用率", "category": "host_memory", "enabled": True, "severity": "warning"},
    {"key": "host.swap.total_gb", "name": "Swap 容量", "category": "host_memory", "enabled": True, "severity": "info"},
    {"key": "host.swap.used_pct", "name": "Swap 使用率", "category": "host_memory", "enabled": True, "severity": "warning"},
    {"key": "host.memory.detail", "name": "/proc/meminfo 关键项", "category": "host_memory", "enabled": True, "severity": "info"},
    {"key": "host.memory.thp_hugepages", "name": "THP 与 HugePages", "category": "host_memory", "enabled": True, "severity": "warning"},
    {"key": "host.filesystem.usage", "name": "文件系统使用率", "category": "host_storage", "enabled": True, "severity": "critical"},
    {"key": "host.filesystem.inode", "name": "inode 使用率", "category": "host_storage", "enabled": True, "severity": "warning"},
    {"key": "host.kernel.sysctl", "name": "内核参数 sysctl / tunables", "category": "host_kernel", "enabled": True, "severity": "warning"},
    {"key": "host.user.limits", "name": "用户 limits / nofiles / stack", "category": "host_limits", "enabled": True, "severity": "warning"},
    {"key": "host.scheduler.crontab", "name": "Oracle 用户 crontab", "category": "host_scheduler", "enabled": True, "severity": "info"},
    {"key": "oracle.runtime.dbusers", "name": "Oracle 软件用户识别", "category": "oracle_runtime", "enabled": True, "severity": "info"},
    {"key": "oracle.runtime.instances", "name": "运行中 Oracle 实例识别", "category": "oracle_runtime", "enabled": True, "severity": "critical"},
    {"key": "oracle.runtime.version_route", "name": "按版本选择 dbcheck SQL", "category": "oracle_runtime", "enabled": True, "severity": "info"},
    {"key": "oracle.patch.db_home", "name": "数据库 Home 补丁", "category": "oracle_patch", "enabled": True, "severity": "warning"},
    {"key": "oracle.patch.gi_home", "name": "Grid Infrastructure 补丁", "category": "oracle_patch", "enabled": True, "severity": "warning"},
    {"key": "oracle.listener.status", "name": "监听器状态", "category": "oracle_listener", "enabled": True, "severity": "critical"},
    {"key": "oracle.listener.sqlnet", "name": "sqlnet.ora 配置", "category": "oracle_listener", "enabled": True, "severity": "info"},
    {"key": "rac.cluster.detect", "name": "RAC 架构检测", "category": "rac_cluster", "enabled": True, "severity": "info"},
    {"key": "rac.cluster.ssh_trust", "name": "RAC 节点 SSH 互信", "category": "rac_cluster", "enabled": True, "severity": "warning"},
    {"key": "rac.cluster.ocrcheck", "name": "OCR 状态", "category": "rac_cluster", "enabled": True, "severity": "critical"},
    {"key": "rac.cluster.votedisk", "name": "Voting Disk 状态", "category": "rac_cluster", "enabled": True, "severity": "critical"},
    {"key": "rac.cluster.ocrbackup", "name": "OCR 备份", "category": "rac_cluster", "enabled": True, "severity": "warning"},
    {"key": "rac.cluster.oifcfg", "name": "RAC 网络接口配置", "category": "rac_cluster", "enabled": True, "severity": "warning"},
    {"key": "rac.cluster.crs_status", "name": "CRS 资源状态", "category": "rac_cluster", "enabled": True, "severity": "critical"},
    {"key": "rac.cluster.nodes", "name": "集群节点清单", "category": "rac_cluster", "enabled": True, "severity": "info"},
    {"key": "report.output.oscheck_file", "name": "oscheck 文本报告", "category": "report_archive", "enabled": True, "severity": "info"},
    {"key": "report.output.dbcheck_html", "name": "数据库 HTML 报告归档", "category": "report_archive", "enabled": True, "severity": "info"},
    {"key": "report.output.tarball", "name": "巡检压缩包", "category": "report_archive", "enabled": True, "severity": "info"},
]


def metric(
    key: str,
    name: str,
    category: str,
    severity: str,
    normal_range: str,
    warning_range: str,
    critical_range: str,
    unit: str,
    collect_method: str,
    advice: str,
    description: str,
    sql: str = "",
) -> dict:
    return {
        "key": key,
        "name": name,
        "category": category,
        "enabled": True,
        "severity": severity,
        "normal_range": normal_range,
        "warning_range": warning_range,
        "critical_range": critical_range,
        "unit": unit,
        "collect_method": collect_method,
        "advice": advice,
        "description": description,
        "sql": sql,
    }


def section_metric(
    key: str,
    name: str,
    category: str,
    collect_method: str,
    description: str,
    advice: str,
    sql: str = "",
    severity: str = "warning",
    normal_range: str = "采集成功且无异常项",
    warning_range: str = "存在警告项、权限降级或需关注数据",
    critical_range: str = "存在严重风险项或采集失败",
    unit: str = "状态/数量",
) -> dict:
    return metric(
        key,
        name,
        category,
        severity,
        normal_range,
        warning_range,
        critical_range,
        unit,
        collect_method,
        advice,
        description,
        sql,
    )


ORACLE_STANDARD_ITEMS = [
    metric(
        "oracle.tablespace.used_pct",
        "表空间使用率",
        "space",
        "critical",
        "< 85%",
        "85% - 95%",
        ">= 95%",
        "%",
        "内置 SQL 采集",
        "扩容表空间或清理历史数据，优先处理 SYSTEM、SYSAUX、UNDO 和业务核心表空间。",
        "检查数据文件与表空间已用比例，判断容量风险。",
    ),
    metric(
        "oracle.temp.used_pct",
        "临时表空间使用率",
        "space",
        "warning",
        "< 70%",
        "70% - 90%",
        ">= 90%",
        "%",
        "内置 SQL 采集",
        "定位排序、hash join、临时段占用较高的会话，必要时扩容 TEMP。",
        "检查 TEMP 使用率，发现大排序和临时段压力。",
    ),
    metric(
        "oracle.session.used_pct",
        "会话使用率",
        "sessions",
        "warning",
        "< 80%",
        "80% - 90%",
        ">= 90%",
        "%",
        "内置 SQL 采集",
        "排查连接池配置、异常空闲会话和 sessions/processes 参数容量。",
        "检查当前会话数与参数上限的比例。",
    ),
    metric(
        "oracle.blocking.seconds",
        "阻塞时长",
        "performance",
        "critical",
        "< 60 秒",
        "60 - 300 秒",
        ">= 300 秒",
        "秒",
        "内置 SQL 采集",
        "定位 blocker SID、SQL ID 与对象，评估是否 kill 会话或优化事务提交。",
        "检查阻塞链路持续时间和影响范围。",
    ),
    metric(
        "oracle.backup.age_hours",
        "最近备份间隔",
        "backup",
        "critical",
        "<= 24 小时",
        "24 - 48 小时",
        "> 48 小时",
        "小时",
        "RMAN/视图采集",
        "确认 RMAN 任务、归档备份和备份介质状态，补跑备份。",
        "检查最近一次成功备份距离当前的时间。",
    ),
    metric(
        "oracle.fra.used_pct",
        "FRA 使用率",
        "space",
        "critical",
        "< 80%",
        "80% - 90%",
        ">= 90%",
        "%",
        "内置 SQL 采集",
        "清理过期归档、调整备份保留策略或扩容 FRA。",
        "检查快速恢复区空间压力。",
    ),
]

ORACLE_STANDARD_ITEMS.extend(
    [
        section_metric(
            "oracle.basic.database_info",
            "数据库基础信息",
            "basic",
            "DBCheck SQL / v$database / v$instance",
            "采集数据库名、DBID、版本、启动时间、字符集、归档模式等基础画像。",
            "确认版本、字符集、归档模式和补丁级别符合生产基线。",
            "select name, dbid, log_mode, open_mode from v$database",
            severity="info",
        ),
        section_metric(
            "oracle.basic.instance_rac",
            "实例/RAC 状态",
            "availability",
            "DBCheck SQL / gv$instance",
            "采集单实例或 RAC 节点、实例状态、线程、主机名和启动时间。",
            "RAC 节点异常或实例状态异常时检查 CRS、监听和实例告警日志。",
            "select inst_id, instance_name, host_name, status from gv$instance",
        ),
        section_metric(
            "oracle.archive.mode",
            "归档模式",
            "logs",
            "DBCheck SQL / v$database",
            "检查数据库是否开启 ARCHIVELOG，以及归档目标是否可用。",
            "生产库建议开启归档并监控归档目标空间，未开启时评估恢复目标差距。",
            "select log_mode from v$database",
            severity="critical",
        ),
        section_metric(
            "oracle.controlfile.count",
            "控制文件数量",
            "backup",
            "DBCheck SQL / v$controlfile",
            "检查控制文件多路复用数量和路径。",
            "控制文件少于 2 份时建议补齐多路复用并纳入备份校验。",
            "select count(*) controlfile_count from v$controlfile",
        ),
        section_metric(
            "oracle.redo.member_count",
            "Redo Log 成员数量",
            "logs",
            "DBCheck SQL / v$logfile",
            "检查在线日志组和成员数量，识别单成员日志组风险。",
            "单成员日志组建议补齐 member，避免磁盘故障导致实例不可恢复。",
            "select group#, count(*) members from v$logfile group by group#",
            severity="critical",
        ),
        section_metric(
            "oracle.archive.generate_mb_per_hour",
            "归档日志生成速率",
            "logs",
            "DBCheck SQL / v$archived_log",
            "统计近期归档日志平均生成速率，用于容量、备份窗口和 Data Guard 评估。",
            "生成速率异常升高时排查批量任务、日志模式、热点 SQL 和归档备份能力。",
            "select round(sum(blocks*block_size)/1024/1024/168,2) mb_per_hour from v$archived_log where first_time > sysdate - 7",
            normal_range="< 业务基线 120%",
            warning_range="业务基线 120% - 200%",
            critical_range="> 业务基线 200%",
            unit="MB/小时",
        ),
        section_metric(
            "oracle.awr.snapshot_coverage",
            "AWR 快照覆盖",
            "performance",
            "DBCheck SQL / dba_hist_snapshot",
            "检查 AWR 快照区间、保留策略和近期开销分析可用性。",
            "缺少 AWR 快照时检查 statistics_level、快照任务和 SYSAUX 空间。",
            "select count(*) snapshots from dba_hist_snapshot where begin_interval_time > sysdate - 1",
            normal_range="最近 24 小时快照完整",
            warning_range="快照间隔异常或部分缺失",
            critical_range="无可用快照或 AWR 不可用",
            unit="状态/个数",
        ),
        section_metric(
            "oracle.performance.top_sql_elapsed",
            "Top SQL 耗时",
            "performance",
            "AWR / v$sql",
            "采集耗时最高 SQL，用于识别性能风险和优化候选。",
            "优先分析执行次数高、平均耗时高、等待集中的 SQL，结合执行计划优化。",
            "select sql_id, elapsed_time, executions from v$sql order by elapsed_time desc fetch first 20 rows only",
            normal_range="无超过慢 SQL 阈值",
            warning_range="存在慢 SQL",
            critical_range="核心业务 SQL 严重超时",
            unit="毫秒/次数",
        ),
        section_metric(
            "oracle.performance.top_sql_buffer_gets",
            "Top SQL 逻辑读",
            "performance",
            "AWR / v$sql",
            "采集逻辑读最高 SQL，发现缓存压力和低效访问路径。",
            "检查索引选择性、执行计划、绑定变量和全表扫描。",
            "select sql_id, buffer_gets, executions from v$sql order by buffer_gets desc fetch first 20 rows only",
            unit="逻辑读/次数",
        ),
        section_metric(
            "oracle.performance.top_sql_disk_reads",
            "Top SQL 物理读",
            "performance",
            "AWR / v$sql",
            "采集物理读最高 SQL，发现 IO 压力来源。",
            "结合等待事件、索引、统计信息和存储延迟排查物理读异常。",
            "select sql_id, disk_reads, executions from v$sql order by disk_reads desc fetch first 20 rows only",
            unit="物理读/次数",
        ),
        section_metric(
            "oracle.performance.wait_events",
            "等待事件 Top",
            "performance",
            "AWR / v$system_event / gv$session",
            "采集主要等待事件，用于区分 IO、锁、网络、日志和并发问题。",
            "排除 idle 等待后，按 Top 等待事件定位根因并结合 SQL 和对象分析。",
            "select event, total_waits, time_waited from v$system_event order by time_waited desc fetch first 20 rows only",
            unit="等待次数/时间",
        ),
        section_metric(
            "oracle.objects.invalid_count",
            "无效对象数量",
            "objects",
            "DBA_OBJECTS",
            "检查 INVALID 对象数量和对象类型。",
            "对业务 schema 的 INVALID 对象执行编译并检查依赖对象或授权变更。",
            "select owner, object_type, count(*) invalid_count from dba_objects where status='INVALID' group by owner, object_type",
            normal_range="0 个",
            warning_range="1 - 20 个",
            critical_range="> 20 个或核心 schema 存在无效对象",
            unit="个",
        ),
        section_metric(
            "oracle.objects.index_unusable",
            "失效索引数量",
            "objects",
            "DBA_INDEXES",
            "检查 UNUSABLE 索引和分区索引。",
            "失效索引会影响 SQL 计划，需确认装载任务后重建或修复。",
            "select owner, index_name, status from dba_indexes where status <> 'VALID'",
            normal_range="0 个",
            warning_range="1 - 5 个",
            critical_range="> 5 个或核心表索引失效",
            unit="个",
        ),
        section_metric(
            "oracle.objects.stats_stale",
            "统计信息过期对象",
            "objects",
            "DBA_TAB_STATISTICS",
            "检查统计信息缺失或过期的表和分区。",
            "对高变更对象制定统计信息采集窗口，避免执行计划漂移。",
            "select owner, table_name, stale_stats from dba_tab_statistics where stale_stats='YES'",
            normal_range="0 个核心对象",
            warning_range="存在非核心对象过期",
            critical_range="核心业务对象统计信息过期或缺失",
            unit="个",
        ),
        section_metric(
            "oracle.security.account_status",
            "用户账号状态",
            "security",
            "DBA_USERS",
            "检查 OPEN、LOCKED、EXPIRED 等账号状态分布。",
            "关闭无用账号，确认业务账号密码状态，避免过期导致连接失败。",
            "select account_status, count(*) from dba_users group by account_status",
        ),
        section_metric(
            "oracle.security.default_password",
            "默认/弱口令线索",
            "security",
            "DBA_USERS / 基线规则",
            "检查常见默认账号、过期策略和弱口令线索。",
            "禁用默认账号，启用密码复杂度策略，并对例外账号记录审批。",
            "select username, account_status from dba_users where oracle_maintained='N'",
            severity="critical",
        ),
        section_metric(
            "oracle.dataguard.lag",
            "Data Guard 延迟",
            "availability",
            "Data Guard 视图 / v$dataguard_stats",
            "检查主备传输延迟、应用延迟和归档 gap。",
            "延迟异常时检查归档、网络、MRP 进程、备库 IO 和大事务回放。",
            "select name, value, unit from v$dataguard_stats",
            normal_range="< 60 秒",
            warning_range="60 - 300 秒",
            critical_range=">= 300 秒或存在 gap",
            unit="秒/状态",
        ),
        section_metric(
            "oracle.listener.status",
            "监听器连通性",
            "connection",
            "监听/连接探测",
            "检查监听器可达性、服务注册和连接入口状态。",
            "监听异常时检查 listener.ora、local_listener、服务注册和防火墙。",
            "select name, value from v$parameter where name in ('local_listener','remote_listener')",
            severity="critical",
        ),
        section_metric(
            "oracle.config.parameter_baseline",
            "参数基线差异",
            "basic",
            "v$parameter / 基线规则",
            "检查关键参数是否偏离巡检基线。",
            "对 SGA/PGA、processes、归档、审计等参数变更建立审批和回退记录。",
            "select name, value from v$parameter where isdefault='FALSE'",
        ),
        section_metric(
            "oracle.objects.segment_fragmentation",
            "段碎片/高水位",
            "objects",
            "DBA_SEGMENTS / DBA_TABLES",
            "识别大表碎片、高水位和可治理对象。",
            "对碎片严重的大表先评估业务窗口、备份和回退，再制定在线重组方案。",
            "select owner, segment_name, segment_type, bytes from dba_segments order by bytes desc fetch first 20 rows only",
        ),
        section_metric(
            "oracle.space.recyclebin_usage",
            "回收站占用",
            "space",
            "DBA_RECYCLEBIN",
            "检查回收站对象数量和占用空间。",
            "回收站占用异常时先确认恢复需求，再按库级策略清理。",
            "select owner, count(*) objects from dba_recyclebin group by owner",
            normal_range="0 或低于基线",
            warning_range="高于基线",
            critical_range="占用导致表空间压力",
            unit="个/MB",
        ),
        section_metric(
            "oracle.undo.used_pct",
            "UNDO 使用率",
            "space",
            "DBA_UNDO_EXTENTS / v$undostat",
            "检查 UNDO 表空间使用、长事务和 undo 保留压力。",
            "UNDO 压力高时排查长事务、批量作业和 retention 设置，必要时扩容。",
            "select status, count(*) extents from dba_undo_extents group by status",
            normal_range="< 70%",
            warning_range="70% - 90%",
            critical_range=">= 90% 或出现 ORA-01555 风险",
            unit="%",
        ),
    ]
)

MYSQL_STANDARD_ITEMS = [
    metric(
        "mysql.connection.used_pct",
        "连接使用率",
        "connection",
        "critical",
        "< 80%",
        "80% - 90%",
        ">= 90%",
        "%",
        "SHOW STATUS/VARIABLES",
        "检查连接池泄漏、慢请求堆积和 max_connections 设置。",
        "检查 Threads_connected 与 max_connections 的比例。",
    ),
    metric(
        "mysql.slow_queries.per_min",
        "慢查询速率",
        "performance",
        "warning",
        "< 5 次/分钟",
        "5 - 20 次/分钟",
        ">= 20 次/分钟",
        "次/分钟",
        "performance_schema/状态变量",
        "打开慢日志明细，优先优化扫描行数高和无索引 SQL。",
        "检查慢查询增长速率，判断性能退化。",
    ),
    metric(
        "mysql.innodb_buffer_pool.hit_pct",
        "Buffer Pool 命中率",
        "performance",
        "warning",
        ">= 99%",
        "95% - 99%",
        "< 95%",
        "%",
        "SHOW GLOBAL STATUS",
        "评估 innodb_buffer_pool_size、热点表和全表扫描。",
        "检查 InnoDB 缓冲池读命中率。",
    ),
    metric(
        "mysql.replication.lag_seconds",
        "主从复制延迟",
        "availability",
        "critical",
        "< 60 秒",
        "60 - 300 秒",
        ">= 300 秒",
        "秒",
        "SHOW REPLICA STATUS",
        "检查 SQL 线程、IO 线程、网络、长事务和大事务回放。",
        "检查复制延迟是否影响容灾和读写分离。",
    ),
    metric(
        "mysql.disk.used_pct",
        "数据目录磁盘使用率",
        "space",
        "critical",
        "< 80%",
        "80% - 90%",
        ">= 90%",
        "%",
        "主机/数据目录采集",
        "清理 binlog、归档历史表或扩容磁盘，先确认备份可用。",
        "检查 MySQL 数据目录所在文件系统容量。",
    ),
    metric(
        "mysql.backup.age_hours",
        "最近备份间隔",
        "backup",
        "critical",
        "<= 24 小时",
        "24 - 48 小时",
        "> 48 小时",
        "小时",
        "备份任务/元数据采集",
        "确认备份任务、备份文件和恢复验证状态，补跑备份。",
        "检查最近一次成功备份距离当前的时间。",
    ),
]

MYSQL_STANDARD_ITEMS.extend(
    [
        section_metric(
            "mysql.script.basic_info",
            "基础信息",
            "basic",
            "mysql_check.sh 章节 1 / SELECT @@version",
            "采集版本、主机、端口、server_id、字符集、basedir、datadir 和运行时长。",
            "确认版本生命周期、字符集、server_id、数据目录和运行时长是否符合生产基线。",
            "SELECT @@version AS version, @@hostname AS hostname, @@port AS port, @@server_id AS server_id",
            severity="info",
        ),
        section_metric(
            "mysql.script.connection_config",
            "连接配置",
            "connection",
            "mysql_check.sh 章节 2 / SHOW VARIABLES/STATUS",
            "采集 max_connections、Threads_connected、Threads_running、超时和 skip_name_resolve。",
            "连接接近上限时检查连接池泄漏、慢请求堆积和超时参数。",
            "SHOW GLOBAL STATUS WHERE Variable_name IN ('Threads_connected','Threads_running','Max_used_connections')",
        ),
        section_metric(
            "mysql.script.memory_config",
            "内存配置",
            "performance",
            "mysql_check.sh 章节 3 / SHOW VARIABLES",
            "采集 Buffer Pool、sort/join/read buffer、tmp_table_size、table cache、thread cache。",
            "结合物理内存和连接数评估全局/会话级内存，避免过度承诺。",
            "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('innodb_buffer_pool_size','sort_buffer_size','join_buffer_size','tmp_table_size')",
        ),
        section_metric(
            "mysql.script.log_config",
            "日志配置",
            "logs",
            "mysql_check.sh 章节 4 / SHOW VARIABLES",
            "采集慢日志、binlog、long_query_time、redo log、flush/sync 策略。",
            "开启必要日志并确认保留策略，binlog 和 redo 配置需与恢复目标匹配。",
            "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('slow_query_log','log_bin','long_query_time','sync_binlog')",
        ),
        section_metric(
            "mysql.script.innodb_config",
            "InnoDB 配置",
            "performance",
            "mysql_check.sh 章节 5 / SHOW VARIABLES",
            "采集 file_per_table、flush_method、doublewrite、IO threads、dirty page、AHI、page size。",
            "按存储介质、写入压力和实例规格校准 InnoDB 关键参数。",
            "SHOW GLOBAL VARIABLES WHERE Variable_name LIKE 'innodb_%'",
        ),
        section_metric(
            "mysql.script.performance",
            "性能指标",
            "performance",
            "mysql_check.sh 章节 6 / SHOW GLOBAL STATUS",
            "采集查询统计、InnoDB 性能、锁、临时表、连接和流量。",
            "关注临时表落盘、锁等待、全表扫描和读写流量异常变化。",
            "SHOW GLOBAL STATUS",
        ),
        section_metric(
            "mysql.script.sampled_metrics",
            "实时采样指标",
            "performance",
            "mysql_check.sh 章节 6.5 / 1 秒状态变量采样",
            "采集 QPS、TPS、读写 QPS、InnoDB IOPS 和实时 Buffer Pool 命中率。",
            "实时突增时结合业务发布时间、慢 SQL、锁等待和磁盘 IO 排查。",
            "SHOW GLOBAL STATUS WHERE Variable_name IN ('Questions','Com_commit','Com_rollback','Innodb_buffer_pool_reads')",
            normal_range="符合业务实时基线",
            warning_range="超过业务基线 120% - 200%",
            critical_range="超过业务基线 200% 或出现雪崩式增长",
            unit="次/秒",
        ),
        section_metric(
            "mysql.script.database_info",
            "数据库信息",
            "basic",
            "mysql_check.sh 章节 7 / information_schema",
            "采集数据库大小、当前进程列表和存储引擎分布。",
            "关注异常大库、非 InnoDB 引擎、异常进程和对象增长。",
            "SELECT table_schema, SUM(data_length+index_length) size_bytes FROM information_schema.TABLES GROUP BY table_schema",
            severity="info",
        ),
        section_metric(
            "mysql.script.security",
            "安全信息",
            "security",
            "mysql_check.sh 章节 8 / mysql.user / replication",
            "采集账号、权限、复制账号和安全配置线索。",
            "清理无用账号，限制远程 root，确认复制账号和管理员账号最小权限。",
            "SELECT user, host, plugin FROM mysql.user",
            severity="critical",
        ),
        section_metric(
            "mysql.script.innodb_deep",
            "InnoDB 深度诊断",
            "performance",
            "mysql_check.sh 章节 9 / SHOW ENGINE INNODB STATUS",
            "采集 InnoDB Status、最近死锁、长事务、锁等待和 History List Length。",
            "History List Length 或死锁异常时排查长事务、热点行和事务提交策略。",
            "SHOW ENGINE INNODB STATUS",
            normal_range="History List Length < 100000 且无长事务/死锁",
            warning_range="存在长事务、锁等待或 History List Length 偏高",
            critical_range="持续阻塞、频繁死锁或 History List Length 严重堆积",
        ),
        section_metric(
            "mysql.script.table_analysis",
            "表与索引分析",
            "objects",
            "mysql_check.sh 章节 10 / information_schema + performance_schema",
            "采集大表、无主键表、碎片、未使用索引、冗余索引和表级 IO 热点。",
            "优先治理大表无主键、碎片高和热点 IO 对象，变更前确认备份与窗口。",
            "SELECT table_schema, table_name, data_length, index_length, data_free FROM information_schema.TABLES",
        ),
        section_metric(
            "mysql.script.wait_events",
            "等待事件 (Top 20)",
            "performance",
            "mysql_check.sh 章节 11 / performance_schema",
            "采集 performance_schema 等待事件 Top 20。",
            "结合等待类型定位 IO、锁、元数据锁、网络或文件系统瓶颈。",
            "SELECT event_name, count_star FROM performance_schema.events_waits_summary_global_by_event_name ORDER BY count_star DESC LIMIT 20",
        ),
        section_metric(
            "mysql.script.ssl_status",
            "SSL/TLS 安全状态",
            "security",
            "mysql_check.sh 章节 12 / SHOW VARIABLES/STATUS",
            "采集 SSL 配置和当前连接 SSL 状态。",
            "跨主机生产连接建议启用 TLS，并确认证书有效期和客户端配置。",
            "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('have_ssl','ssl_ca','ssl_cert','ssl_key')",
        ),
        section_metric(
            "mysql.script.password_policy",
            "密码策略与认证安全",
            "security",
            "mysql_check.sh 章节 13 / mysql.user / validate_password",
            "检查 validate_password、旧认证插件和无密码账号。",
            "禁用空密码和旧认证插件，启用密码复杂度与到期策略。",
            "SELECT user, host, plugin FROM mysql.user",
            severity="critical",
        ),
        section_metric(
            "mysql.script.autoinc_check",
            "auto_increment 使用率",
            "objects",
            "mysql_check.sh 章节 14 / information_schema.COLUMNS",
            "检查自增列当前值、最大值和使用率预警。",
            "自增使用率高时评估字段类型扩容、归档和业务改造窗口。",
            "SELECT table_schema, table_name, column_name, column_type FROM information_schema.COLUMNS WHERE extra LIKE '%auto_increment%'",
            normal_range="< 70%",
            warning_range="70% - 90%",
            critical_range=">= 90%",
            unit="%",
        ),
        section_metric(
            "mysql.script.long_queries",
            "长查询",
            "performance",
            "mysql_check.sh 章节 15 / processlist",
            "采集运行超过 30 秒的非 Sleep 查询。",
            "长查询需结合执行计划、锁等待和业务批处理窗口分析。",
            "SELECT id, user, db, command, time, state, info FROM information_schema.PROCESSLIST WHERE command <> 'Sleep' AND time >= 30",
            normal_range="0 条",
            warning_range="1 - 5 条",
            critical_range="> 5 条或核心 SQL 长时间运行",
            unit="条",
        ),
        section_metric(
            "mysql.script.replication_deep",
            "复制深度诊断",
            "availability",
            "mysql_check.sh 章节 16 / SHOW REPLICA + performance_schema",
            "采集 Binlog、从库、半同步、Group Replication、过滤器、GTID 和复制通道。",
            "复制异常时检查 IO/SQL 线程、网络、GTID、过滤规则和大事务回放。",
            "SHOW REPLICA STATUS",
            normal_range="复制线程正常且延迟 < 60 秒",
            warning_range="复制延迟 60 - 300 秒或存在配置风险",
            critical_range="复制中断、延迟 >= 300 秒或通道错误",
            unit="秒/状态",
        ),
        section_metric(
            "mysql.script.backup_check",
            "备份检查",
            "backup",
            "mysql_check.sh 章节 17 / binlog + 备份事件",
            "采集 Binlog 备份参数、Binlog 文件、InnoDB 持久化和备份事件。",
            "确认物理/逻辑备份、Binlog 保留和恢复演练能满足 RPO/RTO。",
            "SHOW BINARY LOGS",
            severity="critical",
        ),
        section_metric(
            "mysql.script.system_info",
            "系统资源",
            "host",
            "mysql_check.sh 章节 18 / 数据库可见主机信息",
            "采集 hostname、OS、datadir、tmpdir；CPU/内存/磁盘需主机监控源补齐。",
            "绑定 SSH、Zabbix 或 Prometheus 后补齐 OS 级 CPU、内存、磁盘和 IO 指标。",
            "SELECT @@hostname AS hostname, @@datadir AS datadir, @@tmpdir AS tmpdir",
        ),
        section_metric(
            "mysql.script.expert_tuning",
            "专家级调优指标",
            "performance",
            "mysql_check.sh 章节 19 / Handler 读取模式",
            "采集 Handler 读取模式统计，对标 MySQLTuner 风格分析。",
            "Handler_read_rnd_next 占比高时重点排查全表扫描、缺失索引和统计信息。",
            "SHOW GLOBAL STATUS WHERE Variable_name LIKE 'Handler_read%'",
            normal_range="rnd_next_to_key_ratio < 10",
            warning_range="10 - 50",
            critical_range=">= 50",
            unit="比例",
        ),
    ]
)

POSTGRESQL_STANDARD_ITEMS = [
    metric(
        "postgresql.connection.used_pct",
        "连接使用率",
        "connection",
        "critical",
        "< 80%",
        "80% - 90%",
        ">= 90%",
        "%",
        "pg_stat_activity/pg_settings",
        "排查连接池、空闲事务和 max_connections 容量。",
        "检查当前连接数与最大连接数比例。",
    ),
    metric(
        "postgresql.database.size_growth_pct",
        "数据库容量增长率",
        "space",
        "warning",
        "< 10%/天",
        "10% - 20%/天",
        ">= 20%/天",
        "%/天",
        "pg_database_size/历史快照",
        "定位增长最快的库、表和索引，评估归档或分区。",
        "检查数据库容量增长速度。",
    ),
    metric(
        "postgresql.replication.lag_seconds",
        "复制延迟",
        "availability",
        "critical",
        "< 60 秒",
        "60 - 300 秒",
        ">= 300 秒",
        "秒",
        "pg_stat_replication",
        "检查网络、WAL 发送、备库回放和长事务。",
        "检查流复制延迟是否影响高可用切换。",
    ),
    metric(
        "postgresql.deadlocks.count",
        "死锁次数",
        "performance",
        "warning",
        "0 次",
        "1 - 5 次/天",
        "> 5 次/天",
        "次/天",
        "pg_stat_database",
        "分析应用事务顺序和锁等待 SQL，必要时优化索引和事务边界。",
        "检查数据库死锁计数。",
    ),
    metric(
        "postgresql.cache.hit_pct",
        "缓存命中率",
        "performance",
        "warning",
        ">= 99%",
        "95% - 99%",
        "< 95%",
        "%",
        "pg_stat_database",
        "检查 shared_buffers、热点 SQL、表膨胀和全表扫描。",
        "检查 block cache 命中率。",
    ),
    metric(
        "postgresql.backup.age_hours",
        "最近备份间隔",
        "backup",
        "critical",
        "<= 24 小时",
        "24 - 48 小时",
        "> 48 小时",
        "小时",
        "备份任务/元数据采集",
        "确认 base backup、WAL 归档和恢复演练结果，补跑备份。",
        "检查最近一次成功备份距离当前的时间。",
    ),
]

POSTGRESQL_STANDARD_ITEMS.extend(
    [
        section_metric(
            "postgresql.script.basic_info",
            "基本信息",
            "basic",
            "pg_check.sh 章节 1 / version()",
            "采集版本、当前库、当前用户、启动时间、数据目录、配置文件和恢复模式。",
            "确认版本、配置路径、恢复模式和运行身份符合生产基线。",
            "SELECT version(), current_database(), current_user, pg_postmaster_start_time()",
            severity="info",
        ),
        section_metric(
            "postgresql.script.connection",
            "连接信息",
            "connection",
            "pg_check.sh 章节 2 / pg_stat_activity / pg_settings",
            "采集最大连接、超级用户预留、当前连接、连接状态分布和用户连接分布。",
            "连接接近上限时检查连接池、idle in transaction 和应用泄漏。",
            "SELECT state, count(*) FROM pg_stat_activity GROUP BY state",
        ),
        section_metric(
            "postgresql.script.database_info",
            "数据库信息",
            "space",
            "pg_check.sh 章节 3 / pg_database_size",
            "采集数据库大小、编码、排序规则、连接限制和当前连接。",
            "关注容量增长、连接限制和编码/排序规则一致性。",
            "SELECT datname, pg_database_size(datname) AS size_bytes FROM pg_database WHERE datistemplate = false",
            severity="info",
        ),
        section_metric(
            "postgresql.script.table_stats",
            "表统计",
            "objects",
            "pg_check.sh 章节 4 / pg_stat_user_tables",
            "采集活跃行、死元组、死元组率、VACUUM/ANALYZE 时间。",
            "死元组率高时检查 autovacuum、长事务和批量更新模式。",
            "SELECT schemaname, relname, n_live_tup, n_dead_tup FROM pg_stat_user_tables",
            normal_range="核心表死元组率 < 10%",
            warning_range="10% - 20%",
            critical_range=">= 20% 或 autovacuum 长期失效",
            unit="%",
        ),
        section_metric(
            "postgresql.script.index_usage",
            "索引使用分析",
            "objects",
            "pg_check.sh 章节 5 / pg_stat_user_indexes",
            "采集索引扫描率、未使用索引和表扫描线索。",
            "对未使用大索引先确认业务周期，再评估删除；对低扫描率表优化索引。",
            "SELECT schemaname, relname, indexrelname, idx_scan FROM pg_stat_user_indexes",
            normal_range="无核心未使用大索引",
            warning_range="存在可复核索引",
            critical_range="存在大量无效索引或核心表缺失索引",
            unit="个/比例",
        ),
        section_metric(
            "postgresql.script.performance",
            "性能指标",
            "performance",
            "pg_check.sh 章节 6 / pg_stat_database / pg_stat_bgwriter",
            "采集缓存命中率、BGWriter/Checkpointer、事务提交和回滚。",
            "缓存命中率低或 checkpoint 压力高时检查 shared_buffers、SQL 和写入峰值。",
            "SELECT datname, blks_hit, blks_read, xact_commit, xact_rollback FROM pg_stat_database",
        ),
        section_metric(
            "postgresql.script.replication",
            "复制状态",
            "availability",
            "pg_check.sh 章节 7 / pg_replication_slots / pg_stat_replication",
            "采集 wal_level、archive_mode、复制槽、主库 WAL 发送或备库 WAL 接收。",
            "复制槽积压或延迟异常时检查网络、WAL 保留、备库回放和长事务。",
            "SELECT pid, usename, client_addr, state, sent_lsn, replay_lsn FROM pg_stat_replication",
            normal_range="复制正常且延迟 < 60 秒",
            warning_range="延迟 60 - 300 秒或复制槽积压",
            critical_range="复制中断、延迟 >= 300 秒或 WAL 堆积",
            unit="秒/状态",
        ),
        section_metric(
            "postgresql.script.lock_info",
            "锁信息",
            "sessions",
            "pg_check.sh 章节 8 / pg_locks",
            "采集锁概览和锁等待详情。",
            "锁等待异常时定位 blocker pid、SQL、事务年龄和对象。",
            "SELECT mode, granted, count(*) FROM pg_locks GROUP BY mode, granted",
            normal_range="0 个阻塞等待",
            warning_range="1 - 5 个等待",
            critical_range="> 5 个等待或核心事务阻塞",
            unit="个",
        ),
        section_metric(
            "postgresql.script.long_queries",
            "长查询",
            "performance",
            "pg_check.sh 章节 9 / pg_stat_activity",
            "采集运行超过 30 秒的查询。",
            "长查询需结合执行计划、锁等待、work_mem 和业务批处理窗口分析。",
            "SELECT pid, usename, datname, now() - query_start AS duration, query FROM pg_stat_activity WHERE state <> 'idle'",
            normal_range="0 条超过 30 秒",
            warning_range="1 - 5 条",
            critical_range="> 5 条或核心查询长时间运行",
            unit="条",
        ),
        section_metric(
            "postgresql.script.wait_events",
            "等待事件",
            "performance",
            "pg_check.sh 章节 10 / pg_stat_activity",
            "采集 wait_event_type、wait_event 和会话状态。",
            "按等待类型区分锁、IO、客户端、WAL 和后台进程瓶颈。",
            "SELECT wait_event_type, wait_event, count(*) FROM pg_stat_activity GROUP BY wait_event_type, wait_event",
        ),
        section_metric(
            "postgresql.script.security",
            "安全信息",
            "security",
            "pg_check.sh 章节 11 / pg_roles / pg_extension",
            "采集角色、超级用户、复制权限、连接限制和扩展。",
            "控制超级用户数量，复核扩展来源和账号有效期。",
            "SELECT rolname, rolsuper, rolreplication, rolcanlogin, rolvaliduntil FROM pg_roles",
            normal_range="超级用户 <= 1 且无异常账号",
            warning_range="超级用户数量偏多或存在过期策略缺失",
            critical_range="存在未授权超级用户或高危扩展",
            unit="个/状态",
        ),
        section_metric(
            "postgresql.script.settings",
            "关键参数",
            "basic",
            "pg_check.sh 章节 12 / pg_settings",
            "采集 shared_buffers、work_mem、WAL、autovacuum、SSL、track_counts 等关键参数。",
            "关键参数变更应有审批、基线和回退记录，异常值需结合负载评估。",
            "SELECT name, setting, unit, source FROM pg_settings WHERE name IN ('max_connections','shared_buffers','work_mem','autovacuum','archive_mode')",
        ),
        section_metric(
            "postgresql.script.system_info",
            "系统信息",
            "host",
            "pg_check.sh 章节 13 / 数据库可见主机信息",
            "采集数据库可见地址、端口、目录和 backend_pid；OS 指标需主机监控源补齐。",
            "绑定 SSH、Zabbix 或 Prometheus 后补齐 CPU、内存、磁盘、IO 和网络指标。",
            "SELECT inet_server_addr(), inet_server_port(), pg_backend_pid()",
        ),
    ]
)

SQLSERVER_STANDARD_ITEMS = [
    metric(
        "sqlserver.database.used_pct",
        "数据库文件使用率",
        "space",
        "critical",
        "< 80%",
        "80% - 90%",
        ">= 90%",
        "%",
        "系统视图采集",
        "检查数据文件自动增长、磁盘空间和大对象表，必要时扩容。",
        "检查数据库数据文件空间使用率。",
    ),
    metric(
        "sqlserver.log.used_pct",
        "事务日志使用率",
        "space",
        "critical",
        "< 70%",
        "70% - 90%",
        ">= 90%",
        "%",
        "DBCC SQLPERF/sys.dm_db_log_space_usage",
        "检查长事务、日志备份和恢复模式，必要时执行日志备份。",
        "检查事务日志空间压力。",
    ),
    metric(
        "sqlserver.blocking.seconds",
        "阻塞时长",
        "performance",
        "critical",
        "< 60 秒",
        "60 - 300 秒",
        ">= 300 秒",
        "秒",
        "DMV 采集",
        "定位 blocking_session_id、等待资源和 SQL，评估终止或优化事务。",
        "检查阻塞链持续时间。",
    ),
    metric(
        "sqlserver.cpu.used_pct",
        "CPU 使用率",
        "performance",
        "warning",
        "< 80%",
        "80% - 90%",
        ">= 90%",
        "%",
        "性能计数器/DMV",
        "分析 TOP CPU SQL、并行度、索引缺失和统计信息。",
        "检查 SQL Server 实例 CPU 压力。",
    ),
    metric(
        "sqlserver.backup.age_hours",
        "最近完整备份间隔",
        "backup",
        "critical",
        "<= 24 小时",
        "24 - 48 小时",
        "> 48 小时",
        "小时",
        "msdb 备份历史",
        "确认备份作业、备份链和恢复验证，补跑完整备份。",
        "检查最近一次成功完整备份距离当前的时间。",
    ),
    metric(
        "sqlserver.availability.sync_state",
        "可用性副本同步状态",
        "availability",
        "critical",
        "SYNCHRONIZED",
        "SYNCHRONIZING",
        "NOT SYNCHRONIZING / DISCONNECTED",
        "状态",
        "AlwaysOn DMV",
        "检查副本连接、redo 队列、send 队列和网络状态。",
        "检查 AlwaysOn 可用性组副本同步状态。",
    ),
]

SQLSERVER_STANDARD_ITEMS.extend(
    [
        section_metric(
            "sqlserver.script.version_info",
            "版本信息",
            "basic",
            "mssql_check.sh 章节 1 / SERVERPROPERTY",
            "采集 SQL Server 版本、Edition、ProductVersion 和 ProductLevel。",
            "确认版本和补丁级别是否处于厂商支持周期，定期评估 CU/SP。",
            "SELECT @@VERSION AS sqlserver_version",
            severity="info",
        ),
        section_metric(
            "sqlserver.script.connection_info",
            "连接信息",
            "connection",
            "mssql_check.sh 章节 2 / @@MAX_CONNECTIONS",
            "采集 SQL Server 最大连接数和连接容量线索。",
            "结合当前连接数、连接池配置和业务并发评估连接容量。",
            "SELECT @@MAX_CONNECTIONS AS max_connections",
        ),
        section_metric(
            "sqlserver.script.active_sessions",
            "活动会话",
            "sessions",
            "mssql_check.sh 章节 3 / sys.dm_exec_sessions",
            "采集用户会话、登录名、主机、程序和状态。",
            "活动会话或等待会话异常升高时进入会话事务页查看阻塞链和 SQL 文本。",
            "SELECT session_id, login_name, host_name, program_name, status FROM sys.dm_exec_sessions WHERE is_user_process = 1",
        ),
        section_metric(
            "sqlserver.script.database_size",
            "数据库大小",
            "space",
            "mssql_check.sh 章节 4 / sys.databases + sys.master_files",
            "采集数据库容量、状态和恢复模式。",
            "关注 TOP 数据库增长趋势，并与磁盘余量、备份窗口和归档策略联动分析。",
            "SELECT d.name, SUM(mf.size) * 8 / 1024.0 AS size_mb FROM sys.databases d JOIN sys.master_files mf ON d.database_id = mf.database_id GROUP BY d.name",
            severity="info",
        ),
        section_metric(
            "sqlserver.script.database_status",
            "数据库状态",
            "availability",
            "mssql_check.sh 章节 5 / sys.databases",
            "检查数据库 ONLINE、恢复模式和兼容级别。",
            "异常数据库需检查恢复状态、磁盘空间、最近恢复/附加操作和 SQL Server Error Log。",
            "SELECT name, state_desc, recovery_model_desc, compatibility_level FROM sys.databases",
            normal_range="全部 ONLINE",
            warning_range="存在恢复模式或兼容级别需关注",
            critical_range="存在非 ONLINE 数据库",
            unit="状态/个",
            severity="critical",
        ),
        section_metric(
            "sqlserver.script.config_parameters",
            "关键配置",
            "basic",
            "mssql_check.sh 章节 6 / sys.configurations",
            "采集 max memory、MAXDOP、并行成本阈值和备份压缩等关键配置。",
            "结合 CPU、内存、并行等待和备份窗口评估配置基线。",
            "SELECT name, value_in_use FROM sys.configurations WHERE name IN ('max server memory (MB)','max degree of parallelism','cost threshold for parallelism')",
        ),
        section_metric(
            "sqlserver.script.wait_stats",
            "等待统计 TOP 10",
            "performance",
            "mssql_check.sh 章节 7 / sys.dm_os_wait_stats",
            "采集等待事件 TOP 10。",
            "排除系统背景等待后，结合 SQL 画像、磁盘 IO 和阻塞链分析等待根因。",
            "SELECT TOP 10 wait_type, wait_time_ms, waiting_tasks_count FROM sys.dm_os_wait_stats WHERE wait_time_ms > 0 ORDER BY wait_time_ms DESC",
        ),
        section_metric(
            "sqlserver.script.cpu_usage",
            "CPU 信息",
            "performance",
            "mssql_check.sh 章节 8 / @@CPU_BUSY",
            "采集 CPU_BUSY、IDLE 和 TIMETICKS。",
            "CPU 指标异常时结合 Top SQL、等待统计、并行度和操作系统监控分析。",
            "SELECT @@CPU_BUSY AS cpu_busy_ms, @@IDLE AS idle_ms, @@TIMETICKS AS time_ticks",
        ),
        section_metric(
            "sqlserver.script.blocking",
            "阻塞信息",
            "sessions",
            "mssql_check.sh 章节 9 / sys.dm_exec_requests",
            "采集 blocking_session_id、等待类型、等待时间和请求状态。",
            "阻塞异常时定位根 blocker、事务持续时间和 SQL 文本，先生成处置方案再人工执行。",
            "SELECT session_id, blocking_session_id, wait_type, wait_time FROM sys.dm_exec_requests WHERE blocking_session_id <> 0",
            normal_range="0 个阻塞",
            warning_range="1 - 5 个阻塞",
            critical_range="> 5 个阻塞或核心事务阻塞",
            unit="个",
            severity="critical",
        ),
        section_metric(
            "sqlserver.script.lock_info",
            "锁信息",
            "sessions",
            "mssql_check.sh 章节 10 / sys.dm_tran_locks",
            "采集锁模式、资源类型和等待锁数量。",
            "锁等待异常时结合阻塞链、事务和执行计划定位热点对象。",
            "SELECT resource_type, request_mode, request_status, COUNT(*) AS count FROM sys.dm_tran_locks GROUP BY resource_type, request_mode, request_status",
        ),
        section_metric(
            "sqlserver.script.deadlock_info",
            "死锁信息",
            "performance",
            "mssql_check.sh 章节 11 / system_health 扩展事件",
            "采集最近死锁事件线索。",
            "出现死锁时分析对象访问顺序、索引覆盖和事务粒度。",
            "SELECT CAST(target_data AS XML) FROM sys.dm_xe_session_targets",
            normal_range="0 次",
            warning_range="1 - 5 次/天",
            critical_range="> 5 次/天或核心链路死锁",
            unit="次/天",
            severity="critical",
        ),
        section_metric(
            "sqlserver.script.backup_info",
            "备份信息",
            "backup",
            "mssql_check.sh 章节 12 / msdb.backupset",
            "采集数据库最近完整、差异和日志备份信息。",
            "确认备份链完整、备份校验和恢复演练结果，缺失时补跑备份。",
            "SELECT database_name, MAX(backup_finish_date) AS last_backup FROM msdb.dbo.backupset GROUP BY database_name",
            severity="critical",
        ),
        section_metric(
            "sqlserver.script.log_space",
            "日志空间",
            "logs",
            "mssql_check.sh 章节 13 / DBCC SQLPERF(LOGSPACE)",
            "采集事务日志大小和使用率。",
            "日志使用率高时检查长事务、日志备份、恢复模式和自动增长。",
            "DBCC SQLPERF(LOGSPACE)",
            normal_range="< 70%",
            warning_range="70% - 90%",
            critical_range=">= 90%",
            unit="%",
            severity="critical",
        ),
        section_metric(
            "sqlserver.script.memory_usage",
            "SQL Server 内存",
            "performance",
            "mssql_check.sh 章节 14 / sys.dm_os_process_memory",
            "采集 SQL Server 进程内存使用和内存压力线索。",
            "内存压力异常时检查 max server memory、Buffer Manager 和操作系统余量。",
            "SELECT physical_memory_in_use_kb, process_physical_memory_low FROM sys.dm_os_process_memory",
            normal_range="符合实例内存基线",
            warning_range="接近 max memory 或 OS 余量不足",
            critical_range="出现内存压力或频繁外部等待",
            unit="MB/状态",
        ),
        section_metric(
            "sqlserver.script.linked_servers",
            "链接服务器",
            "security",
            "mssql_check.sh 章节 15 / sys.servers",
            "采集链接服务器数量和配置。",
            "复核链接服务器权限、凭据和跨库访问风险，清理无用配置。",
            "SELECT name, product, provider, data_source FROM sys.servers WHERE is_linked = 1",
        ),
        section_metric(
            "sqlserver.script.file_groups",
            "文件组信息",
            "space",
            "mssql_check.sh 章节 16 / sys.filegroups",
            "采集文件组、数据文件和增长配置。",
            "文件增长过小或百分比增长容易造成碎片和频繁增长，应统一为合理固定增量。",
            "SELECT name, type_desc, is_default FROM sys.filegroups",
        ),
        section_metric(
            "sqlserver.script.system_info",
            "系统信息",
            "host",
            "mssql_check.sh 章节 17 / SERVERPROPERTY + DMV",
            "采集实例可见主机、服务、版本和环境信息；OS 资源需监控源补齐。",
            "绑定主机监控源后联动分析 CPU、内存、磁盘、IO 和网络瓶颈。",
            "SELECT SERVERPROPERTY('MachineName') AS machine_name, SERVERPROPERTY('InstanceName') AS instance_name",
            severity="info",
        ),
    ]
)


DEFAULT_POLICIES = [
    {
        "policy_key": "oracle_standard",
        "name": "Oracle 默认巡检模板",
        "db_types": ["oracle"],
        "enabled": True,
        "schedule": "manual_or_daily",
        "categories": ["basic", "connection", "space", "logs", "sessions", "objects", "performance", "backup", "security", "availability"],
        "item_config": ORACLE_STANDARD_ITEMS,
        "threshold_config": {
            "space_usage_pct": 85,
            "critical_space_usage_pct": 95,
            "blocking_seconds": 300,
            "backup_age_hours": 24,
        },
        "action_config": ["create_ticket", "push_zabbix", "generate_action_plan", "export_report"],
        "report_config": {
            "include_summary": True,
            "include_findings": True,
            "include_actions": True,
            "include_raw_data": False,
        },
        "remark": "Oracle 专属巡检模板，覆盖 DBCheck/AWR、空间、日志、会话、对象、性能、备份、安全和高可用指标。",
        "read_only": True,
    },
    {
        "policy_key": "mysql_standard",
        "name": "MySQL 默认巡检模板",
        "db_types": ["mysql"],
        "enabled": True,
        "schedule": "manual_or_daily",
        "categories": ["basic", "connection", "space", "logs", "objects", "performance", "backup", "security", "availability", "host"],
        "item_config": MYSQL_STANDARD_ITEMS,
        "threshold_config": {
            "connection_usage_pct": 80,
            "slow_queries_per_min": 5,
            "replication_lag_seconds": 60,
            "backup_age_hours": 24,
        },
        "action_config": ["create_ticket", "push_zabbix", "generate_action_plan", "export_report"],
        "report_config": {
            "include_summary": True,
            "include_findings": True,
            "include_actions": True,
            "include_raw_data": False,
        },
        "remark": "MySQL 专属巡检模板，对齐 mysql_check.sh 章节，覆盖基础、连接、内存、日志、InnoDB、性能、对象、安全、复制、备份和系统资源。",
        "read_only": True,
    },
    {
        "policy_key": "postgresql_standard",
        "name": "PostgreSQL 默认巡检模板",
        "db_types": ["postgresql"],
        "enabled": True,
        "schedule": "manual_or_daily",
        "categories": ["basic", "connection", "space", "sessions", "objects", "performance", "backup", "security", "availability", "host"],
        "item_config": POSTGRESQL_STANDARD_ITEMS,
        "threshold_config": {
            "connection_usage_pct": 80,
            "replication_lag_seconds": 60,
            "deadlocks_per_day": 1,
            "backup_age_hours": 24,
        },
        "action_config": ["create_ticket", "push_zabbix", "generate_action_plan", "export_report"],
        "report_config": {
            "include_summary": True,
            "include_findings": True,
            "include_actions": True,
            "include_raw_data": False,
        },
        "remark": "PostgreSQL 专属巡检模板，对齐 pg_check.sh 章节，覆盖基础、连接、数据库、表统计、索引、性能、复制、锁、长查询、安全、参数和系统信息。",
        "read_only": True,
    },
    {
        "policy_key": "sqlserver_standard",
        "name": "SQL Server 默认巡检模板",
        "db_types": ["sqlserver"],
        "enabled": True,
        "schedule": "manual_or_daily",
        "categories": ["basic", "connection", "space", "logs", "sessions", "performance", "backup", "security", "availability", "host"],
        "item_config": SQLSERVER_STANDARD_ITEMS,
        "threshold_config": {
            "database_used_pct": 80,
            "log_used_pct": 70,
            "blocking_seconds": 300,
            "backup_age_hours": 24,
        },
        "action_config": ["create_ticket", "push_zabbix", "generate_action_plan", "export_report"],
        "report_config": {
            "include_summary": True,
            "include_findings": True,
            "include_actions": True,
            "include_raw_data": False,
        },
        "remark": "SQL Server 专属巡检模板，对齐 mssql_check.sh 17 项，覆盖版本、连接、会话、空间、数据库状态、配置、等待、CPU、阻塞、锁、死锁、备份、日志、内存、链接服务器、文件组和系统信息。",
        "read_only": True,
    },
    {
        "policy_key": "daily_standard",
        "name": "日常标准巡检",
        "db_types": ["oracle", "mysql", "postgresql", "sqlserver"],
        "enabled": True,
        "schedule": "manual_or_daily",
        "categories": [
            "basic",
            "connection",
            "space",
            "logs",
            "sessions",
            "objects",
            "performance",
            "backup",
            "ha",
            "security",
            "host",
        ],
        "item_config": [
            {"key": "connection.basic", "name": "Connection availability", "enabled": True, "severity": "critical"},
            {"key": "space.capacity", "name": "Space and capacity", "enabled": True, "severity": "warning"},
            {"key": "performance.slow_sql", "name": "Slow SQL and performance", "enabled": True, "severity": "warning"},
            {"key": "backup.recovery", "name": "Backup and recovery readiness", "enabled": True, "severity": "warning"},
            {"key": "security.account", "name": "Security and account baseline", "enabled": True, "severity": "info"},
        ],
        "threshold_config": {
            "space_usage_pct": 85,
            "critical_space_usage_pct": 95,
            "slow_sql_ms": 3000,
            "blocking_seconds": 300,
            "backup_age_hours": 24,
        },
        "action_config": ["create_ticket", "push_zabbix", "generate_action_plan", "export_report"],
        "report_config": {
            "include_summary": True,
            "include_findings": True,
            "include_actions": True,
            "include_raw_data": False,
        },
        "read_only": True,
    },
    {
        "policy_key": "host_resource",
        "name": "Host / Oracle OS Health Check",
        "template_type": "主机与 Oracle OS 巡检模板",
        "db_types": ["host"],
        "enabled": True,
        "schedule": "manual",
        "categories": HOST_OSCHECK_CATEGORIES,
        "item_config": HOST_OSCHECK_ITEMS,
        "threshold_config": {
            "cpu_pct": 85,
            "memory_pct": 85,
            "swap_pct": 50,
            "filesystem_pct": 90,
            "critical_filesystem_pct": 95,
            "inode_pct": 90,
            "load_per_cpu": 1.5,
        },
        "action_config": ["create_ticket", "generate_action_plan", "export_report", "readonly_fallback"],
        "report_config": {
            "include_summary": True,
            "include_findings": True,
            "include_actions": True,
            "include_raw_data": True,
            "script_name": "oscheck.sh",
            "outputs": ["oscheck text", "dbcheck html", "tar.gz archive"],
        },
        "remark": "基于 oscheck.sh 的主机巡检模板，覆盖 Linux/AIX/HP-UX 主机资源、Oracle 运行上下文、监听、补丁和 RAC 集群检查项。",
        "read_only": True,
    },
]


# Keep the old key for stored schedules and API clients; it is not a fourth variant.
from .native_catalog import CATALOG_REVISION, item_digest, oracle_native_items

_oracle_base = DEFAULT_POLICIES[0]
_oracle_variants = []
for _profile, _key in ORACLE_TEMPLATE_KEYS.items():
    _template = deepcopy(_oracle_base)
    _template.update(policy_key=_key, name=f"{ORACLE_PROFILE_LABELS[_profile]} 巡检模板")
    _template["item_config"] = oracle_native_items(_profile, ORACLE_STANDARD_ITEMS)
    _template["report_config"].update(
        oracle_profile=_profile,
        script_name=SCRIPT_CATALOG[_profile]["script_name"],
    )
    _template["remark"] = (
        f"{ORACLE_PROFILE_LABELS[_profile]} 专属巡检模板，"
        f"使用 {SCRIPT_CATALOG[_profile]['script_name']}，执行前验证版本及 CDB 架构。"
    )
    _oracle_variants.append(_template)
DEFAULT_POLICIES[1:1] = _oracle_variants
from .windows_host_checks import WINDOWS_HOST_ITEMS

_host_template = next(template for template in DEFAULT_POLICIES if template["policy_key"] == "host_resource")
_windows_template = deepcopy(_host_template)
_windows_template.update(
    policy_key="host_windows", name="主机巡检（Windows）", item_config=deepcopy(WINDOWS_HOST_ITEMS),
    remark="基于 oscheck.ps1 的只读 Windows 主机巡检，通过已配置的 SSH 和 PowerShell 采集。未适配的原脚本项目保留但默认停用，不执行外部程序或远程文件写入。",
)
_windows_template["report_config"].update(host_platform="windows", script_name="oscheck.ps1", outputs=["platform_report"])
_windows_template["threshold_config"] = {
    "cpu_pct": 80, "critical_cpu_pct": 90,
    "memory_pct": 80, "critical_memory_pct": 90,
    "swap_pct": 30, "critical_swap_pct": 60,
    "filesystem_pct": 80, "critical_filesystem_pct": 90,
}
_host_template["name"] = "主机巡检（Linux / AIX / HP-UX）"
_host_template["report_config"]["host_platform"] = "unix"
DEFAULT_POLICIES.append(_windows_template)
for _template in DEFAULT_POLICIES:
    _script = {
        "mysql_standard": "mysql_check.sh",
        "postgresql_standard": "pg_check.sh",
        "sqlserver_standard": "mssql_check.sh",
    }.get(_template["policy_key"])
    if _script:
        _template["report_config"]["script_name"] = _script
    _template["report_config"].update(
        builtin_catalog_revision=CATALOG_REVISION,
        builtin_items_digest=item_digest(_template["item_config"]),
    )


def _bool_int(value) -> int:
    return 1 if bool(value) else 0


def _assistant_default_db_types(item: DBInspectionPolicy) -> set[str]:
    report_config = item.report_config_json or {}
    configured = report_config.get("assistant_default_db_types")
    if isinstance(configured, list):
        candidates = configured
    elif report_config.get("assistant_default") is True:
        candidates = item.db_types_json or []
    else:
        candidates = []
    applicable = {str(value).strip().lower() for value in (item.db_types_json or [])}
    return {
        str(value).strip().lower()
        for value in candidates
        if str(value).strip().lower() in applicable
    }


def _enforce_unique_assistant_defaults(db: Session, item: DBInspectionPolicy) -> None:
    selected_db_types = _assistant_default_db_types(item)
    item_report_config = dict(item.report_config_json or {})
    item_report_config["assistant_default"] = bool(selected_db_types)
    item_report_config["assistant_default_db_types"] = sorted(selected_db_types)
    item.report_config_json = item_report_config
    if not selected_db_types:
        return

    for other in db.query(DBInspectionPolicy).filter(DBInspectionPolicy.id != item.id).all():
        other_defaults = _assistant_default_db_types(other)
        overlapping = set(selected_db_types)
        profile = item_report_config.get("oracle_profile") or "auto"
        other_profile = (other.report_config_json or {}).get("oracle_profile") or "auto"
        if profile != "auto" and other_profile != "auto" and profile != other_profile:
            overlapping.discard("oracle")
        host_profile = item_report_config.get("host_platform") or "auto"
        other_host_profile = (other.report_config_json or {}).get("host_platform") or "auto"
        if host_profile != "auto" and other_host_profile != "auto" and host_profile != other_host_profile:
            overlapping.discard("host")
        remaining = other_defaults - overlapping
        if remaining == other_defaults:
            continue
        report_config = dict(other.report_config_json or {})
        report_config["assistant_default"] = bool(remaining)
        report_config["assistant_default_db_types"] = sorted(remaining)
        other.report_config_json = report_config


def _row(item: DBInspectionPolicy) -> dict:
    from . import template_runtime

    report_config = item.report_config_json or {}
    assistant_default_db_types = sorted(_assistant_default_db_types(item))
    schedule_meta = parse_schedule(item.schedule)
    next_run_time = compute_next_run_time(item.schedule, report_config.get("last_run_at"))
    runtime = template_runtime.inspect_template_runtime(
        item.db_types_json or [],
        item.item_config_json or [],
        report_config,
    )
    return {
        "id": item.id,
        "key": item.policy_key,
        "policy_key": item.policy_key,
        "legacy_template": item.policy_key == "oracle_standard" and bool(item.read_only),
        "name": item.name,
        "db_types": item.db_types_json or [],
        "enabled": bool(item.enabled),
        "schedule": item.schedule,
        "schedule_type": schedule_meta["type"],
        "schedule_day": schedule_meta.get("day"),
        "schedule_days": schedule_meta.get("days") or [],
        "schedule_month": schedule_meta.get("month"),
        "schedule_time": schedule_meta.get("time"),
        "categories": item.categories_json or [],
        "item_config": item.item_config_json or [],
        "runtime_valid": runtime["valid"],
        "runtime_errors": runtime["errors"],
        "runtime_summary": runtime,
        "executable_item_count": runtime["executable_item_count"],
        "threshold_config": item.threshold_config_json or {},
        "action_config": item.action_config_json or [],
        "report_config": report_config,
        "assistant_default": bool(assistant_default_db_types),
        "assistant_default_db_types": assistant_default_db_types,
        "instance_ids": report_config.get("instance_ids") or [],
        "last_run_at": report_config.get("last_run_at"),
        "last_status": report_config.get("last_status"),
        "last_message": report_config.get("last_message"),
        "next_run_time": _time(next_run_time),
        "read_only": bool(item.read_only),
        "remark": item.remark or "",
        "create_time": _time(item.create_time),
        "update_time": _time(item.update_time),
    }


def _time(value: datetime | None) -> str | None:
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def ensure_default_policies(db: Session, *, commit: bool = True) -> None:
    from . import template_runtime

    existing = {row.policy_key: row for row in db.query(DBInspectionPolicy).all()}
    changed = False
    for template in DEFAULT_POLICIES:
        template_runtime.require_template_runtime(
            template.get("db_types") or [],
            template.get("item_config") or [],
            template.get("report_config") or {},
        )
        current = existing.get(template["policy_key"])
        if current:
            # Registry refresh must not reset customer thresholds, items or schedules.
            config = dict(current.report_config_json or {})
            if current.read_only and current.policy_key == "host_resource" and current.name == "主机资源巡检":
                current.name = template["name"]
                changed = True
            target_digest = item_digest(template["item_config"])
            current_digest = item_digest(current.item_config_json or [])
            known_digest = config.get("builtin_items_digest")
            legacy_oracle = template["policy_key"] in ORACLE_TEMPLATE_KEYS.values() and current_digest == item_digest(ORACLE_STANDARD_ITEMS)
            # Only replace a known factory item list, never a customer's changed list.
            if current.read_only and (current_digest == known_digest or legacy_oracle or current_digest == target_digest):
                if current_digest != target_digest:
                    current.item_config_json = deepcopy(template["item_config"])
                    changed = True
                config.update(builtin_catalog_revision=CATALOG_REVISION, builtin_items_digest=target_digest)
            if current.read_only and config.get("script_source") == "E:/chromedownload/oscheck (1).sh":
                config.pop("script_source")
            for field in ("oracle_profile", "script_name", "host_platform"):
                value = (template.get("report_config") or {}).get(field)
                if current.read_only and value is not None and field not in config:
                    config[field] = value
            if config != (current.report_config_json or {}):
                current.report_config_json = config
                changed = True
            continue
        db.add(_make_policy(template))
        changed = True
    if changed and commit:
        db.commit()


def _make_policy(payload: dict) -> DBInspectionPolicy:
    item = DBInspectionPolicy(policy_key=payload["policy_key"])
    _apply_policy_payload(item, payload, keep_enabled=False)
    return item


def _apply_policy_payload(item: DBInspectionPolicy, payload: dict, keep_enabled: bool = False) -> None:
    payload = _normalize_payload(payload, item.report_config_json if item.id else None)
    item.name = payload["name"]
    item.db_types_json = payload.get("db_types") or []
    if not keep_enabled:
        item.enabled = _bool_int(payload.get("enabled", True))
    item.schedule = payload.get("schedule")
    item.categories_json = payload.get("categories") or []
    item.item_config_json = payload.get("item_config") or []
    item.threshold_config_json = payload.get("threshold_config") or {}
    item.action_config_json = payload.get("action_config") or []
    item.report_config_json = payload.get("report_config") or {}
    item.read_only = _bool_int(payload.get("read_only", False))
    item.remark = payload.get("remark")


def list_policies(
    db: Session, db_type: str | None = None, enabled: bool | None = None,
    *, initialize_defaults: bool = True,
) -> list[dict]:
    if initialize_defaults:
        ensure_default_policies(db)
    query = db.query(DBInspectionPolicy)
    if enabled is not None:
        query = query.filter(DBInspectionPolicy.enabled == _bool_int(enabled))
    items = query.order_by(DBInspectionPolicy.id.asc()).all()
    if db_type:
        items = [item for item in items if db_type in (item.db_types_json or [])]
    return [_row(item) for item in items]


def get_policy(db: Session, key_or_id: str) -> dict | None:
    ensure_default_policies(db)
    query = db.query(DBInspectionPolicy)
    item = None
    if key_or_id.isdigit():
        item = query.filter(DBInspectionPolicy.id == int(key_or_id)).first()
    if not item:
        item = query.filter(DBInspectionPolicy.policy_key == key_or_id).first()
    return _row(item) if item else None


def get_policy_model(db: Session, policy_id: int) -> DBInspectionPolicy | None:
    ensure_default_policies(db)
    return db.query(DBInspectionPolicy).filter(DBInspectionPolicy.id == policy_id).first()


def get_policy_model_by_key(db: Session, policy_key: str) -> DBInspectionPolicy | None:
    ensure_default_policies(db)
    return (
        db.query(DBInspectionPolicy)
        .filter(DBInspectionPolicy.policy_key == str(policy_key or "").strip())
        .populate_existing()
        .first()
    )


def _capture_history(db: Session, item, **kwargs) -> None:
    """Record a version row when the history table exists, and only warn when it does not.

    Every mutation path funnels through here so the degradation rule lives in one
    place. History is optional infrastructure: a lost row is recoverable by
    re-saving, whereas a refused write leaves the operator with no way forward.
    """
    from . import template_versions

    if template_versions.version_history_available(db):
        template_versions.capture_version(db, item, **kwargs)
        return
    _LOGGER.warning(
        "inspection version history unavailable; change applied without a version row "
        "| policy_id=%s operation=%s",
        item.id,
        kwargs.get("operation"),
    )


def create_policy(db: Session, payload: dict) -> dict:
    from . import template_runtime

    payload = _normalize_payload(payload)
    if _bool_int(payload.get("enabled", True)):
        template_runtime.require_template_runtime(
            payload.get("db_types") or [],
            payload.get("item_config") or [],
            payload.get("report_config") or {},
        )
    item = _make_policy(payload)
    db.add(item)
    db.flush()
    _enforce_unique_assistant_defaults(db, item)
    _capture_history(db, item, operation="create")
    db.commit()
    db.refresh(item)
    return _row(item)


def update_policy(
    db: Session,
    item: DBInspectionPolicy,
    payload: dict,
    *,
    expected_revision: int | None = None,
    actor=None,
    operation: str = "update",
    source_policy_id: int | None = None,
    source_revision: int | None = None,
) -> dict:
    """Apply a payload to a policy and record it in the version history.

    ``expected_revision`` opts into optimistic concurrency: a caller restoring a
    historical version needs proof the template has not moved since it was read,
    which plain "last writer wins" would silently discard. ``actor`` and
    ``source_*`` are provenance carried into the captured version row.
    """
    from . import template_runtime

    if expected_revision is not None:
        from . import template_versions

        # Re-reads and refresh the row under SQLite's serialized write path; also
        # raises when the caller's observed revision is no longer current.
        item = template_versions.lock_policy(db, item.id, expected_revision=expected_revision)
    payload = _normalize_payload(payload, item.report_config_json or {})
    if "item_config" in payload or "db_types" in payload:
        from . import custom_metric_service

        custom_metric_service.validate_metric_db_types(
            payload.get("item_config", item.item_config_json or []),
            payload.get("db_types", item.db_types_json or []),
        )
    candidate_enabled = _bool_int(payload.get("enabled", bool(item.enabled)))
    candidate_db_types = payload.get("db_types", item.db_types_json or [])
    candidate_items = payload.get("item_config", item.item_config_json or [])
    from .template_profiles import normalize_profile_config

    candidate_report = normalize_profile_config(
        candidate_db_types, payload.get("report_config", item.report_config_json or {}),
    )
    if "report_config" in payload:
        payload["report_config"] = candidate_report
    if candidate_enabled:
        template_runtime.require_template_runtime(
            candidate_db_types,
            candidate_items,
            candidate_report,
        )
    for source, target in [
        ("name", "name"),
        ("schedule", "schedule"),
        ("remark", "remark"),
    ]:
        if source in payload:
            setattr(item, target, payload[source])
    if "policy_key" in payload and not item.read_only:
        item.policy_key = payload["policy_key"]
    if "db_types" in payload:
        item.db_types_json = payload.get("db_types") or []
    if "enabled" in payload:
        item.enabled = _bool_int(payload.get("enabled"))
    if "categories" in payload:
        item.categories_json = payload.get("categories") or []
    if "item_config" in payload:
        item.item_config_json = payload.get("item_config") or []
    if "threshold_config" in payload:
        item.threshold_config_json = payload.get("threshold_config") or {}
    if "action_config" in payload:
        item.action_config_json = payload.get("action_config") or []
    if "report_config" in payload:
        item.report_config_json = payload.get("report_config") or {}
    if "report_config" in payload or "db_types" in payload:
        _enforce_unique_assistant_defaults(db, item)
    # Capture before commit: the snapshot belongs to this change, and history is
    # useless if the row can be written without it. Stays inside this session's
    # existing commit boundary, so a failure rolls back both.
    _capture_history(
        db,
        item,
        operation=operation,
        actor=actor,
        source_policy_id=source_policy_id,
        source_revision=source_revision,
    )
    db.commit()
    db.refresh(item)
    return _row(item)


def clone_policy(db: Session, item: DBInspectionPolicy, policy_key: str, name: str) -> dict:
    from . import custom_metric_service, template_runtime

    report_config = dict(item.report_config_json or {})
    report_config.pop("last_run_at", None)
    report_config.pop("last_status", None)
    report_config.pop("last_message", None)
    report_config.pop("assistant_default", None)
    report_config.pop("assistant_default_db_types", None)
    template_runtime.require_template_runtime(
        item.db_types_json or [],
        item.item_config_json or [],
        report_config,
    )
    clone = DBInspectionPolicy(
        policy_key=policy_key,
        name=name,
        db_types_json=item.db_types_json or [],
        enabled=1,
        schedule=item.schedule,
        categories_json=item.categories_json or [],
        item_config_json=custom_metric_service.normalize_item_configs(item.item_config_json or []),
        threshold_config_json=item.threshold_config_json or {},
        action_config_json=item.action_config_json or [],
        report_config_json=report_config,
        read_only=0,
        remark=f"Cloned from {item.policy_key}",
    )
    db.add(clone)
    db.flush()
    # Provenance: the clone's first version row names the template it came from,
    # so "where did this template come from" is answerable from history alone.
    from . import template_versions

    source_revision = (
        template_versions.current_revision(db, item.id)
        if template_versions.version_history_available(db) else None
    )
    _capture_history(
        db,
        clone,
        operation="clone",
        source_policy_id=item.id,
        source_revision=source_revision,
    )
    db.commit()
    db.refresh(clone)
    return _row(clone)


def delete_policy(db: Session, item: DBInspectionPolicy) -> None:
    """Delete the live template while leaving a tombstone in the retained history.

    The version table has no foreign key to the policy table by design, so history
    outlives the row it describes. Without a tombstone the last revision simply
    looks like the current state of a template that no longer exists.
    """
    _capture_history(db, item, operation="delete")
    db.delete(item)
    db.commit()


def _normalize_payload(payload: dict, existing_report_config: dict | None = None) -> dict:
    from . import custom_metric_service

    payload = dict(payload or {})
    if "item_config" in payload:
        payload["item_config"] = custom_metric_service.normalize_item_configs(payload.get("item_config") or [])
        if "db_types" in payload:
            custom_metric_service.validate_metric_db_types(
                payload["item_config"],
                payload.get("db_types") or [],
            )
    if (
        "schedule_type" in payload
        or "schedule_day" in payload
        or "schedule_days" in payload
        or "schedule_month" in payload
        or "schedule_time" in payload
    ):
        payload["schedule"] = format_schedule(
            payload.get("schedule_type") or "manual",
            payload.get("schedule_days") if "schedule_days" in payload else payload.get("schedule_day"),
            payload.get("schedule_time") or "02:00",
            month=payload.get("schedule_month"),
        )
    if "instance_ids" in payload:
        report_config = dict(
            (payload.get("report_config") or {})
            if "report_config" in payload else (existing_report_config or {})
        )
        for field in ("last_run_at", "last_status", "last_message"):
            if field in (existing_report_config or {}):
                report_config[field] = existing_report_config[field]
        report_config["instance_ids"] = [
            int(value) for value in (payload.get("instance_ids") or []) if str(value).strip()
        ]
        payload["report_config"] = report_config
    if "report_config" in payload and "db_types" in payload:
        from .template_profiles import normalize_profile_config

        payload["report_config"] = normalize_profile_config(
            payload["db_types"], payload["report_config"],
        )
    return payload


def parse_schedule(schedule: str | None) -> dict:
    text = (schedule or "manual").strip().lower()
    if text in ("", "manual", "manual_or_daily"):
        return {"type": "manual", "time": "02:00", "day": None, "days": []}
    parts = text.split(":")
    if parts[0] == "daily":
        return {"type": "daily", "time": _normalize_time(":".join(parts[1:]) or "02:00"), "day": None, "days": []}
    if parts[0] == "weekdays":
        return {"type": "weekdays", "time": _normalize_time(":".join(parts[1:]) or "02:00"), "day": None, "days": [1, 2, 3, 4, 5]}
    if parts[0] in ("weekly", "monthly"):
        max_day = 7 if parts[0] == "weekly" else 31
        days = _normalize_day_list(parts[1] if len(parts) > 1 else None, 1, max_day, [1])
        time_value = ":".join(parts[2:]) if len(parts) > 2 else "02:00"
        return {"type": parts[0], "day": days[0], "days": days, "time": _normalize_time(time_value)}
    if parts[0] == "quarterly":
        month = min(max(_safe_int(parts[1] if len(parts) > 1 else None, 1), 1), 3)
        days = _normalize_day_list(parts[2] if len(parts) > 2 else None, 1, 31, [1])
        time_value = ":".join(parts[3:]) if len(parts) > 3 else "02:00"
        return {"type": "quarterly", "month": month, "day": days[0], "days": days, "time": _normalize_time(time_value)}
    return {"type": "manual", "time": "02:00", "day": None, "days": []}


def format_schedule(schedule_type: str, day: int | list[int] | str | None = None, time_value: str | None = None, *, month: int | None = None) -> str:
    kind = (schedule_type or "manual").strip().lower()
    scheduled_time = _normalize_time(time_value or "02:00")
    if kind == "daily":
        return f"daily:{scheduled_time}"
    if kind == "weekdays":
        return f"weekdays:{scheduled_time}"
    if kind == "weekly":
        days = _normalize_day_list(day, 1, 7, [1])
        return f"weekly:{','.join(str(value) for value in days)}:{scheduled_time}"
    if kind == "monthly":
        days = _normalize_day_list(day, 1, 31, [1])
        return f"monthly:{','.join(str(value) for value in days)}:{scheduled_time}"
    if kind == "quarterly":
        schedule_month = min(max(_safe_int(month, 1), 1), 3)
        days = _normalize_day_list(day, 1, 31, [1])
        return f"quarterly:{schedule_month}:{','.join(str(value) for value in days)}:{scheduled_time}"
    return "manual"


def compute_next_run_time(schedule: str | None, last_run_at: str | None = None, now: datetime | None = None) -> datetime | None:
    meta = parse_schedule(schedule)
    now = now or datetime.now()
    if meta["type"] == "manual":
        return None
    candidate = _candidate_for_period(meta, now)
    last_run = _parse_datetime(last_run_at)
    if candidate <= now and _same_schedule_period(meta, candidate, last_run):
        return _next_candidate_after(meta, candidate)
    return candidate


def is_policy_due(policy: DBInspectionPolicy, now: datetime | None = None) -> bool:
    if not policy.enabled:
        return False
    meta = parse_schedule(policy.schedule)
    if meta["type"] == "manual":
        return False
    now = now or datetime.now()
    candidate = _candidate_for_period(meta, now)
    if now < candidate:
        return False
    last_run = _parse_datetime((policy.report_config_json or {}).get("last_run_at"))
    return not _same_schedule_period(meta, candidate, last_run)


def mark_policy_run(policy: DBInspectionPolicy, status: str, message: str, run_at: datetime | None = None) -> None:
    report_config = dict(policy.report_config_json or {})
    report_config["last_run_at"] = (run_at or datetime.now()).isoformat(sep=" ", timespec="seconds")
    report_config["last_status"] = status
    report_config["last_message"] = message[:500]
    policy.report_config_json = report_config


def _candidate_for_period(meta: dict, anchor: datetime) -> datetime:
    target_time = _parse_time(meta.get("time"))
    if meta["type"] == "daily":
        return datetime.combine(anchor.date(), target_time)
    if meta["type"] in ("weekdays", "weekly"):
        days = meta.get("days") or ([1, 2, 3, 4, 5] if meta["type"] == "weekdays" else [1])
        return _next_week_candidate(anchor, days, target_time)
    if meta["type"] == "monthly":
        return _next_month_candidate(anchor, meta.get("days") or [1], target_time)
    if meta["type"] == "quarterly":
        return _next_quarter_candidate(anchor, meta.get("month") or 1, meta.get("days") or [1], target_time)
    return datetime.combine(anchor.date(), target_time)


def _same_schedule_period(meta: dict, candidate: datetime, last_run: datetime | None) -> bool:
    if not last_run:
        return False
    if meta["type"] in ("daily", "weekdays", "weekly", "monthly", "quarterly"):
        return last_run.date() == candidate.date()
    return False


def _next_candidate_after(meta: dict, candidate: datetime) -> datetime:
    return _candidate_for_period(meta, candidate + timedelta(days=1))


def _next_week_candidate(anchor: datetime, days: list[int], target_time: time) -> datetime:
    schedule_days = _normalize_day_list(days, 1, 7, [1])
    for offset in range(0, 15):
        day = anchor.date() + timedelta(days=offset)
        if day.isoweekday() not in schedule_days:
            continue
        candidate = datetime.combine(day, target_time)
        if candidate >= anchor or day == anchor.date():
            return candidate
    return datetime.combine(anchor.date() + timedelta(days=7), target_time)


def _next_month_candidate(anchor: datetime, days: list[int], target_time: time) -> datetime:
    schedule_days = _normalize_day_list(days, 1, 31, [1])
    year, month = anchor.year, anchor.month
    for month_offset in range(0, 15):
        current_month = month + month_offset
        current_year = year + ((current_month - 1) // 12)
        current_month = ((current_month - 1) % 12) + 1
        last_day = calendar.monthrange(current_year, current_month)[1]
        for day_value in schedule_days:
            day = min(day_value, last_day)
            candidate = datetime(current_year, current_month, day, target_time.hour, target_time.minute)
            if candidate >= anchor or candidate.date() == anchor.date():
                return candidate
    return datetime(anchor.year, anchor.month, 1, target_time.hour, target_time.minute)


def _next_quarter_candidate(anchor: datetime, month: int, days: list[int], target_time: time) -> datetime:
    quarter_month = min(max(_safe_int(month, 1), 1), 3)
    schedule_days = _normalize_day_list(days, 1, 31, [1])
    quarter_start = ((anchor.month - 1) // 3) * 3 + 1
    for quarter_offset in range(0, 12):
        base_month = quarter_start + quarter_offset * 3
        current_year = anchor.year + ((base_month - 1) // 12)
        current_month = ((base_month - 1) % 12) + quarter_month
        if current_month > 12:
            current_month -= 12
            current_year += 1
        last_day = calendar.monthrange(current_year, current_month)[1]
        for day_value in schedule_days:
            day = min(day_value, last_day)
            candidate = datetime(current_year, current_month, day, target_time.hour, target_time.minute)
            if candidate >= anchor or candidate.date() == anchor.date():
                return candidate
    return datetime(anchor.year, anchor.month, 1, target_time.hour, target_time.minute)


def _normalize_day_list(value, minimum: int, maximum: int, default: list[int]) -> list[int]:
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    elif isinstance(value, str) and "," in value:
        raw_values = value.split(",")
    elif value is None or value == "":
        raw_values = default
    else:
        raw_values = [value]
    days = sorted({
        min(max(_safe_int(raw, default[0]), minimum), maximum)
        for raw in raw_values
        if str(raw).strip()
    })
    return days or list(default)


def _normalize_time(value: str) -> str:
    parsed = _parse_time(value)
    return f"{parsed.hour:02d}:{parsed.minute:02d}"


def _parse_time(value: str | None) -> time:
    text = str(value or "02:00").strip()
    try:
        hour, minute = text.split(":", 1)
        return time(hour=min(max(int(hour), 0), 23), minute=min(max(int(minute), 0), 59))
    except Exception:
        return time(hour=2, minute=0)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("T", " ")
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default
