import logging
import posixpath
import shlex
import socket
from contextlib import contextmanager
from pathlib import Path

from app.core.crypto import decrypt_password
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.ssh_executor")

SAFE_SCRIPT_DIRS = ("/home/oracle/scripts", "/tmp/mesguard", "/opt/mesguard/scripts")


class SSHConnectionError(Exception):
    """Raised when the remote SSH connection cannot be established or used."""


def _get_ssh_paramiko():
    try:
        import paramiko

        return paramiko
    except ImportError as exc:
        raise RuntimeError("paramiko 未安装，请执行 pip install paramiko") from exc


def _instance_ssh_config(instance: DBInstance) -> dict:
    if not instance.ssh_host:
        raise SSHConnectionError("未配置 SSH 主机")
    if not instance.ssh_username:
        raise SSHConnectionError("未配置 SSH 用户名")
    if not instance.ssh_password_cipher:
        raise SSHConnectionError("未配置 SSH 密码")

    return {
        "hostname": instance.ssh_host,
        "port": instance.ssh_port or 22,
        "username": instance.ssh_username,
        "password": decrypt_password(instance.ssh_password_cipher),
    }


def _ok_result(message: str, **extra) -> dict:
    return {"ok": True, "success": True, "message": message, **extra}


def _fail_result(message: str, **extra) -> dict:
    return {"ok": False, "success": False, "message": message, **extra}


def _normalize_remote_path(remote_path: str) -> str:
    if not remote_path or "\x00" in remote_path:
        raise SSHConnectionError("远程路径不能为空")
    normalized = posixpath.normpath(remote_path)
    if not normalized.startswith("/"):
        raise SSHConnectionError("远程路径必须是绝对路径")
    if not any(normalized == base or normalized.startswith(f"{base}/") for base in SAFE_SCRIPT_DIRS):
        allowed = "、".join(SAFE_SCRIPT_DIRS)
        raise SSHConnectionError(f"远程脚本路径不在白名单目录内：{allowed}")
    return normalized


