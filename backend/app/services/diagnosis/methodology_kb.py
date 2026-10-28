"""方法论知识库：CRUD + 匹配 + 种子灌入。"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.diagnosis_center import DiagMethodology
from app.services.diagnosis import constants as C

logger = logging.getLogger("app.services.diagnosis.methodology")


# 种子方法论（builtin）。覆盖 3.0 设计点名的经典案例。
SEED_METHODOLOGIES: list[dict] = [
    {
        "code": "MED_FRA_FULL",
        "title": "FRA / 归档空间满",
        "symptom": "数据库 hang、ORA-00257，业务写入卡住，归档进程停止。",
        "layer": C.LAYER_STORAGE,
        "applicable_arch": ["单机", "RAC", "DataGuard"],
        "entry_point": "v$recovery_file_dest / v$flash_recovery_area_usage 使用率",
        "troubleshoot_path": ["确认 db_recovery_file_dest_size 与实际占用", "区分归档/闪回日志/备份集占比", "确认归档是否已备份", "评估清理或扩容"],
        "check_sql": "SELECT * FROM v$flash_recovery_area_usage;",
        "check_command": "df -h <FRA_MOUNT>",
        "judge_logic": "FRA 使用率 > 90% 且归档占比高 → 归档积压；已备份可清理，未备份需先备份。",
        "solution": "先 BACKUP ARCHIVELOG，再 CROSSCHECK + DELETE EXPIRED；必要时扩 db_recovery_file_dest_size。",
        "risk": "未备份直接删除归档会破坏恢复链；扩容需确认文件系统剩余。",
        "review_note": "建立归档备份与清理定时任务，FRA 使用率纳入告警基线。",
        "keywords": ["FRA", "归档", "ORA-00257", "archive", "recovery area"],
        "prior_prob": 0.75,
        "tags": ["空间", "归档"],
    },
    {
        "code": "MED_TABLESPACE_FULL",
        "title": "表空间使用率满",
        "symptom": "ORA-01653/ORA-01688 无法扩展段，表空间使用率接近或达到 100%，业务写入失败。",
        "layer": C.LAYER_STORAGE,
        "applicable_arch": ["单机", "RAC", "DataGuard"],
        "entry_point": "dba_tablespace_usage_metrics / dba_data_files 使用率与自动扩展",
        "troubleshoot_path": ["定位高使用率表空间", "确认数据文件 autoextend 与 maxsize", "确认底层文件系统/ASM 剩余", "评估对象增长来源与历史数据归档"],
        "check_sql": "SELECT tablespace_name, ROUND(used_percent,2) used_pct FROM dba_tablespace_usage_metrics ORDER BY used_percent DESC;",
        "check_command": "df -h <DATAFILE_MOUNT>",
        "judge_logic": "表空间 used_pct ≥ 95% 且 autoextend 受限或文件系统满 → 需扩容或清理；持续增长则需归档治理。",
        "solution": "在确认文件系统/ASM 剩余后，ADD DATAFILE 或开启/调大 AUTOEXTEND MAXSIZE；长期通过历史数据归档控制增长。",
        "risk": "扩容前必须确认底层剩余空间；大表清理需先备份/校验。",
        "review_note": "表空间使用率纳入容量基线与预测告警。",
        "keywords": ["表空间", "tablespace", "used_pct", "ORA-01653", "ORA-01688", "使用率"],
        "prior_prob": 0.7,
        "tags": ["空间", "表空间"],
    },
    {
        "code": "MED_TEMP_BLOWUP",
        "title": "TEMP 表空间暴涨",
        "symptom": "ORA-01652 无法扩展 TEMP 段，大查询失败，TEMP 使用率飙升。",
        "layer": C.LAYER_SQL,
        "applicable_arch": ["单机", "RAC"],
        "entry_point": "v$tempseg_usage / v$sort_usage 定位占用会话与 SQL",
        "troubleshoot_path": ["定位占用 TEMP 最多的 SQL_ID 与会话", "检查执行计划是否大排序/哈希溢出", "评估统计信息与索引", "确认 PGA / workarea 配置"],
        "check_sql": "SELECT u.tablespace, s.sid, s.sql_id, u.blocks*8/1024 mb FROM v$tempseg_usage u JOIN v$session s ON u.session_addr=s.saddr ORDER BY u.blocks DESC;",
        "judge_logic": "单一 SQL 长期占用大量 TEMP → 执行计划劣化/缺索引；多会话并发 → 业务高峰排序压力。",
        "solution": "优化 SQL 走索引避免大排序；必要时增大 TEMP 或调整 PGA_AGGREGATE_TARGET。",
        "risk": "盲目扩 TEMP 掩盖 SQL 问题；杀会话影响业务。",
        "review_note": "对高 TEMP 消耗 SQL 建立画像与基线告警。",
        "keywords": ["TEMP", "ORA-01652", "direct path read temp", "排序", "sort"],
        "prior_prob": 0.6,
        "tags": ["SQL", "TEMP"],
    },
    {
        "code": "MED_LOG_FILE_SYNC",
        "title": "log file sync 等待高",
        "symptom": "commit 缓慢，TPS 下降，AWR Top Event 为 log file sync。",
        "layer": C.LAYER_STORAGE,
        "applicable_arch": ["单机", "RAC", "DataGuard"],
        "entry_point": "AWR 等待事件 + LGWR 写时延（log file parallel write）",
        "troubleshoot_path": ["对比 log file sync 与 log file parallel write 平均时延", "检查 redo 所在存储 await", "评估提交频率（频繁小事务）", "DG 场景检查 LNS/网络"],
        "check_sql": "SELECT event, total_waits, time_waited_micro/total_waits avg_us FROM v$system_event WHERE event LIKE 'log file%';",
        "check_command": "iostat -x 1 5  # 关注 redo 盘 await",
        "judge_logic": "parallel write 高 → 存储慢；sync 高但 parallel write 低 → CPU 调度/提交过频；DG 同步影响。",
        "solution": "redo 移到低延迟存储；合并小事务；评估 commit_write/NOLOGGING（谨慎）；DG 调整保护模式。",
        "risk": "改提交方式涉及业务一致性；存储调整需维护窗口。",
        "review_note": "redo 写时延纳入存储基线，提交频率纳入应用规范评审。",
        "keywords": ["log file sync", "log file parallel write", "redo", "commit"],
        "prior_prob": 0.7,
        "tags": ["存储", "Redo"],
    },
    {
        "code": "MED_DBFILE_SEQ_READ",
        "title": "db file sequential read 单块读延迟高",
        "symptom": "AWR Top Event 为 db file sequential read，索引读慢，OLTP 响应变长。",
        "layer": C.LAYER_STORAGE,
        "applicable_arch": ["单机", "RAC", "DataGuard"],
        "entry_point": "AWR 等待事件 + 存储 await/IOPS + Top SQL 物理读",
        "troubleshoot_path": ["确认 db file sequential read 平均时延与占比", "对照存储 await/队列深度", "定位高物理读 SQL 与索引", "评估缓冲池命中率与热块"],
        "check_sql": "SELECT event, time_waited_micro/GREATEST(total_waits,1) avg_us FROM v$system_event WHERE event='db file sequential read';",
        "check_command": "iostat -x 1 5  # 关注数据盘 await/avgqu-sz",
        "judge_logic": "单块读平均时延高且存储 await 高 → 存储慢；时延正常但次数极多 → SQL 物理读过多（缺索引/计划差）。",
        "solution": "存储侧优化慢盘/链路；SQL 侧减少物理读（索引/计划/缓冲池）；评估热点对象。",
        "risk": "存储调整需维护窗口；加索引影响 DML。",
        "review_note": "单块读时延与物理读 Top SQL 纳入基线。",
        "keywords": ["db file sequential read", "单块读", "物理读", "buffer cache", "索引读"],
        "prior_prob": 0.6,
        "tags": ["存储", "IO"],
    },
    {
        "code": "MED_RAC_CACHE_FUSION",
        "title": "RAC Cache Fusion 延迟 / gc 等待",
        "symptom": "RAC 多节点性能抖动，AWR 出现 gc cr request / gc buffer busy 高等待。",
        "layer": C.LAYER_NETWORK,
        "applicable_arch": ["RAC"],
        "entry_point": "gv$ 全局等待 + 私网（Interconnect）延迟与丢包",
        "troubleshoot_path": ["确认 gc 等待平均时延", "检查私网网卡错误包/丢包/MTU", "排查热块与右增长索引争用", "评估节点负载是否倾斜"],
        "check_sql": "SELECT inst_id, event, time_waited_micro/GREATEST(total_waits,1) avg_us FROM gv$system_event WHERE event LIKE 'gc %' ORDER BY 3 DESC;",
        "check_command": "ping -s 8192 <priv_ip>; netstat -i  # 关注私网丢包/错误包",
        "judge_logic": "gc 平均时延高且私网有丢包/错误包 → 互联问题；无网络异常但热块多 → 应用/索引争用。",
        "solution": "修复私网（网卡/交换机/MTU/HAIP）；热块用反向键索引/分区缓解；均衡节点负载。",
        "risk": "私网调整可能触发节点重配；索引改造需评估。",
        "review_note": "私网质量纳入巡检；热块对象登记到方法论库。",
        "keywords": ["gc cr request", "gc buffer busy", "cache fusion", "interconnect", "私网", "RAC"],
        "prior_prob": 0.65,
        "tags": ["网络", "RAC"],
    },
    {
        "code": "MED_DG_LAG",
        "title": "Data Guard 应用 / 传输延迟",
        "symptom": "备库落后，apply lag / transport lag 增大，切换 RPO 风险。",
        "layer": C.LAYER_DB,
        "applicable_arch": ["DataGuard"],
        "entry_point": "v$dataguard_stats / v$archive_gap",
        "troubleshoot_path": ["查看 transport lag 与 apply lag", "检查归档 gap", "评估网络带宽与 LNS", "检查备库 MRP 与资源"],
        "check_sql": "SELECT name, value FROM v$dataguard_stats; SELECT * FROM v$archive_gap;",
        "judge_logic": "transport lag 大 → 网络/主库 LNS；apply lag 大但 transport 正常 → 备库 MRP/IO/资源。",
        "solution": "修复网络/带宽；补 gap；提升备库应用并行（parallel apply）与 IO。",
        "risk": "调整保护模式影响主库提交；补 gap 需归档完整。",
        "review_note": "DG 延迟纳入告警，定期演练切换。",
        "keywords": ["Data Guard", "apply lag", "transport lag", "archive gap", "MRP", "备库"],
        "prior_prob": 0.6,
        "tags": ["高可用", "DataGuard"],
    },
    {
        "code": "MED_ROW_LOCK",
        "title": "行锁争用 / 阻塞会话",
        "symptom": "业务接口卡顿、过站/报工超时，enq: TX - row lock contention 等待。",
        "layer": C.LAYER_DB,
        "applicable_arch": ["单机", "RAC"],
        "entry_point": "v$session.blocking_session 阻塞树",
        "troubleshoot_path": ["定位 blocker/waiter 与 SQL_ID", "确认长事务/未提交", "评估热点行与业务并发", "必要时与业务确认处置"],
        "check_sql": "SELECT sid, serial#, username, event, blocking_session, sql_id FROM v$session WHERE blocking_session IS NOT NULL;",
        "judge_logic": "存在持续 blocker 且 waiter 多 → 长事务/未提交或热点行并发。",
        "solution": "推动业务及时提交；优化并发更新热点（排队/拆分）；必要时人工结束阻塞源（需授权）。",
        "risk": "结束会话回滚影响业务；需业务确认。",
        "review_note": "热点表更新模式纳入应用评审。",
        "keywords": ["锁", "阻塞", "row lock", "enq: tx", "blocking"],
        "prior_prob": 0.55,
        "tags": ["事务", "锁"],
    },
    {
        "code": "MED_UNDO_REUSE",
        "title": "UNDO 无法复用 / ORA-01555",
        "symptom": "长查询报 ORA-01555 snapshot too old，UNDO 争用。",
        "layer": C.LAYER_DB,
        "applicable_arch": ["单机", "RAC"],
        "entry_point": "v$undostat / undo_retention 与长查询时长",
        "troubleshoot_path": ["查看 maxquerylen 与 retention", "评估 UNDO 表空间大小", "定位长查询 SQL", "评估高 DML 与查询并发窗口"],
        "check_sql": "SELECT begin_time, maxquerylen, tuned_undoretention, ssolderrcnt, nospaceerrcnt FROM v$undostat ORDER BY begin_time DESC FETCH FIRST 24 ROWS ONLY;",
        "judge_logic": "maxquerylen > tuned_undoretention 且 ssolderrcnt 增长 → UNDO 保留不足或空间小。",
        "solution": "增大 UNDO 表空间 / undo_retention；错峰长查询；评估 retention guarantee（谨慎）。",
        "risk": "retention guarantee 可能导致 DML 失败；扩容需空间。",
        "review_note": "长查询窗口与 UNDO 配置纳入容量评审。",
        "keywords": ["UNDO", "ORA-01555", "snapshot too old", "undo_retention"],
        "prior_prob": 0.5,
        "tags": ["事务", "UNDO"],
    },
    {
        "code": "MED_INVALID_OBJECTS",
        "title": "无效数据库对象",
        "symptom": "存在 INVALID 的 VIEW/PACKAGE/FUNCTION/TRIGGER，可能导致业务模块报错或编译失败。",
        "layer": C.LAYER_DB,
        "applicable_arch": ["单机", "RAC", "DataGuard"],
        "entry_point": "dba_objects status='INVALID' 定位无效对象与归属用户",
        "troubleshoot_path": ["列出无效对象与类型/owner", "分析失效来源（DDL/依赖变更/补丁）", "评估依赖链", "测试窗口重新编译并验证业务"],
        "check_sql": "SELECT owner, object_type, COUNT(*) FROM dba_objects WHERE status='INVALID' GROUP BY owner, object_type ORDER BY 3 DESC;",
        "judge_logic": "存在 INVALID 对象且与近期 DDL/发布相关 → 编译失效；核心业务对象失效优先处理。",
        "solution": "UTL_RECOMP.RECOMP_SERIAL 或 ALTER ... COMPILE 重新编译；确认依赖完整后验证业务。",
        "risk": "重新编译可能短暂锁定对象；核心对象需在窗口操作。",
        "review_note": "发布后纳入无效对象巡检，避免遗留失效对象。",
        "keywords": ["无效对象", "INVALID", "invalid_count", "无效", "编译", "失效"],
        "prior_prob": 0.45,
        "tags": ["对象", "数据库"],
    },
    {
        "code": "MED_STALE_STATS",
        "title": "统计信息过期",
        "symptom": "表/索引统计信息 STALE，优化器选错执行计划，SQL 性能抖动。",
        "layer": C.LAYER_SQL,
        "applicable_arch": ["单机", "RAC", "DataGuard"],
        "entry_point": "dba_tab_statistics.stale_stats='YES' 定位过期统计信息对象",
        "troubleshoot_path": ["列出 STALE 统计信息的大表/高频表", "确认自动统计任务是否正常", "评估 DML 量与采样", "低峰收集统计信息"],
        "check_sql": "SELECT owner, table_name, stale_stats, last_analyzed FROM dba_tab_statistics WHERE stale_stats='YES' ORDER BY last_analyzed;",
        "judge_logic": "高频/大表统计信息过期 + 执行计划变差 → 需收集统计信息；自动任务停用则恢复。",
        "solution": "DBMS_STATS.GATHER_TABLE_STATS（AUTO_SAMPLE_SIZE）在低峰收集；恢复自动统计任务。",
        "risk": "收集统计信息会触发游标失效与硬解析；大表收集耗资源，宜低峰。",
        "review_note": "把关键表统计信息新鲜度纳入巡检与基线。",
        "keywords": ["统计信息", "stale_stats", "stale", "过期", "last_analyzed"],
        "prior_prob": 0.5,
        "tags": ["SQL", "统计信息"],
    },
    {
        "code": "MED_HIGH_LOGICAL_READ",
        "title": "高逻辑读 / 全表扫描慢 SQL",
        "symptom": "SQL 逻辑读高、全表扫描多、执行计划差，CPU/缓冲区压力大，接口慢。",
        "layer": C.LAYER_SQL,
        "applicable_arch": ["单机", "RAC", "DataGuard"],
        "entry_point": "v$sql / AWR Top SQL 按 buffer_gets、executions 排序",
        "troubleshoot_path": ["定位高逻辑读/高频 SQL_ID", "查看执行计划是否全表扫描/索引失效", "评估谓词选择性与绑定变量", "评估索引/分区/SQL 改写"],
        "check_sql": "SELECT sql_id, buffer_gets, executions, buffer_gets/GREATEST(executions,1) per_exec FROM v$sql ORDER BY buffer_gets DESC FETCH FIRST 20 ROWS ONLY;",
        "judge_logic": "单条 SQL buffer_gets 占比高 + 全表扫描 → 缺索引/计划劣化；高频小查询累积 → 业务调用模式问题。",
        "solution": "补合适索引或改写 SQL 走索引；收集统计信息稳定计划；评估绑定变量与分区裁剪。",
        "risk": "加索引影响 DML 与空间；改写需测试验证计划与结果一致。",
        "review_note": "高逻辑读 SQL 纳入 SQL 画像与基线告警。",
        "keywords": ["逻辑读", "buffer_gets", "全表扫描", "执行计划", "慢 SQL", "高频"],
        "prior_prob": 0.5,
        "tags": ["SQL", "索引"],
    },
    {
        "code": "MED_CPU_SATURATION",
        "title": "主机 CPU 饱和 / 高负载",
        "symptom": "CPU 使用率高、run queue 长、load 高，会话等待 CPU，整体响应变慢。",
        "layer": C.LAYER_OS,
        "applicable_arch": ["单机", "RAC", "DataGuard"],
        "entry_point": "load/核 + CPU 使用率 + Top 进程/会话",
        "troubleshoot_path": ["确认 load 与逻辑核比", "区分 user/sys/iowait 占比", "定位高 CPU 进程与 SQL", "评估并行度与并发"],
        "check_command": "top -b -n1 | head -20; uptime; vmstat 1 3",
        "judge_logic": "load/核 > 1.5 且 user 高 → 计算瓶颈（多为低效 SQL/并行过高）；sys 高 → 系统调用/上下文切换。",
        "solution": "优化高 CPU SQL；降低不当并行；评估扩核或迁移负载。",
        "risk": "调整并行/迁移需评估业务影响。",
        "review_note": "CPU/load 纳入主机基线与告警。",
        "keywords": ["CPU", "load", "负载", "run queue", "cpu_util"],
        "prior_prob": 0.5,
        "tags": ["操作系统", "CPU"],
    },
    {
        "code": "MED_CONN_EXHAUST",
        "title": "连接数 / 会话耗尽",
        "symptom": "processes/sessions 接近上限，ORA-00018/连接池耗尽，新连接被拒。",
        "layer": C.LAYER_APP,
        "applicable_arch": ["单机", "RAC", "DataGuard"],
        "entry_point": "v$resource_limit（processes/sessions）+ 应用连接池配置",
        "troubleshoot_path": ["查看 processes/sessions 当前与上限", "定位会话来源（机器/程序）", "确认连接池泄漏或未释放", "评估上限与池配置"],
        "check_sql": "SELECT resource_name, current_utilization, max_utilization, limit_value FROM v$resource_limit WHERE resource_name IN ('processes','sessions');",
        "judge_logic": "current 接近 limit + 大量同源会话 → 连接池泄漏或并发激增；偶发峰值 → 上限偏小。",
        "solution": "修复应用连接池泄漏与超时回收；合理调大 processes/sessions（评估内存）。",
        "risk": "调大连接上限增加内存占用；需评估 PGA/SGA。",
        "review_note": "连接数水位纳入告警，连接池配置纳入应用评审。",
        "keywords": ["连接数", "连接池", "sessions", "processes", "ORA-00018", "会话数"],
        "prior_prob": 0.5,
        "tags": ["应用", "连接"],
    },
    {
        "code": "MED_SHARED_POOL",
        "title": "共享池不足 / ORA-04031 / 硬解析风暴",
        "symptom": "ORA-04031 无法分配共享池内存，硬解析高，library cache 争用。",
        "layer": C.LAYER_DB,
        "applicable_arch": ["单机", "RAC", "DataGuard"],
        "entry_point": "v$sgastat / v$librarycache + 硬解析比",
        "troubleshoot_path": ["查看 shared pool free 与碎片", "评估硬解析比与未绑定变量 SQL", "定位大对象/字面量 SQL", "评估 SGA/共享池配置"],
        "check_sql": "SELECT namespace, gets, gethits, reloads, invalidations FROM v$librarycache ORDER BY reloads DESC;",
        "judge_logic": "硬解析高 + 字面量 SQL 多 → 未用绑定变量；shared pool free 低 → 配置不足或碎片。",
        "solution": "推动绑定变量（或 cursor_sharing 评估）；合理增大共享池；处置大对象。",
        "risk": "cursor_sharing 改动影响计划；调 SGA 需重启或评估。",
        "review_note": "硬解析比与共享池水位纳入巡检。",
        "keywords": ["ORA-04031", "shared pool", "共享池", "硬解析", "library cache", "cursor"],
        "prior_prob": 0.5,
        "tags": ["数据库", "内存"],
    },
    {
        "code": "MED_ASM_FAULT",
        "title": "ASM 磁盘组故障 / 空间不足",
        "symptom": "ASM 磁盘组 offline/掉盘，DISK_REPAIR_TIME 倒计时，空间不足。",
        "layer": C.LAYER_STORAGE,
        "applicable_arch": ["RAC", "单机", "SAN"],
        "entry_point": "v$asm_diskgroup / v$asm_disk 状态与剩余",
        "troubleshoot_path": ["查看磁盘组 state 与 free_mb", "检查掉盘与 multipath", "评估 rebalance 进度", "确认底层 LUN/链路"],
        "check_sql": "SELECT name, state, total_mb, free_mb FROM v$asm_diskgroup;",
        "check_command": "multipath -ll; asmcmd lsdsk -k",
        "judge_logic": "磁盘 offline 且 multipath 缺路径 → 链路/LUN 故障；free_mb 低 → 容量问题。",
        "solution": "修复多路径/LUN 后 online 磁盘；扩盘并 rebalance；清理空间。",
        "risk": "rebalance 影响性能；加盘需与存储协同。",
        "review_note": "ASM 磁盘组与多路径状态纳入巡检与告警。",
        "keywords": ["ASM", "diskgroup", "multipath", "掉盘", "rebalance"],
        "prior_prob": 0.55,
        "tags": ["存储", "ASM"],
    },
]


def ensure_seed_methodologies(db: Session) -> None:
    """灌入/更新种子方法论（幂等，仿 ensure_default_diagnosis_rules）。"""
    existing = {row.code: row for row in db.query(DiagMethodology).all()}
    for item in SEED_METHODOLOGIES:
        row = existing.get(item["code"])
        if row:
            # 仅刷新 builtin 内容，不覆盖人工编辑过的 source
            if row.source == "builtin":
                for k, v in item.items():
                    setattr(row, k, v)
        else:
            db.add(DiagMethodology(**item, source="builtin", enabled=1))
    db.commit()


def to_dict(row: DiagMethodology) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "title": row.title,
        "symptom": row.symptom,
        "layer": row.layer,
        "applicable_arch": row.applicable_arch or [],
        "entry_point": row.entry_point,
        "troubleshoot_path": row.troubleshoot_path or [],
        "check_sql": row.check_sql,
        "check_command": row.check_command,
        "judge_logic": row.judge_logic,
        "solution": row.solution,
        "risk": row.risk,
        "review_note": row.review_note,
        "keywords": row.keywords or [],
        "prior_prob": row.prior_prob,
        "tags": row.tags or [],
        "source": row.source,
        "enabled": row.enabled,
        "create_time": row.create_time.isoformat(sep=" ") if row.create_time else None,
    }


def list_methodologies(db: Session, layer: str | None = None, keyword: str | None = None) -> list[dict]:
    q = db.query(DiagMethodology)
    if layer:
        q = q.filter(DiagMethodology.layer == layer)
    rows = q.order_by(DiagMethodology.prior_prob.desc(), DiagMethodology.id.desc()).all()
    out = [to_dict(r) for r in rows]
    if keyword:
        kw = keyword.lower()
        out = [r for r in out if kw in (r["title"] or "").lower() or kw in (r["symptom"] or "").lower()]
    return out


def match_methodology(db: Session, text: str, layer: str | None = None) -> tuple[DiagMethodology | None, float]:
    """按关键词/层匹配最相关方法论，返回 (方法论, 匹配度 0~1)。"""
    text_l = (text or "").lower()
    best: DiagMethodology | None = None
    best_score = 0.0
    for row in db.query(DiagMethodology).filter(DiagMethodology.enabled == 1).all():
        kws = row.keywords or []
        hits = sum(1 for kw in kws if str(kw).lower() in text_l)
        if not hits:
            continue
        score = min(1.0, hits / max(1, len(kws)) + 0.2 * hits)
        if layer and row.layer == layer:
            score = min(1.0, score + 0.15)
        if score > best_score:
            best_score = score
            best = row
    return best, round(best_score, 3)


def create_methodology(db: Session, data: dict) -> DiagMethodology:
    code = data.get("code") or f"MED_USER_{int(db.query(DiagMethodology).count()) + 1:03d}"
    row = DiagMethodology(
        code=code,
        title=data["title"],
        symptom=data.get("symptom"),
        layer=data.get("layer"),
        applicable_arch=data.get("applicable_arch"),
        entry_point=data.get("entry_point"),
        troubleshoot_path=data.get("troubleshoot_path"),
        check_sql=data.get("check_sql"),
        check_command=data.get("check_command"),
        judge_logic=data.get("judge_logic"),
        solution=data.get("solution"),
        risk=data.get("risk"),
        review_note=data.get("review_note"),
        keywords=data.get("keywords"),
        prior_prob=data.get("prior_prob", 0.5),
        tags=data.get("tags"),
        source=data.get("source", "manual"),
        enabled=data.get("enabled", 1),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_methodology(db: Session, row: DiagMethodology, data: dict) -> DiagMethodology:
    for k, v in data.items():
        if v is not None:
            setattr(row, k, v)
    # 人工编辑后转为 manual，避免被种子覆盖
    if row.source == "builtin":
        row.source = "manual"
    db.commit()
    db.refresh(row)
    return row


def delete_methodology(db: Session, row: DiagMethodology) -> None:
    db.delete(row)
    db.commit()
