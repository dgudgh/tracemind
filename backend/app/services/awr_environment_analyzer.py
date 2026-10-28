import logging
from datetime import datetime

from app.models.db_instance import DBInstance
from app.collectors.oracle.oracle_connection import oracle_connection

logger = logging.getLogger("app.services.awr_environment")


def collect_environment(instance: DBInstance, begin_snap: int = None, end_snap: int = None) -> dict:
    """采集 AWR 环境信息：CPU、实例、数据库、快照时长"""
    result = {
        "cpu_count": 0, "cpu_cores": 0, "cpu_sockets": 0,
        "physical_memory_gb": 0,
        "instance_name": "", "host_name": "", "startup_time": "",
        "db_uptime_hours": 0, "version": "",
        "dbid": "", "db_name": "", "db_role": "",
        "snapshot_elapsed_minutes": 0,
        "db_time_seconds": 0, "db_cpu_seconds": 0,
        "server_type": "UNKNOWN", "server_type_label": "无法判断",
        "server_type_confidence": "LOW", "server_type_evidence": [],
        "aas": 0, "aas_per_cpu": 0, "db_cpu_per_cpu": 0,
        "snapshot_quality": "UNKNOWN", "analysis_reliability": "MEDIUM",
        "messages": [],
    }

    try:
        with oracle_connection(instance) as conn:
            with conn.cursor() as cur:
                # v$osstat - CPU info
                try:
                    cur.execute("""
                        SELECT stat_name, value FROM v$osstat
                        WHERE stat_name IN ('NUM_CPUS','NUM_CPU_CORES','NUM_CPU_SOCKETS','PHYSICAL_MEMORY_BYTES')
                    """)
                    for row in cur.fetchall():
                        name, val = row[0], row[1]
                        if name == "NUM_CPUS":
                            result["cpu_count"] = int(val) if val else 0
                        elif name == "NUM_CPU_CORES":
                            result["cpu_cores"] = int(val) if val else 0
                        elif name == "NUM_CPU_SOCKETS":
                            result["cpu_sockets"] = int(val) if val else 0
                        elif name == "PHYSICAL_MEMORY_BYTES":
                            result["physical_memory_gb"] = round(int(val) / 1024 / 1024 / 1024, 1) if val else 0
                except Exception as e:
                    logger.warning("v$osstat query failed: %s", e)

                # v$instance
                try:
                    cur.execute("""
                        SELECT instance_name, host_name, startup_time, version,
                               ROUND((SYSDATE - startup_time) * 24, 2) AS uptime_hours
                        FROM v$instance
                    """)
                    row = cur.fetchone()
                    if row:
                        result["instance_name"] = row[0] or ""
                        result["host_name"] = row[1] or ""
                        result["startup_time"] = str(row[2]) if row[2] else ""
                        result["version"] = row[3] or ""
                        result["db_uptime_hours"] = float(row[4]) if row[4] else 0
                except Exception as e:
                    logger.warning("v$instance query failed: %s", e)

                # v$database
                try:
                    cur.execute("SELECT dbid, name, database_role FROM v$database")
                    row = cur.fetchone()
                    if row:
                        result["dbid"] = str(row[0]) if row[0] else ""
                        result["db_name"] = row[1] or ""
                        result["db_role"] = row[2] or ""
                except Exception as e:
                    logger.warning("v$database query failed: %s", e)

                # Snapshot elapsed time
                if begin_snap and end_snap:
                    try:
                        cur.execute("""
                            SELECT ROUND((CAST(MAX(end_interval_time) AS DATE) - CAST(MIN(begin_interval_time) AS DATE)) * 24 * 60, 2)
                            FROM dba_hist_snapshot
                            WHERE snap_id BETWEEN :bs AND :es AND dbid = :dbid
                        """, {"bs": begin_snap, "es": end_snap, "dbid": result["dbid"]})
                        row = cur.fetchone()
                        if row and row[0]:
                            result["snapshot_elapsed_minutes"] = float(row[0])
                    except Exception as e:
                        logger.warning("snapshot elapsed query failed: %s", e)

                    # DB Time / DB CPU from AWR
                    try:
                        cur.execute("""
                            SELECT e1.value AS db_time, e2.value AS db_cpu
                            FROM (
                                SELECT value FROM dba_hist_sysstat
                                WHERE snap_id BETWEEN :bs AND :es AND dbid = :dbid
                                  AND stat_name = 'DB time' AND instance_number = 1
                                ORDER BY snap_id DESC FETCH FIRST 1 ROWS ONLY
                            ) e1,
                            (
                                SELECT value FROM dba_hist_sysstat
                                WHERE snap_id BETWEEN :bs AND :es AND dbid = :dbid
                                  AND stat_name = 'DB CPU' AND instance_number = 1
                                ORDER BY snap_id DESC FETCH FIRST 1 ROWS ONLY
                            ) e2
                        """, {"bs": begin_snap, "es": end_snap, "dbid": result["dbid"]})
                        row = cur.fetchone()
                        if row:
                            result["db_time_seconds"] = float(row[0]) / 1_000_000 if row[0] else 0
                            result["db_cpu_seconds"] = float(row[1]) / 1_000_000 if row[1] else 0
                    except Exception as e:
                        logger.warning("DB time/CPU query failed: %s", e)

    except Exception as e:
        logger.error("collect_environment failed: %s", e)
        result["messages"].append(f"环境采集失败: {e}")

    # Detect VM via SSH
    _detect_server_type(instance, result)

    # Calculate derived metrics
    _calculate_metrics(result)

    return result


