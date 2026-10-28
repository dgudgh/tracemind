"""采集器共享：Signal 数据结构 + HTTP/SSH 工具 + 采集上下文。"""
from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests

from app.services.diagnosis import constants as C

logger = logging.getLogger("app.collectors.diag")


@dataclass
class Signal:
    """采集到的一条归一化信号。abnormal 时进入证据链。"""
    layer: str
    source: str            # nginx/tomcat/redis/kafka/skywalking/os/storage
    metric: str
    value: Any = None
    threshold: Any = None
    status: str = "ok"     # ok/warn/critical
    phenomenon: str = ""
    judgement: str = ""
    impact: str = ""
    suggestion: str = ""
    node_type: str | None = None   # 拓扑节点类型
    detail: dict = field(default_factory=dict)
    event_time: datetime | None = None

    @property
    def abnormal(self) -> bool:
        return self.status in ("warn", "critical")

    @property
    def severity(self) -> str:
        return {"critical": "critical", "warn": "warning"}.get(self.status, "info")


@dataclass
class CollectContext:
    """单个数据源的采集上下文。"""
    name: str
    source_type: str
    endpoint: str | None
    auth: dict
    config: dict
    instance: Any = None   # 绑定的 DBInstance（用于 SSH 回退）

    def cfg(self, key: str, default=None):
        return (self.config or {}).get(key, default)


# --------------------------------------------------------------------------- HTTP
def http_get(url: str, timeout: int = 6, auth: tuple | None = None, headers: dict | None = None) -> str | None:
    try:
        resp = requests.get(url, timeout=timeout, auth=auth, headers=headers)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.info("http_get failed %s: %s", url, e)
        return None


def http_post_json(url: str, payload: dict, timeout: int = 6, headers: dict | None = None) -> dict | None:
    try:
        resp = requests.post(url, json=payload, timeout=timeout, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.info("http_post_json failed %s: %s", url, e)
        return None


# --------------------------------------------------------------------------- SSH
def ssh_run(ctx: CollectContext, command: str, timeout: int = 10) -> str | None:
    """在数据源主机执行命令；数据源未配 SSH 凭据时回退到绑定实例的 SSH 配置。"""
    from app.core.crypto import decrypt_password
    from app.services.ssh_executor import ManagedSSHClient, SSHConnectionError

    auth = ctx.auth or {}
    host = auth.get("ssh_host") or auth.get("host") or ctx.endpoint
    port = int(auth.get("ssh_port") or auth.get("port") or 22)
    user = auth.get("ssh_username") or auth.get("username")
    password = auth.get("ssh_password") or auth.get("password")

    # 回退到绑定实例的 SSH 配置
    inst = ctx.instance
    if (not host or not user or not password) and inst is not None and getattr(inst, "ssh_host", None):
        host = host or inst.ssh_host
        port = port or (inst.ssh_port or 22)
        user = user or inst.ssh_username
        try:
            password = password or (decrypt_password(inst.ssh_password_cipher) if inst.ssh_password_cipher else None)
        except Exception:
            pass

    if not host or not user or not password:
        logger.info("ssh_run skipped (no creds) for %s", ctx.name)
        return None

    try:
        with ManagedSSHClient(hostname=host, port=port, username=user, password=password, timeout=8) as cli:
            res = cli.execute_command(command, timeout=timeout)
        return res.get("stdout") if res.get("ok") else None
    except SSHConnectionError as e:
        logger.info("ssh_run failed for %s: %s", ctx.name, e)
        return None
    except Exception as e:
        logger.info("ssh_run error for %s: %s", ctx.name, e)
        return None


# --------------------------------------------------------------------------- TCP/RESP
def tcp_send_recv(host: str, port: int, payload: bytes, timeout: int = 5, recv_size: int = 65536) -> bytes | None:
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(payload)
            chunks = []
            try:
                while True:
                    data = s.recv(recv_size)
                    if not data:
                        break
                    chunks.append(data)
                    if len(data) < recv_size:
                        break
            except socket.timeout:
                pass
            return b"".join(chunks)
    except Exception as e:
        logger.info("tcp_send_recv failed %s:%s: %s", host, port, e)
        return None


def status_of(value: float, warn: float, crit: float, higher_is_worse: bool = True) -> str:
    """阈值判定。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "ok"
    if higher_is_worse:
        if v >= crit:
            return "critical"
        if v >= warn:
            return "warn"
    else:
        if v <= crit:
            return "critical"
        if v <= warn:
            return "warn"
    return "ok"


# 暴露层常量给采集器
LAYER_APP = C.LAYER_APP
LAYER_NETWORK = C.LAYER_NETWORK
LAYER_OS = C.LAYER_OS
LAYER_STORAGE = C.LAYER_STORAGE
LAYER_BUSINESS = C.LAYER_BUSINESS
