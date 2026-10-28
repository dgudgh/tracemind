from statistics import mean

from app.analyzers.rules import Finding


def _status_by_pct(value: float, warning: float, critical: float) -> str:
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "normal"


def analyze_oracle_metrics(metrics: dict[str, list[dict]]) -> list[Finding]:
    findings: list[Finding] = []

    for row in metrics.get("inspection_profile", []):
        warnings = row.get("warnings") or []
        status = "warning" if warnings else "normal"
        findings.append(
            Finding(
                "Oracle inspection profile",
                "Version detection and SQL script selection",
                status,
                "script_name",
                row.get("script_name", "unknown"),
                "dbcheck10g.sql / dbcheck_noncdb.sql / dbcheck_cdb.sql",
                (
                    f"Detected Oracle {row.get('version') or 'unknown'}"
                    f", CDB={row.get('cdb') or 'unknown'}."
                    f" Selected {row.get('script_name') or 'unknown'}."
                ),
                "Keep this profile as the baseline for one-click inspection; if the selected script is unexpected, verify database version, CDB mode, and connection target.",
                row,
            )
        )

    for row in metrics.get("account_capability", []):
        status = "warning" if row.get("status") == "warning" else "normal"
        findings.append(
            Finding(
                "连接方式",
                "Oracle 巡检账号能力",
                status,
                "account_capability",
                row.get("capability", "unknown"),
                "SYSDBA=完整巡检；SYSTEM=标准巡检；业务账号=降级巡检",
                row.get("message", "Oracle 巡检账号能力尚未评估"),
                (
                    "补充动态性能、DBA 数据字典和 AWR 视图的只读权限后重新测试连接并巡检。"
                    if status == "warning"
                    else "当前账号满足已启用巡检项，无需提升到 SYSDBA。"
                ),
                row,
            )
        )

    for row in metrics.get("business_objects", []):
        if "error" in row:
            findings.append(_error("业务对象", "对象状态", row))
            continue
        if row.get("status") == "INVALID":
            findings.append(
                Finding(
                    "业务对象",
                    row.get("object_type", "UNKNOWN"),
                    "warning",
                    "invalid_count",
                    str(row.get("object_count") or 0),
                    "存在 INVALID 业务对象",
                    "当前业务账号下存在无效对象",
                    "建议确认对象依赖关系，在业务低峰期重新编译并验证 MES 模块功能。",
                    row,
                )
            )

    for row in metrics.get("tablespace_usage", []):
        if "error" in row:
            findings.append(_error("表空间状态", "表空间使用率", row))
            continue
        used_pct = float(row.get("used_pct") or 0)
        status = _status_by_pct(used_pct, 85, 95)
        findings.append(
            Finding(
                "表空间状态",
                row.get("tablespace_name", "UNKNOWN"),
                status,
                "used_pct",
                f"{used_pct:.2f}%",
                "warning>85%, critical>95%",
                "表空间使用率正常" if status == "normal" else "表空间使用率偏高",
                "关注数据文件增长趋势，必要时评估扩容或清理历史数据。",
                row,
            )
        )

    for row in metrics.get("temp_usage", []):
        if "error" in row:
            findings.append(_error("临时表空间状态", "临时表空间使用率", row))
            continue
        used_pct = float(row.get("used_pct") or 0)
        status = _status_by_pct(used_pct, 70, 90)
        findings.append(
            Finding(
                "临时表空间状态",
                row.get("tablespace_name", "TEMP"),
                status,
                "temp_used_pct",
                f"{used_pct:.2f}%",
                "warning>70%, critical>90%",
                "临时表空间使用率正常" if status == "normal" else "临时表空间使用率偏高",
                "定位大排序、Hash Join 和临时段占用较高的会话，必要时评估扩容 TEMP。",
                row,
            )
        )

    for row in metrics.get("undo_usage", []):
        if "error" in row:
            findings.append(_error("UNDO 状态", "UNDO 使用率", row))
            continue
        used_pct = float(row.get("used_pct") or 0)
        status = _status_by_pct(used_pct, 70, 90)
        findings.append(
            Finding(
                "UNDO 状态",
                row.get("tablespace_name", "UNDO"),
                status,
                "undo_used_pct",
                f"{used_pct:.2f}%",
                "warning>70%, critical>90%",
                "UNDO 使用率正常" if status == "normal" else "UNDO 使用率偏高",
                "排查长事务、批量作业和 UNDO retention 设置，必要时评估扩容。",
                row,
            )
        )

    listener_rows = metrics.get("listener_parameters", [])
    if listener_rows:
        listener_error = next((row for row in listener_rows if "error" in row), None)
        if listener_error:
            findings.append(_error("连接方式", "监听器参数", listener_error))
        else:
            configured = {
                str(row.get("name") or ""): row.get("value")
                for row in listener_rows
            }
            findings.append(
                Finding(
                    "连接方式",
                    "监听器连通性与服务注册参数",
                    "normal",
                    "listener_status",
                    "reachable",
                    "数据库连接成功且监听参数可读取",
                    "当前连接已通过监听器建立，服务注册参数读取成功。",
                    "继续监控监听进程、服务注册和连接入口可用性。",
                    {"parameters": configured},
                )
            )

    for row in metrics.get("asm_usage", []):
        if "error" in row:
            findings.append(_error("ASM 状态", "ASM 磁盘组使用率", row))
            continue
        used_pct = float(row.get("used_pct") or 0)
        status = _status_by_pct(used_pct, 80, 90)
        findings.append(
            Finding(
                "ASM 状态",
                row.get("name", "UNKNOWN"),
                status,
                "used_pct",
                f"{used_pct:.2f}%",
                "warning>80%, critical>90%",
                "ASM 磁盘组使用率正常" if status == "normal" else "ASM 磁盘组空间压力偏高",
                "检查磁盘组可用空间、归档占用和近期扩容计划。",
                row,
            )
        )

    for row in metrics.get("fra_usage", []):
        if "error" in row:
            findings.append(_error("FRA / 归档状态", "FRA 使用率", row))
            continue
        used_pct = float(row.get("used_pct") or 0)
        status = _status_by_pct(used_pct, 80, 90)
        findings.append(
            Finding(
                "FRA / 归档状态",
                row.get("name", "FRA"),
                status,
                "used_pct",
                f"{used_pct:.2f}%",
                "warning>80%, critical>90%",
                "FRA 使用率正常" if status == "normal" else "FRA 使用率偏高",
                "检查 RMAN 备份、归档删除策略和恢复窗口配置。",
                row,
            )
        )

    archive_rows = [row for row in metrics.get("archive_growth", []) if "error" not in row]
    if len(archive_rows) >= 2:
        sizes = [float(row.get("size_gb") or 0) for row in archive_rows]
        latest = sizes[0]
        baseline = mean(sizes[1:]) if sizes[1:] else 0
        if baseline > 0 and latest > baseline * 2:
            findings.append(
                Finding(
                    "FRA / 归档状态",
                    "归档日志增长",
                    "warning",
                    "size_gb",
                    f"{latest:.2f}GB",
                    "latest > 7日均值2倍",
                    "归档日志单日增长明显高于近期均值",
                    "排查批量任务、接口重试、异常 DML 或日志切换频率突增。",
                    archive_rows[0],
                )
            )

    for row in metrics.get("locks", []):
        if "error" in row:
            findings.append(_error("会话与锁等待", "锁等待", row))
            continue
        seconds = int(row.get("seconds_in_wait") or 0)
        status = "critical" if seconds >= 300 else "warning"
        findings.append(
            Finding(
                "会话与锁等待",
                f"SID {row.get('sid')}",
                status,
                "seconds_in_wait",
                str(seconds),
                "存在阻塞 warning, 阻塞超过300秒 critical",
                "发现 blocking session",
                "定位阻塞链路和业务操作窗口，谨慎评估是否需要人工干预会话。",
                row,
            )
        )

    for row in metrics.get("invalid_objects", []):
        if "error" in row:
            findings.append(_error("对象状态", "无效对象", row))
            continue
        count = int(row.get("invalid_count") or 0)
        if count > 0:
            findings.append(
                Finding(
                    "对象状态",
                    f"{row.get('owner')}.{row.get('object_type')}",
                    "warning",
                    "invalid_count",
                    str(count),
                    "存在 INVALID 对象",
                    "存在无效数据库对象",
                    "确认对象依赖关系，优先在测试窗口编译并验证业务模块。",
                    row,
                )
            )

    for row in metrics.get("stale_statistics", []):
        if "error" in row:
            findings.append(_error("统计信息状态", "统计信息过期", row))
            continue
        findings.append(
            Finding(
                "统计信息状态",
                f"{row.get('owner')}.{row.get('table_name')}",
                "warning",
                "stale_stats",
                str(row.get("stale_stats")),
                "stale_stats=YES",
                "表统计信息已过期",
                "评估业务低峰期收集统计信息，避免执行计划偏差。",
                row,
            )
        )

    if not findings:
        findings.append(
            Finding("实例状态", "Oracle 巡检", "normal", "inspection", "ok", "", "未发现异常项", "保持常规巡检。", metrics)
        )
    return findings


def _error(category: str, item: str, row: dict) -> Finding:
    return Finding(
        category,
        item,
        "warning",
        "collector_error",
        row.get("error", "unknown"),
        "采集 SQL 可执行",
        "巡检项采集失败",
        "检查账号权限、数据库版本和相关组件是否启用。",
        row,
    )