def _detect_server_type(instance: DBInstance, result: dict):
    """通过 SSH 判断虚拟机/物理机"""
    try:
        from app.services.ssh_executor import execute_command

        def _ssh_exec(inst, cmd):
            r = execute_command(inst, cmd)
            return r.get("exit_code", -1), r.get("stdout", "") or r.get("output", "")
        if not instance.ssh_host:
            result["messages"].append("未配置 SSH，无法判断服务器类型")
            return

        evidence = []

        # systemd-detect-virt
        try:
            rc, out = _ssh_exec(instance, "systemd-detect-virt 2>/dev/null || echo UNKNOWN")
            virt_type = out.strip().split("\n")[0].strip() if out else ""
            if virt_type and virt_type != "UNKNOWN" and virt_type != "none":
                evidence.append(f"systemd-detect-virt={virt_type}")
                if "vmware" in virt_type.lower():
                    result["server_type"] = "VMWARE_VM"
                    result["server_type_label"] = "VMware 虚拟机"
                elif "kvm" in virt_type.lower() or "qemu" in virt_type.lower():
                    result["server_type"] = "KVM_VM"
                    result["server_type_label"] = "KVM 虚拟机"
                elif "microsoft" in virt_type.lower() or "hyper" in virt_type.lower():
                    result["server_type"] = "HYPER_V_VM"
                    result["server_type_label"] = "Hyper-V 虚拟机"
                elif "xen" in virt_type.lower():
                    result["server_type"] = "XEN_VM"
                    result["server_type_label"] = "Xen 虚拟机"
                elif "oracle" in virt_type.lower():
                    result["server_type"] = "ORACLE_VM"
                    result["server_type_label"] = "Oracle VM 虚拟机"
            elif virt_type == "none":
                evidence.append("systemd-detect-virt=none(物理机)")
        except Exception:
            pass

        # product_name
        try:
            rc, out = _ssh_exec(instance, "cat /sys/class/dmi/id/product_name 2>/dev/null || echo N/A")
            product = out.strip().split("\n")[0].strip() if out else ""
            if product and product != "N/A":
                evidence.append(f"product_name={product}")
                pl = product.lower()
                if "vmware" in pl:
                    result["server_type"] = "VMWARE_VM"
                    result["server_type_label"] = "VMware 虚拟机"
                elif "virtualbox" in pl:
                    result["server_type"] = "VIRTUALBOX_VM"
                    result["server_type_label"] = "VirtualBox 虚拟机"
                elif "kvm" in pl or "qemu" in pl:
                    result["server_type"] = "KVM_VM"
                    result["server_type_label"] = "KVM 虚拟机"
                elif "hyper-v" in pl:
                    result["server_type"] = "HYPER_V_VM"
                    result["server_type_label"] = "Hyper-V 虚拟机"
                elif any(x in pl for x in ["alibaba", "tencent", "huawei", "amazon", "ecs"]):
                    result["server_type"] = "CLOUD_VM"
                    result["server_type_label"] = "云主机"
                elif any(x in pl for x in ["poweredge", "proliant", "thinksystem", "rh2288", "kunlun", "taishan"]):
                    if result["server_type"] == "UNKNOWN":
                        result["server_type"] = "PHYSICAL"
                        result["server_type_label"] = "物理机(推测)"
        except Exception:
            pass

        result["server_type_evidence"] = evidence
        result["server_type_confidence"] = "HIGH" if len(evidence) >= 2 else ("MEDIUM" if len(evidence) == 1 else "LOW")

    except Exception as e:
        logger.warning("SSH server type detection failed: %s", e)
        result["messages"].append(f"SSH 检测失败: {e}")


