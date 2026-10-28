from app.analyzers.rules import Finding


def analyze_mysql_metrics(metrics: dict[str, list[dict]]) -> list[Finding]:
    findings: list[Finding] = []

    for row in metrics.get("account_capability", []):
        if row.get("status") == "warning":
            findings.append(
                Finding(
                    "连接方式",
                    "业务用户巡检模式",
                    "warning",
                    "account_type",
                    row.get("account_type", "business"),
                    "业务用户仅采集账号可见对象",
                    row.get("message", "业务用户权限受限"),
                    "如需复制状态、Binlog、全局状态、performance_schema 等完整巡检，请配置 System 用户或具备监控权限的专用巡检账号。",
                    row,
                )
            )

    for row in metrics.get("connection_status", []):
        if "error" in row:
            findings.append(_error("连接数", "连接数使用率", row))
            continue
        max_connections = float(row.get("max_connections") or 0)
        threads = float(row.get("Threads_connected") or 0)
        max_used = float(row.get("Max_used_connections") or 0)
        if max_connections:
            used_pct = max(threads, max_used) / max_connections * 100
            status = _status_by_pct(used_pct, 80, 90)
            findings.append(
                Finding(
                    "连接数",
                    "连接数使用率",
                    status,
                    "connection_used_pct",
                    f"{used_pct:.2f}%",
                    "warning>80%, critical>90%",
                    "连接数使用率正常" if status == "normal" else "连接数使用率偏高",
                    "检查连接池配置、慢请求堆积和应用释放连接逻辑。",
                    row,
                )
            )

    for row in metrics.get("replication", []):
        io = row.get("Replica_IO_Running", row.get("Slave_IO_Running"))
        sql = row.get("Replica_SQL_Running", row.get("Slave_SQL_Running"))
        lag = row.get("Seconds_Behind_Source", row.get("Seconds_Behind_Master"))
        if io and sql:
            lag_value = int(lag or 0)
            status = "critical" if lag_value > 300 else "warning" if lag_value > 60 else "normal"
            if io != "Yes" or sql != "Yes":
                status = "critical"
            findings.append(
                Finding(
                    "主从复制",
                    "复制状态",
                    status,
                    "seconds_behind",
                    str(lag_value),
                    "延迟>60秒 warning, >300秒 critical",
                    "复制状态正常" if status == "normal" else "复制存在延迟或线程异常",
                    "检查 IO/SQL 线程错误、网络延迟、主库写入峰值和 GTID 位点。",
                    row,
                )
            )

    for row in metrics.get("large_tables", []):
        if "error" in row:
            findings.append(_error("大表分析", "大表 Top 20", row))
            continue
        total_gb = float(row.get("total_gb") or 0)
        status = "critical" if total_gb > 500 else "warning" if total_gb > 100 else "normal"
        findings.append(
            Finding(
                "大表分析",
                f"{row.get('table_schema')}.{row.get('table_name')}",
                status,
                "total_gb",
                f"{total_gb:.2f}GB",
                "单表>100GB warning, >500GB critical",
                "表容量正常" if status == "normal" else "发现大表容量风险",
                "评估分区、归档、冷热数据拆分和查询路径。",
                row,
            )
        )

    for row in metrics.get("fragmentation", []):
        if "error" in row:
            findings.append(_error("表碎片分析", "表碎片 Top 50", row))
            continue
        free_mb = float(row.get("free_mb") or 0)
        free_gb = free_mb / 1024
        status = "critical" if free_gb > 50 else "warning" if free_gb > 10 else "normal"
        findings.append(
            Finding(
                "表碎片分析",
                f"{row.get('table_schema')}.{row.get('table_name')}",
                status,
                "free_gb",
                f"{free_gb:.2f}GB",
                "碎片>10GB warning, >50GB critical",
                "表碎片空间正常" if status == "normal" else "表碎片空间偏高",
                "在维护窗口评估重建表或在线 DDL，先确认磁盘余量和业务影响。",
                row,
            )
        )

    for row in metrics.get("slow_sql", []):
        if "error" in row:
            findings.append(_error("慢 SQL", "慢 SQL 摘要", row))
            continue
        avg_seconds = float(row.get("avg_seconds") or 0)
        status = "critical" if avg_seconds > 5 else "warning" if avg_seconds > 1 else "normal"
        findings.append(
            Finding(
                "慢 SQL",
                row.get("digest", "SQL Digest"),
                status,
                "avg_seconds",
                f"{avg_seconds:.4f}s",
                "平均耗时>1秒 warning, >5秒 critical",
                "SQL 平均耗时正常" if status == "normal" else "发现慢 SQL 风险",
                "结合执行计划、索引选择性、扫描行数和 MES 业务时段分析。",
                row,
            )
        )

    for row in metrics.get("locks", []):
        if "error" in row:
            findings.append(_error("锁等待", "当前锁等待", row))
            continue
        findings.append(
            Finding(
                "锁等待",
                f"waiting_thread {row.get('waiting_thread')}",
                "warning",
                "lock_wait",
                "detected",
                "存在锁等待",
                "发现 InnoDB 锁等待",
                "定位阻塞事务和业务操作，谨慎评估事务提交、回滚或应用侧限流。",
                row,
            )
        )

    if not findings:
        findings.append(
            Finding("实例状态", "MySQL 巡检", "normal", "inspection", "ok", "", "未发现异常项", "保持常规巡检。", metrics)
        )
    return findings


def _status_by_pct(value: float, warning: float, critical: float) -> str:
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "normal"


def _error(category: str, item: str, row: dict) -> Finding:
    return Finding(
        category,
        item,
        "warning",
        "collector_error",
        row.get("error", "unknown"),
        "采集 SQL 可执行",
        "巡检项采集失败",
        "检查账号权限、数据库版本和 performance_schema / information_schema 兼容性。",
        row,
    )