class ManagedSSHClient:
    def __init__(
        self,
        hostname: str,
        port: int = 22,
        username: str | None = None,
        password: str | None = None,
        timeout: int = 10,
        *,
        require_known_host: bool = False,
    ):
        self.hostname = hostname
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.require_known_host = require_known_host
        self.client = None

    def connect(self):
        paramiko = _get_ssh_paramiko()
        try:
            self.client = paramiko.SSHClient()
            if self.require_known_host:
                try:
                    self.client.load_system_host_keys()
                    user_known_hosts = Path.home() / ".ssh" / "known_hosts"
                    if user_known_hosts.is_file():
                        self.client.load_host_keys(str(user_known_hosts))
                except Exception as exc:
                    raise SSHConnectionError("SSH主机密钥信任库加载失败，已拒绝连接") from exc
                self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.client.connect(
                hostname=self.hostname,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            logger.info("SSH 连接成功 | host=%s:%s | user=%s", self.hostname, self.port, self.username)
            return True
        except SSHConnectionError:
            raise
        except paramiko.AuthenticationException as exc:
            raise SSHConnectionError("SSH认证失败：用户名或密码错误") from exc
        except paramiko.BadHostKeyException as exc:
            raise SSHConnectionError("SSH主机密钥不匹配，已拒绝连接") from exc
        except paramiko.SSHException as exc:
            if self.require_known_host:
                raise SSHConnectionError("SSH主机密钥未受信任或握手校验失败，已拒绝连接") from exc
            raise SSHConnectionError(f"SSH连接错误：{exc}") from exc
        except socket.timeout as exc:
            raise SSHConnectionError(f"连接超时：无法在 {self.timeout} 秒内连接到服务器") from exc
        except OSError as exc:
            raise SSHConnectionError(f"网络连接错误：{exc}") from exc
        except Exception as exc:
            raise SSHConnectionError(f"连接失败：{exc}") from exc

    def execute_command(self, command: str, timeout: int = 30) -> dict:
        if not self.client:
            raise SSHConnectionError("SSH连接未建立")
        if not command or not command.strip():
            raise SSHConnectionError("命令不能为空")

        try:
            logger.info("SSH 执行命令 | host=%s | command_length=%d", self.hostname, len(command))
            _, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            stdout_data = stdout.read().decode("utf-8", errors="replace").strip()
            stderr_data = stderr.read().decode("utf-8", errors="replace").strip()
            exit_code = stdout.channel.recv_exit_status()
            return {
                "ok": exit_code == 0,
                "success": exit_code == 0,
                "stdout": stdout_data,
                "stderr": stderr_data,
                "output": stdout_data,
                "error": stderr_data,
                "exit_code": exit_code,
            }
        except socket.timeout as exc:
            raise SSHConnectionError("命令执行超时") from exc
        except Exception as exc:
            raise SSHConnectionError(f"命令执行失败：{exc}") from exc

    def write_text_file(self, remote_path: str, content: str):
        if not self.client:
            raise SSHConnectionError("SSH连接未建立")
        sftp = self.client.open_sftp()
        try:
            with sftp.file(remote_path, "w") as remote_file:
                remote_file.write(content)
        finally:
            sftp.close()

    def close(self):
        if self.client:
            try:
                self.client.close()
            finally:
                self.client = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


@contextmanager
def ssh_connection(instance: DBInstance, timeout: int = 10):
    config = _instance_ssh_config(instance)
    client = ManagedSSHClient(**config, timeout=timeout)
    try:
        client.connect()
        yield client
    finally:
        client.close()


def test_ssh_connection(instance: DBInstance) -> dict:
    try:
        with ssh_connection(instance, timeout=15) as client:
            details = client.execute_command('echo "SSH连接测试成功"', timeout=15)
        return _ok_result("SSH连接测试成功", details=details)
    except Exception as exc:
        logger.error("SSH 连接测试失败 | host=%s | error=%s", instance.ssh_host, exc)
        return _fail_result(str(exc), details=None)


def execute_command(instance: DBInstance, command: str, timeout: int = 30, connect_timeout: int = 10) -> dict:
    try:
        if len(command or "") > 4000:
            raise SSHConnectionError("命令过长，单次远程命令不能超过 4000 字符")
        with ssh_connection(instance, timeout=connect_timeout) as client:
            result = client.execute_command(command, timeout=timeout)
        return result
    except Exception as exc:
        logger.error("SSH 执行失败 | host=%s | error=%s", instance.ssh_host, exc)
        return {"ok": False, "success": False, "stdout": "", "stderr": str(exc), "output": "", "error": str(exc), "exit_code": -1}


def deploy_script(
    instance: DBInstance,
    script_content: str,
    remote_path: str,
    run: bool = False,
    timeout: int = 60,
) -> dict:
    try:
        normalized_path = _normalize_remote_path(remote_path)
        parent_dir = posixpath.dirname(normalized_path)
        quoted_dir = shlex.quote(parent_dir)
        quoted_path = shlex.quote(normalized_path)

        with ssh_connection(instance, timeout=10) as client:
            mkdir_result = client.execute_command(f"mkdir -p {quoted_dir}", timeout=10)
            if not mkdir_result["ok"]:
                return _fail_result("创建远程目录失败", details=mkdir_result)

            client.write_text_file(normalized_path, script_content)
            chmod_result = client.execute_command(f"chmod 700 {quoted_path}", timeout=10)
            if not chmod_result["ok"]:
                return _fail_result("脚本权限设置失败", remote_path=normalized_path, details=chmod_result)

            run_result = None
            if run:
                run_result = client.execute_command(f"bash {quoted_path}", timeout=timeout)

        logger.info("脚本已部署 | host=%s | path=%s | run=%s", instance.ssh_host, normalized_path, run)
        return _ok_result(
            f"脚本已部署到 {normalized_path}",
            remote_path=normalized_path,
            run_output=(run_result or {}).get("stdout", ""),
            run_result=run_result,
        )
    except Exception as exc:
        logger.error("脚本部署失败 | host=%s | error=%s", instance.ssh_host, exc)
        return _fail_result(f"部署失败：{exc}")


def get_system_info(instance: DBInstance) -> dict:
    commands = {
        "os": "cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d'\"' -f2 || cat /etc/redhat-release 2>/dev/null || uname -s",
        "kernel": "uname -r",
        "hostname": "hostname",
        "uptime": "uptime -p 2>/dev/null || uptime",
        "current_user": "whoami",
        "workdir": "pwd",
        "cpu_info": "cat /proc/cpuinfo | grep 'model name' | head -1 | cut -d':' -f2 | sed 's/^ *//' || echo 'N/A'",
        "memory": "free -h | grep '^Mem:' | awk '{print $2\" total, \"$3\" used, \"$7\" available\"}' || echo 'N/A'",
        "disk_usage": "df -hT -x devtmpfs -x tmpfs -x squashfs | grep -v '^Filesystem'",
        "network": "ip -o -4 addr show | awk '{print $2\": \"$4}' | grep -v '^lo:' || ifconfig | grep -A1 'flags=' | grep 'inet ' | grep -v '127.0.0.1'",
    }
    results = {}

    try:
        with ssh_connection(instance, timeout=10) as client:
            for key, command in commands.items():
                try:
                    result = client.execute_command(command, timeout=15)
                    results[key] = result["stdout"] if result["ok"] and result["stdout"] else result.get("stderr") or "N/A"
                except Exception as exc:
                    results[key] = f"获取失败：{exc}"
        return _ok_result("系统信息获取成功", data=results)
    except Exception as exc:
        logger.error("系统信息获取失败 | host=%s | error=%s", instance.ssh_host, exc)
        return _fail_result(str(exc), data={})


def diagnose_ssh_connection(instance: DBInstance) -> dict:
    diagnosis = {
        "hostname": instance.ssh_host,
        "port": instance.ssh_port or 22,
        "username": instance.ssh_username,
        "tests": [],
    }

    if not instance.ssh_host:
        diagnosis["tests"].append({"name": "配置检查", "status": "failed", "message": "未配置 SSH 主机"})
        return diagnosis

    try:
        with socket.create_connection((instance.ssh_host, instance.ssh_port or 22), timeout=5):
            diagnosis["tests"].append({"name": "网络连通性", "status": "success", "message": f"端口 {instance.ssh_port or 22} 可达"})
    except Exception as exc:
        diagnosis["tests"].append({"name": "网络连通性", "status": "failed", "message": f"无法连接到端口：{exc}"})
        return diagnosis

    paramiko = _get_ssh_paramiko()
    transport = None
    try:
        transport = paramiko.Transport((instance.ssh_host, instance.ssh_port or 22))
        transport.start_client(timeout=10)
        try:
            auth_methods = transport.get_allowed_auths(instance.ssh_username or "test")
        except Exception:
            auth_methods = ""
        diagnosis["tests"].append(
            {
                "name": "SSH服务",
                "status": "success",
                "message": f"SSH 服务可用，认证方式：{auth_methods or '未知'}",
            }
        )
    except Exception as exc:
        diagnosis["tests"].append({"name": "SSH服务", "status": "error", "message": f"SSH 服务测试失败：{exc}"})
        return diagnosis
    finally:
        if transport:
            transport.close()

    auth_test = test_ssh_connection(instance)
    diagnosis["tests"].append(
        {
            "name": "密码认证",
            "status": "success" if auth_test.get("ok") else "failed",
            "message": auth_test.get("message", "认证测试失败"),
        }
    )
    return diagnosis