def _calculate_metrics(result: dict):
    """计算 AAS、CPU per CPU、快照质量、分析可信度"""
    cpu_count = result["cpu_count"] or 0
    elapsed_min = result["snapshot_elapsed_minutes"]
    db_time = result["db_time_seconds"]
    db_cpu = result["db_cpu_seconds"]
    uptime = result["db_uptime_hours"]

    # AAS & CPU metrics
    if elapsed_min and elapsed_min > 0 and cpu_count > 0:
        elapsed_sec = elapsed_min * 60
        result["aas"] = round(db_time / elapsed_sec, 2) if db_time else 0
        result["aas_per_cpu"] = round(result["aas"] / cpu_count, 2)
        result["db_cpu_per_cpu"] = round(db_cpu / elapsed_sec / cpu_count, 2) if db_cpu else 0

    # Snapshot quality
    if elapsed_min:
        if elapsed_min < 15:
            result["snapshot_quality"] = "TOO_SHORT"
            result["messages"].append(f"快照区间仅 {elapsed_min:.0f} 分钟，样本过短，容易受瞬时波动影响")
        elif elapsed_min <= 120:
            result["snapshot_quality"] = "GOOD"
        elif elapsed_min <= 360:
            result["snapshot_quality"] = "ACCEPTABLE"
            result["messages"].append(f"快照区间 {elapsed_min:.0f} 分钟，较长但可参考")
        else:
            result["snapshot_quality"] = "TOO_LONG"
            result["messages"].append(f"快照区间 {elapsed_min:.0f} 分钟，过长可能掩盖短时故障峰值")

    # Analysis reliability
    if uptime < 1:
        result["analysis_reliability"] = "LOW"
        result["messages"].append("数据库刚启动不到 1 小时，AWR 参考价值低，缓存未稳定")
    elif uptime < 6:
        result["analysis_reliability"] = "MEDIUM"
        result["messages"].append("数据库运行时间较短（<6小时），缓存和负载可能未稳定")
    elif uptime >= 24:
        result["analysis_reliability"] = "HIGH"
    else:
        result["analysis_reliability"] = "MEDIUM"

    if result["snapshot_quality"] == "TOO_SHORT":
        result["analysis_reliability"] = "LOW"


def get_cpu_risk_level(env: dict) -> dict:
    """根据 CPU 利用率判断风险等级"""
    cpu_per_cpu = env.get("db_cpu_per_cpu", 0)
    server_type = env.get("server_type", "UNKNOWN")
    is_vm = server_type in ("VMWARE_VM", "KVM_VM", "HYPER_V_VM", "CLOUD_VM", "VIRTUALBOX_VM", "XEN_VM", "ORACLE_VM")

    risk = {"level": "LOW", "score": 100, "messages": []}

    if is_vm:
        if cpu_per_cpu >= 0.7:
            risk["level"] = "HIGH"
            risk["score"] = 30
            risk["messages"].append("虚拟机环境 CPU 利用率高，建议检查宿主机 CPU Ready、vCPU 超分")
        elif cpu_per_cpu >= 0.5:
            risk["level"] = "MEDIUM"
            risk["score"] = 60
            risk["messages"].append("虚拟机环境 CPU 有一定压力，注意宿主机调度影响")
    else:
        if cpu_per_cpu >= 0.9:
            risk["level"] = "CRITICAL"
            risk["score"] = 10
        elif cpu_per_cpu >= 0.75:
            risk["level"] = "HIGH"
            risk["score"] = 30
        elif cpu_per_cpu >= 0.5:
            risk["level"] = "MEDIUM"
            risk["score"] = 60

    return risk


def get_environment_prompt(env: dict) -> str:
    """生成 AWR AI 分析的环境上下文 prompt"""
    lines = [
        f"服务器类型：{env.get('server_type_label', '未知')}",
        f"判断置信度：{env.get('server_type_confidence', 'LOW')}",
        f"CPU 核数：{env.get('cpu_count', 0)}",
        f"CPU Cores：{env.get('cpu_cores', 0)}",
        f"CPU Sockets：{env.get('cpu_sockets', 0)}",
        f"物理内存：{env.get('physical_memory_gb', 0)} GB",
        f"数据库启动时间：{env.get('startup_time', '')}",
        f"运行时长：{env.get('db_uptime_hours', 0)} 小时",
        f"AWR 快照时长：{env.get('snapshot_elapsed_minutes', 0)} 分钟",
        f"AAS：{env.get('aas', 0)}",
        f"AAS/CPU：{env.get('aas_per_cpu', 0)}",
        f"DB CPU/CPU：{env.get('db_cpu_per_cpu', 0)}",
        f"快照质量：{env.get('snapshot_quality', '未知')}",
        f"分析可信度：{env.get('analysis_reliability', '未知')}",
    ]

    is_vm = env.get("server_type", "") in ("VMWARE_VM", "KVM_VM", "HYPER_V_VM", "CLOUD_VM")
    if is_vm:
        lines.append("重要：当前为虚拟机环境，CPU/IO 分析需考虑虚拟化影响（CPU Ready、存储后端延迟等）")
    if env.get("db_uptime_hours", 0) < 6:
        lines.append("重要：数据库运行时间较短，Buffer Cache 命中率可能偏低，不要直接判断为内存不足")
    if env.get("snapshot_quality") == "TOO_SHORT":
        lines.append("重要：快照时间过短，分析结论参考价值有限")

    for msg in env.get("messages", []):
        lines.append(f"提示：{msg}")

    return "\n".join(lines)
