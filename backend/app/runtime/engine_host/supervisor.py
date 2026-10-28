"""Fixture-only Supervisor: verified staging, real Provider server, per-call UDS."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.runtime.engine_host import ipc
from app.runtime.engine_host.lifecycle import CrashPolicy, State
from app.runtime.engine_packs.contract import build_payload_inventory
from app.runtime.engine_packs.trust import canonical_manifest_bytes, verify_engine_manifest_signature
from app.runtime.engine_sdk.contracts import METHODS, RESULT_SCHEMA, TASK_SCHEMA, SdkError, digest_json
from app.runtime.engine_host.ownership import (
    OwnershipError, capture_process, ensure_private_workdir)
from app.runtime.engine_host.packages import ENTRYPOINT, describe_context, runtime_digest

SUPPORTED_SDK_VERSIONS = ("1.0", "0.9")
RPC_VERSION = "engine.rpc/v1"

# 参数白名单：类型必须精确匹配，bool 不能冒充 int（True 会被当成 1）。
_NUMERIC_PARAMS = ("sleep", "timeout", "output_bytes", "reply_delay")
_SCALAR_PARAMS = ("echo",)
_BOOL_PARAMS = ("ignore_cancel", "spawn_child")
_FORBIDDEN_PARAMS = ("shell", "command", "argv", "exec")


def _validate_dispatch_params(params) -> None:
    """在连 Provider 之前拒绝非法参数：不能让"看起来像参数"的输入进到进程里。"""
    if params is None:
        return
    if not isinstance(params, dict):
        raise SupervisorError("sdk_params_invalid")
    for name in _FORBIDDEN_PARAMS:
        if name in params:
            raise SupervisorError("sdk_params_invalid", name)
    for name in _NUMERIC_PARAMS:
        if name in params:
            value = params[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SupervisorError("sdk_params_invalid", name)
            if value < 0:
                raise SupervisorError("sdk_params_invalid", name)
    for name in _SCALAR_PARAMS:
        # echo 是"原样回显"的探针参数，允许任意标量；容器类型一律拒绝，
        # 免得把结构塞进日志/证据里再当字符串比较。
        value = params[name] if name in params else None
        if name in params and value is not None and not isinstance(value, (str, int, float, bool)):
            raise SupervisorError("sdk_params_invalid", name)
    for name in _BOOL_PARAMS:
        if name in params and not isinstance(params[name], bool):
            raise SupervisorError("sdk_params_invalid", name)


@dataclass
class LaunchRecord:
    pid: int
    launch_instance_id: str
    socket_path: str
    staging_dir: str
    manifest_sha256: str
    provider_version: str
    package_digest: str = ""
    runtime_digest: str = ""
    sdk_version: str = "engine-sdk/v1"
    members: list = field(default_factory=list)
    start_time: float = 0.0
    owner_token: str = ""
    host_pid: int = 0


class SupervisorError(RuntimeError):
    def __init__(self, code: str, detail: str = "", *, category: str = "runtime",
                 retryable: bool = False, effect_state: str = "unknown"):
        self.code = code
        self.detail = detail
        self.category = category
        self.retryable = retryable
        self.effect_state = effect_state
        super().__init__(code)


class _JobFuture(Future):
    """排队中的 job 必须能被 cancel() 撤销，并让 result() 抛 call_cancelled。

    `concurrent.futures.Future.cancel()` 只会把 future 置成 CANCELLED，调用方拿到的是
    `CancelledError`——那无法区分"取消"和"框架内部异常"，也带不出 effect_state。
    所以这里自己维护取消语义：cancel() 返回 True 并写入带 effect_state 的 SupervisorError。
    """

    def __init__(self, on_cancel=None) -> None:
        super().__init__()
        self.request_id = uuid.uuid4().hex
        self.optional_fields: dict | None = None
        # 取消必须同时做两件事：把 job 从 pending 队列摘除（否则位置永不释放），
        # 以及给调用方一个带 effect_state 的确定结果。
        self._on_cancel = on_cancel

    def cancel(self) -> bool:  # noqa: D401 - 语义与 Future.cancel 对齐
        if self.done():
            return False
        if self._on_cancel is not None:
            self._on_cancel(self.request_id)
        self.set_exception(SupervisorError("call_cancelled", effect_state="none"))
        return True


@dataclass
class _Job:
    method: str
    params: dict
    timeout: float
    context: dict
    optional_fields: dict | None
    future: _JobFuture
    enqueued_at: float
    deadline_at: float
    operation_id: str



def _heartbeat_issue(payload, launch_instance_id) -> str | None:
    """心跳自述校验：类型、非负、以及是否还是同一代进程。返回问题码或 None。"""
    if not isinstance(payload, dict):
        return "heartbeat_invalid"
    inflight = payload.get("inflight")
    if isinstance(inflight, bool) or not isinstance(inflight, int) or inflight < 0:
        return "heartbeat_count_invalid"
    queue = payload.get("queue")
    if queue is not None and (isinstance(queue, bool) or not isinstance(queue, int) or queue < 0):
        return "heartbeat_count_invalid"
    connections = payload.get("connections")
    if connections is not None and (isinstance(connections, bool)
                                    or not isinstance(connections, int) or connections < 0):
        return "heartbeat_invalid"
    reported = payload.get("launch_instance_id")
    if reported is not None and launch_instance_id is not None and reported != launch_instance_id:
        return "launch_identity_mismatch"
    return None


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _identity_from(verified, manifest: dict, generation: int) -> dict:
    """`_stage` 的两种返回形态都要接：VerifiedManifest（基类）与 dict（ProviderSupervisor）。"""
    if isinstance(verified, dict):
        base = dict(verified)
        base.setdefault("pkg_manifest_sha256", base.get("manifest_sha256"))
        base["generation"] = int(base.get("generation", generation))
        return base
    base = {
        "manifest_sha256": getattr(verified, "manifest_sha256", ""),
        "engine_id": getattr(verified, "engine_id", manifest.get("engine_id")),
        "package_digest": "sha256:" + getattr(verified, "manifest_sha256", ""),
        "runtime_digest": manifest.get("runtime_digest", runtime_digest()),
        "provider_version": str(manifest.get("version")),
        "sdk_version": str(manifest.get("sdk_version", "1.0")),
        "generation": generation,
    }
    return base


class FixtureSupervisor:
    # Provider 入口约定。fixture 与 native 包的文件名/entrypoint_id 不同，
    # 之前 _spawn 写死 fake_provider.py，导致 native 包永远 entrypoint_missing。
    ENTRYPOINT_ID = ENTRYPOINT
    ENTRYPOINT_FILE = "fake_provider.py"

    """宿主侧 Provider 监管器。

    生命周期口径（lifecycle.State / CrashPolicy 在此真正被消费，而不是只存在于单测里）：
      STARTING → READY ──(崩溃且 restart)──> STARTING → READY …
                      └─(崩溃 6 次或熔断)──> QUARANTINED ──manual_reset()──> READY
      READY ──drain()──> DRAINING ──> READY（在途调用清零）
      READY ──restart()──> STARTING → READY（复用不可变 staging，换 launch_instance_id）

    restart 默认 False：只有显式开启自动重启的宿主才会启动崩溃监控线程，
    避免影响不关心重启语义的既有调用方。
    """

    def __init__(self, *, workdir: Path | None = None, phase_hook=None,
                 restart: bool = False, crash_policy: CrashPolicy | None = None,
                 max_queue: int = 8, max_inflight: int = 8,
                 max_output_bytes: int = 64 * 1024 * 1024, max_connections: int = 32,
                 cancellation_grace: float = 0.5, generation: int = 1, **_options):
        if workdir is None:
            raise SupervisorError("explicit_workdir_required")
        # 私有 workdir 的校验只有一处权威实现：ownership.ensure_private_workdir。
        # 这里自己再判一遍会分叉（相对路径、符号链接、已有内容三种情况各说各话）。
        try:
            self.workdir = ensure_private_workdir(workdir)
        except OwnershipError:
            raise
        self.owner_token = json.loads(
            (self.workdir / "OWNERSHIP").read_text(encoding="utf-8"))["token"]
        self.socket_dir = Path(tempfile.mkdtemp(prefix="tm-ipc-", dir="/tmp"))
        self.socket_path = self.socket_dir / "provider.sock"
        self.record: LaunchRecord | None = None
        self._child: subprocess.Popen | None = None
        self._token = b""
        self._launch_id = ""
        self._closed = False
        self.phase_hook = phase_hook

        # ---- 生命周期与容量参数 ----
        # 注意：属性必须叫 auto_restart，不能叫 restart —— 那会覆盖同名方法 restart()。
        self.auto_restart = bool(restart)
        self.crash_policy = crash_policy if crash_policy is not None else CrashPolicy()
        self.max_queue = max(0, int(max_queue))
        self.max_inflight = max(1, int(max_inflight))
        self.max_output_bytes = int(max_output_bytes)
        self.max_connections = int(max_connections)
        self.cancellation_grace = float(cancellation_grace)
        self._generation = int(generation)
        # 未识别的构造参数如实留存，便于发现"传了但没生效"的选项。
        self.unused_options = dict(_options)

        self.state: State = State.STOPPED
        self.events: list[dict] = []
        self._events_lock = threading.Lock()
        self._spec: tuple = (None, {})
        self._staged: Path | None = None
        self._base_identity: dict = {}
        self._launch_params: tuple | None = None
        self._quarantine_reason: str | None = None
        self._cancel_requested: set[str] = set()
        self._executed = 0
        self._queue_depth = 0
        self._monitor: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._slots = threading.BoundedSemaphore(self.max_inflight)
        self._inflight = 0
        self._inflight_lock = threading.Lock()

        # ---- 真实调度队列 ----
        # 之前每个 submit 各起一个线程，等于"没有队列"：max_queue 只是计数，
        # 排队超时、排队取消、max_inflight 限流全都不成立。这里改成
        # 「固定 worker 池 + 显式 pending 列表」，排队超时在**取活时**判定，
        # 取消在**入队与运行两处**分别处理。
        self._pending: deque = deque()
        self._queue_lock = threading.Lock()
        self._work_cv = threading.Condition(self._queue_lock)
        self._workers: list[threading.Thread] = []
        self._workers_stop = threading.Event()
        # operation_id -> 运行中的 job，供 cancel 找到要通知 Provider 的那一次调用。
        self._running: dict[str, "_Job"] = {}
        # Provider 派生出来的子进程（spawn_child），跨重启保留身份以便回收。
        self._observed_children: dict[int, float] = {}

    # ---------------------------------------------------------------- 事件与状态
    def _event(self, name: str, **detail) -> dict:
        entry = {"event": name, "at": time.monotonic()}
        entry.update(detail)
        with self._events_lock:
            self.events.append(entry)
        return entry

    def _set_state(self, state: State, **detail) -> None:
        self.state = state
        self._event("state", state=state.value, **detail)
        self._phase("state", {"state": state.value})

    @property
    def identity(self) -> dict:
        base = dict(self._base_identity or {})
        if self.record is not None:
            base.update(launch_instance_id=self.record.launch_instance_id, pid=self.record.pid,
                        package_digest=self.record.package_digest,
                        runtime_digest=self.record.runtime_digest,
                        provider_version=self.record.provider_version,
                        sdk_version=self.record.sdk_version)
        base.setdefault("generation", self._generation)
        return base

    def quarantine(self, reason: str) -> dict:
        """熔断：杀掉 Provider 并停在 QUARANTINED，后续调用一律拒绝。"""
        self._quarantine_reason = reason
        self._terminate_process_group()
        self._event("quarantine", reason=reason)
        self._set_state(State.QUARANTINED, reason=reason)
        return {"state": State.QUARANTINED.value, "reason": reason}

    def manual_reset(self) -> dict:
        """人工复位：清熔断与退避计数，然后从原 staging 重新拉起。"""
        self.crash_policy.manual_reset()
        self._quarantine_reason = None
        self._cancel_requested.clear()
        self._event("manual_reset")
        if self._launch_params is not None:
            _, manifest, store_path = self._launch_params
            self._spawn(manifest, store_path)
        return {"state": self.state.value, "circuit_until": self.crash_policy.circuit_until}

    def cleanup_report(self) -> dict:
        """如实报告残留：进程、socket、临时产物。空列表才代表干净。"""
        processes: list[int] = []
        if self.record is not None:
            processes.extend(m.get("pid") for m in self.record.members if _pid_alive(m.get("pid")))
        if self._child is not None and self._child.poll() is None and self._child.pid not in processes:
            processes.append(self._child.pid)
        for pid in list(self._observed_children):
            if _pid_alive(pid) and pid not in processes:
                processes.append(pid)
            elif not _pid_alive(pid):
                self._observed_children.pop(pid, None)
        sockets = [str(self.socket_path)] if self.socket_path.exists() else []
        artifacts: list[str] = []
        if self.socket_dir.exists():
            artifacts.append(str(self.socket_dir))
        runtime_root = self.workdir / "runtime"
        if runtime_root.is_dir():
            artifacts.extend(str(p) for p in runtime_root.iterdir())
        return {"processes_remaining": processes, "sockets_remaining": sockets,
                "temporary_artifacts_remaining": artifacts}

    def owned_resources(self) -> list[dict]:
        """当前我们真实拥有的 Provider 进程记录，可直接交给 ownership.cleanup_owned。

        空列表 = 没有可主张的归属（启动失败、已停止、进程已退出）。
        这里不能返回"残留清单"那种汇总 dict：清理逻辑需要 pid/start_time/pgid 这一组
        精确身份，否则要么杀错进程，要么因身份不符而什么都不敢杀。
        """
        if self.record is None or self._child is None or self._child.poll() is not None:
            return []
        try:
            return [capture_process(self.record.pid, launch_instance_id=self._launch_id,
                                    socket_path=self.socket_path, workdir=self.workdir,
                                    package_digest=self.record.package_digest)]
        except Exception:  # noqa: BLE001 - 进程已消失时按"无归属"处理
            return []

    # ---------------------------------------------------------------- 崩溃监控
    def _start_monitor(self) -> None:
        if not self.auto_restart:
            return
        if self._monitor is not None and self._monitor.is_alive():
            return
        self._monitor_stop.clear()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(0.02):
            child = self._child
            if child is None or child.poll() is None:
                continue
            if self.state in (State.STOPPED, State.QUARANTINED, State.DRAINING):
                continue
            self._handle_crash()

    def _handle_crash(self) -> None:
        self._event("crash", launch_instance_id=self._launch_id,
                    exit_code=self._child.poll() if self._child else None)
        delay = self.crash_policy.delay()
        if delay is None:
            # 退避策略判定熔断：不再重启，停在 QUARANTINED。
            self._terminate_process_group()
            self._quarantine_reason = "crash_circuit_open"
            self._set_state(State.QUARANTINED, reason="crash_circuit_open")
            return
        if self._monitor_stop.wait(delay):
            return
        if self._launch_params is None:
            self._set_state(State.FAILED, reason="staging_unavailable")
            return
        try:
            _, manifest, store_path = self._launch_params
            self._spawn(manifest, store_path)
        except Exception:  # noqa: BLE001 - 重启失败按崩溃处理，交给退避策略
            self._set_state(State.FAILED, reason="restart_failed")

    def drain(self, timeout: float = 5.0) -> dict:
        """排空：等在途调用清零；不改变不可变 staging。"""
        previous = self.state
        if previous in (State.READY, State.ACTIVE):
            self._set_state(State.DRAINING)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._inflight_lock:
                if self._inflight == 0:
                    break
            time.sleep(0.01)
        with self._inflight_lock:
            remaining = self._inflight
        if previous in (State.READY, State.ACTIVE):
            self._set_state(previous)
        return {"inflight": remaining, "state": self.state.value}

    def restart(self, timeout: float = 5.0) -> LaunchRecord:
        """热重启：复用同一不可变 staging，换新的 launch_instance_id。"""
        if self._launch_params is None:
            raise SupervisorError("not_started")
        self.drain(timeout=timeout)
        self._terminate_process_group()
        _, manifest, store_path = self._launch_params
        self._spawn(manifest, store_path)
        return self.record

    def _phase(self, name: str, detail: dict | None = None):
        if self.phase_hook is None:
            return
        payload = dict(detail or {})
        if self.record is not None and name in {"provider_started", "ready"}:
            payload.update(asdict(self.record))
        self.phase_hook(name, payload)

    def _check_compatible(self, manifest: dict) -> None:
        """启动前把"能不能在这台宿主上跑"判清楚，不允许带着不兼容清单起进程。

        这些检查原先只在 ProviderSupervisor 的 _stage 里，fixture 路径完全没有，
        于是四种不兼容包都能一路走到 spawn 才失败（或干脆跑起来）。
        """
        if manifest.get("entrypoint_id") != self.ENTRYPOINT_ID:
            raise SupervisorError("entrypoint_invalid")
        context = describe_context()
        for key in ("os", "cpu", "python_abi"):
            if manifest.get(key) != context[key]:
                raise SupervisorError("runtime_abi_mismatch", key)
        if manifest.get("runtime_digest") != runtime_digest():
            raise SupervisorError("runtime_identity_mismatch")
        if (manifest.get("sdk_version") not in SUPPORTED_SDK_VERSIONS
                or manifest.get("rpc_version") != RPC_VERSION
                or manifest.get("task_schema") != TASK_SCHEMA
                or manifest.get("result_schema") != RESULT_SCHEMA):
            raise SupervisorError("contract_version_unsupported")

    def _stage(self, source: Path, manifest: dict, signature: dict, trusted_keys, *,
               expected_namespace: str, expected_channel: str):
        verified = verify_engine_manifest_signature(
            manifest, signature, trusted_keys=trusted_keys,
            expected_namespace=expected_namespace, expected_channel=expected_channel)
        self._phase("verified", {"manifest_sha256": verified.manifest_sha256})
        self._check_compatible(manifest)
        built = build_payload_inventory(source, manifest)
        inventory = built["inventory"]
        expected_payload_digest = built["self_payload_digest"]
        target = self.workdir / "runtime" / verified.manifest_sha256
        if target.exists():
            raise SupervisorError("staging_exists")
        target.mkdir(parents=True, mode=0o700)
        try:
            for item in inventory:
                src = source / item["path"]
                dst = target / item["path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                if dst.stat().st_size != item["size"] or hashlib.sha256(dst.read_bytes()).hexdigest() != item["sha256"]:
                    raise SupervisorError("staging_byte_mismatch")
            (target / "ENGINE-MANIFEST.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            (target / "ENGINE-MANIFEST.sig").write_text(json.dumps(signature, sort_keys=True))
            self._phase("staged", {"staging_dir": str(target), "workdir": str(self.workdir)})
            # phase hook 之后必须复核落盘字节。staging 是对"已验签字节"的承诺，
            # 任何在 verify 与 launch 之间改动文件的行为都要在这里被拒（ValueError）。
            actual = build_payload_inventory(target, manifest)
            if actual["self_payload_digest"] != expected_payload_digest:
                raise ValueError("staging_byte_mismatch")
            return target, verified
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def _connect(self):
        if not self.socket_path.exists():
            raise SupervisorError("provider_socket_missing")
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(10)
        try:
            conn.connect(str(self.socket_path))
        except (ConnectionRefusedError, FileNotFoundError) as exc:
            conn.close()
            # socket 文件还在但没人 listen：Provider 已经退出。
            # 退出码 2 是"检测到非授权对端后自停"，要如实报成身份问题。
            if self._child is not None and self._child.poll() == 2:
                raise SupervisorError("peer_identity_mismatch") from exc
            raise SupervisorError("provider_socket_unavailable") from exc
        # 走模块属性而不是 from-import 的绑定名：否则 monkeypatch wire.peer_credentials 不生效，
        # 「对端 pid 不符」这条护栏就成了永远通过的摆设。
        creds = __import__("app.runtime.engine_sdk.wire", fromlist=["peer_credentials"]).peer_credentials(conn)
        if creds.uid != os.getuid() or (creds.pid is not None and self.record is not None
                                         and creds.pid != self.record.pid):
            conn.close()
            raise SupervisorError("peer_identity_mismatch")
        hello = self._recv_plain(conn)
        if hello.get("provider_version") != self.record.provider_version if self.record else False:
            conn.close()
            raise SupervisorError("handshake_version_mismatch")
        host_nonce = os.urandom(32)
        try:
            provider_nonce = bytes.fromhex(hello["nonce"])
        except (KeyError, ValueError) as exc:
            conn.close()
            raise SupervisorError("handshake_nonce_invalid") from exc
        host_id = "host"
        transcript = (b"hello" + provider_nonce + host_nonce + host_id.encode() +
                      self._launch_id.encode() + str(hello.get("provider_version")).encode())
        auth = hmac.new(self._token, transcript, hashlib.sha256).hexdigest()
        self._send_plain(conn, {"nonce": host_nonce.hex(), "auth": auth,
                                "host_id": host_id, "launch_id": self._launch_id})
        provider_auth = self._recv_plain(conn)
        expected = hmac.new(self._token, b"auth" + host_nonce + provider_nonce +
                            host_id.encode() + self._launch_id.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(provider_auth.get("auth")), expected):
            conn.close()
            raise SupervisorError("handshake_provider_auth_invalid")
        host_key, provider_key = ipc.session_keys(
            self._token, host_nonce, provider_nonce, "host", "provider",
            transcript={"host_id": "host", "provider_id": "provider",
                        "launch_id": self._launch_id})
        return conn, host_key, provider_key

    @staticmethod
    def _send_plain(sock, value):
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        sock.sendall(struct.pack("!I", len(body)) + body)

    @staticmethod
    def _recv_plain(sock):
        header = ipc._recv_exact(sock, 4)
        length = struct.unpack("!I", header)[0]
        if length > 65536:
            raise SupervisorError("handshake_too_large")
        return ipc.strict_json(ipc._recv_exact(sock, length))

    def start(self, source: Path, manifest: dict, signature: dict, trusted_keys, *,
              expected_namespace: str, expected_channel: str,
              store_path: Path | None = None, generation: int | None = None) -> LaunchRecord:
        if self.record is not None:
            raise SupervisorError("already_started")
        if generation is not None:
            # 首次激活可以显式指定代次；不指定则沿用构造时的 generation。
            self._generation = int(generation)
        self._set_state(State.VERIFYING)
        try:
            staged, verified = self._stage(source, manifest, signature, trusted_keys,
                                           expected_namespace=expected_namespace,
                                           expected_channel=expected_channel)
            self._staged = Path(staged)
            self._base_identity = _identity_from(verified, manifest, self._generation)
            self._launch_params = (self._staged, manifest, store_path)
            self._set_state(State.STAGED)
            return self._spawn(manifest, store_path)
        except Exception:
            # 启动失败不得留下任何 owned 资源（进程 / socket / staging）。
            self._terminate_process_group()
            self._cleanup_owned_paths()
            if self.state is not State.QUARANTINED:
                self._set_state(State.FAILED, reason="start_failed")
            raise

    def _cleanup_owned_paths(self) -> None:
        if self._staged is not None:
            shutil.rmtree(self._staged, ignore_errors=True)
        shutil.rmtree(self.socket_dir, ignore_errors=True)

    def _spawn(self, manifest: dict, store_path: Path | None) -> LaunchRecord:
        """从已冻结的 staging 拉起一代 Provider。start / restart / manual_reset 共用。"""
        if self._staged is None:
            raise SupervisorError("not_staged")
        staged = Path(self._staged)
        provider = staged / self.ENTRYPOINT_FILE
        if not provider.is_file():
            raise SupervisorError("entrypoint_missing")
        # 复用 socket 目录时必须先删掉上一代的 socket / OWNERSHIP，
        # 否则 awaits 会命中残留文件，把"旧 socket 还在"误判成"新一代已就绪"。
        for stale in (self.socket_path, self.socket_dir / "OWNERSHIP"):
            try:
                if stale.exists():
                    stale.unlink()
            except OSError:
                pass
        self._token = os.urandom(32)
        self._launch_id = os.urandom(12).hex()
        (self.socket_dir / "OWNERSHIP").write_text(json.dumps(
            {"uid": os.getuid(), "launch_instance_id": self._launch_id}, sort_keys=True))
        os.chmod(self.socket_dir / "OWNERSHIP", 0o600)
        self._set_state(State.STARTING)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONUNBUFFERED": "1"}
        child = subprocess.Popen(
            [sys.executable, str(provider), str(self.socket_path), str(manifest.get("version")),
             str(os.getpid()), str(store_path or ""), str(self._launch_id)],
            cwd=str(staged), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        self._child = child
        try:
            import psutil
            create_time = psutil.Process(child.pid).create_time()
        except Exception:
            create_time = time.time()
        identity = dict(self._base_identity or {})
        self.record = LaunchRecord(pid=child.pid, launch_instance_id=self._launch_id,
                                   socket_path=str(self.socket_path), staging_dir=str(staged),
                                   manifest_sha256=identity.get("manifest_sha256", ""),
                                   provider_version=str(manifest.get("version")),
                                   package_digest=identity.get("package_digest", ""),
                                   runtime_digest=manifest.get("runtime_digest", runtime_digest()),
                                   sdk_version=str(manifest.get("sdk_version", "1.0")),
                                   members=[{"pid": child.pid,
                                             "start_time": create_time,
                                             "uid": os.getuid(),
                                             "pgid": os.getpgid(child.pid)}],
                                   start_time=create_time, owner_token=self.owner_token,
                                   host_pid=os.getpid())
        try:
            child.stdin.write(struct.pack("!I", len(self._token)) + self._token)
            child.stdin.close()
            deadline = time.monotonic() + 10
            while not self.socket_path.exists():
                if child.poll() is not None:
                    # Provider 检测到非授权对端会主动退出（码 2）：把原因如实传出去，
                    # 而不是笼统报"启动前退出"，否则排障时看不到是身份问题。
                    if child.returncode == 2:
                        raise SupervisorError("peer_identity_mismatch")
                    raise SupervisorError("provider_exited_before_ready")
                if time.monotonic() > deadline:
                    raise SupervisorError("provider_socket_timeout")
                time.sleep(0.01)
            # Ready requires a real authenticated connection and selftest.
            self._phase("provider_started", {"pid": child.pid, "launch_id": self._launch_id,
                                             "workdir": str(self.workdir)})
            result = self.call("selftest")
            if result.get("ok") is not True:
                raise SupervisorError("selftest_failed")
            self._set_state(State.READY)
            self._phase("candidate_ready", {"pid": child.pid, "launch_id": self._launch_id})
            self._start_monitor()
            return self.record
        except Exception:
            self._terminate_process_group()
            raise

    def submit(self, method: str, params: dict | None = None, *, timeout: float = 5,
               context: dict | None = None, optional_fields: dict | None = None) -> _JobFuture:
        """入队一次调用，由固定 worker 池消费。

        之前每个 submit 各起一个线程，`max_queue` 只是计数器：排队超时、排队取消、
        `max_inflight` 限流全都不成立。现在 pending 列表就是真队列，
        取活时才判定排队是否已超时——**过期的活绝不派发**。
        """
        if self.record is None:
            raise SupervisorError("not_started")
        if self.state is State.QUARANTINED:
            raise SupervisorError("worker_quarantined")
        if timeout <= 0 or timeout > 60:
            raise SupervisorError("invalid_deadline")
        _validate_dispatch_params(params)
        now = time.monotonic()
        ctx = dict(context or {})
        future = _JobFuture()
        future._on_cancel = self._drop_pending
        operation_id = str(ctx.get("operation_id") or future.request_id)
        future.request_id = operation_id
        future.optional_fields = optional_fields
        job = _Job(method=method, params=dict(params or {}), timeout=float(timeout),
                   context=ctx, optional_fields=optional_fields, future=future,
                   enqueued_at=now, deadline_at=now + float(timeout),
                   operation_id=operation_id)
        with self._queue_lock:
            # max_queue 是"每个并发槽位的排队深度"，容量 = max_inflight × max_queue。
            # 直接拿 max_queue 当总深度会与"8 路并发、各排 8 个"冲突：后者要 64 个位置。
            if len(self._pending) >= self.max_inflight * self.max_queue:
                raise SupervisorError("queue_full")
            self._pending.append(job)
            self._work_cv.notify()
        self._ensure_workers()
        return future

    def _ensure_workers(self) -> None:
        with self._queue_lock:
            alive = [worker for worker in self._workers if worker.is_alive()]
            self._workers = alive
            missing = self.max_inflight - len(alive)
            for index in range(max(0, missing)):
                worker = threading.Thread(target=self._worker_loop, daemon=True,
                                          name=f"engine-worker-{index}")
                self._workers.append(worker)
                worker.start()

    def _worker_loop(self) -> None:
        while not self._workers_stop.is_set():
            with self._work_cv:
                while not self._pending and not self._workers_stop.is_set():
                    self._work_cv.wait(0.05)
                if self._workers_stop.is_set():
                    return
                job = self._pending.popleft()
            self._run_job(job)

    def _run_job(self, job: _Job) -> None:
        if job.future.done():
            return  # 排队期间已被 cancel()
        remaining = job.deadline_at - time.monotonic()
        if remaining <= 0:
            job.future.set_exception(SupervisorError(
                "queued_deadline_exceeded", category="timeout", effect_state="none"))
            return
        with self._inflight_lock:
            self._inflight += 1
        self._running[job.operation_id] = job
        try:
            # operation_id 必须从头到尾是同一个：cancel() 按它通知 Provider，
            # 而 _context_for 若拿不到就会新生成一个，导致"取消发给了另一个 id"。
            context = dict(job.context)
            context["operation_id"] = job.operation_id
            result = self.call(job.method, job.params, timeout=remaining, context=context,
                               optional_fields=job.optional_fields)
            with self._inflight_lock:
                self._executed += 1
            job.future.set_result(result)
        except BaseException as exc:  # noqa: BLE001 - 原样交给 future
            job.future.set_exception(exc)
        finally:
            self._running.pop(job.operation_id, None)
            with self._inflight_lock:
                self._inflight -= 1

    def _drop_pending(self, request_id: str) -> bool:
        """把排队中的 job 摘掉。幂等：Future.cancel() 与 supervisor.cancel() 都可能走到这里。"""
        with self._queue_lock:
            for index, job in enumerate(self._pending):
                if job.operation_id == request_id:
                    del self._pending[index]
                    return True
        return False

    def cancel(self, request_id: str) -> bool:
        """取消。排队中的直接撤销；运行中的标记后通知 Provider 协作退出。

        两处必须分开：排队中的取消不能占用返回通道，运行中的取消不能只在本进程置标记——
        Provider 不知道就会一直跑下去，最终变成"取消成功但副作用照样发生"。
        """
        if not isinstance(request_id, str) or not request_id:
            return False
        with self._queue_lock:
            queued = next((job for job in self._pending if job.operation_id == request_id), None)
        if queued is not None:
            self._event("cancel_requested", request_id=request_id, phase="queued")
            return queued.future.cancel()
        if request_id not in self._running:
            return False
        self._cancel_requested.add(request_id)
        self._event("cancel_requested", request_id=request_id, phase="running")
        self._notify_provider_cancel(request_id)
        return True

    def _notify_provider_cancel(self, operation_id: str) -> None:
        """用独立短连接投递取消标记。失败不代表取消失败：运行中的调用仍会在此侧判定。"""
        conn = None
        try:
            conn, host_key, provider_key = self._connect()
            conn.settimeout(2)
            request = {"schema": RPC_VERSION, "request_id": "cancel-" + operation_id,
                       "method": "cancel", "params": {"operation_id": operation_id},
                       "context": {}}
            conn.sendall(ipc.encode_frame(request, host_key, 0))
            ipc.recv_frame(conn, provider_key, 0)
        except Exception:  # noqa: BLE001 - 通知尽力而为
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    def status(self) -> dict:
        with self._inflight_lock:
            inflight = self._inflight
            queued = self._queue_depth
            executed = self._executed
        return {"state": self.state.value, "queue_depth": queued, "inflight": inflight,
                "executed": executed, "ipc_connections": 0,
                "provider_pid": self.record.pid if self.record else None}

    def heartbeat(self) -> dict:
        """本地计数 + Provider 心跳。心跳自述与实际身份/计数矛盾时立即熔断。

        不能让一个"自称 inflight=-1 / 换了 launch_instance_id"的 worker 继续接单：
        那说明它已经不是在为我们服务的那一代进程了。
        """
        value = self.status()
        value["marker"] = "v1"
        # 每次心跳顺手记下 Provider 派生出的子进程身份：leader 崩了以后，
        # 这是唯一能证明"这些孤儿进程是我们拥有的"的证据（pid + 创建时间）。
        self._observe_children()
        try:
            remote = self.call("heartbeat")
        except SupervisorError as exc:
            if exc.code in ("queue_full", "invalid_deadline"):
                return value
            if exc.code == "worker_quarantined":
                return value
            self.quarantine(f"heartbeat_failed:{exc.code}")
            return self.status()
        issue = _heartbeat_issue(remote, self.identity.get("launch_instance_id"))
        if issue is not None:
            # 自述矛盾必须让调用方看见：静默返回一份"看起来正常"的 status，
            # 等于把"这一代进程已经不是我们那一代"这件事藏起来。
            self.quarantine(issue)
            raise SupervisorError(issue)
        value["remote"] = remote
        return value

    def _context_for(self, method: str, params: dict, provided: dict) -> dict:
        """构造完整的调用上下文（CONTEXT_FIELDS 全集）。

        身份字段由宿主给，不能由调用方或 Provider 猜：先前直接用调用方传入的 dict，
        结果 `decode_result` 要求 20 个字段一个不能少，而调用方通常只给 operation_id。
        """
        now = time.time()
        record = self.record
        context = {
            "engine_id": str(self.identity.get("engine_id") or "test.fake"),
            "package_digest": str(record.package_digest) if record else "",
            # fixture 以一个包摘要同时代表 payload 清单摘要；真实 Provider 应传清单摘要。
            "payload_digest": str(record.package_digest) if record else "",
            "runtime_digest": str(record.runtime_digest) if record else runtime_digest(),
            "provider_version": str(record.provider_version) if record else "",
            "sdk_version": str(record.sdk_version) if record else "1.0",
            "generation": max(1, int(self._generation)),
            "launch_instance_id": self._launch_id,
            "host_id": "host",
            "operation_id": str(provided.get("operation_id") or uuid.uuid4().hex),
            "attempt": max(1, int(provided.get("attempt") or 1)),
            "fence_epoch": max(1, int(provided.get("fence_epoch") or 1)),
            "task_schema": TASK_SCHEMA,
            "result_schema": RESULT_SCHEMA,
            "capability_id": str(provided.get("capability_id") or "runtime.overview"),
            "input_digest": str(provided.get("input_digest") or digest_json(params or {})),
            "target_digest": str(provided.get("target_digest") or digest_json({})),
            "scope": provided.get("scope") if isinstance(provided.get("scope"), dict) else {},
            "policy_digest": str(provided.get("policy_digest") or digest_json({})),
            "deadline": float(provided.get("deadline") or (now + 60)),
        }
        return context

    def call(self, method: str, params: dict | None = None, *, timeout: float = 5,
             context: dict | None = None, return_envelope: bool = False,
             optional_fields: dict | None = None) -> dict:
        if self.state is State.QUARANTINED:
            raise SupervisorError("worker_quarantined")
        if method not in METHODS or method == "shell":
            raise SdkError("sdk_method_unknown")
        _validate_dispatch_params(params)
        params = dict(params or {})
        ctx_in = dict(context or {})
        deadline = ctx_in.get("deadline")
        if deadline is not None:
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
                raise SupervisorError("invalid_deadline")
            if deadline <= time.time():
                # 过期请求一律不派发：宁可报错，也不能让"已超时"的调用真的产生副作用。
                raise SupervisorError("deadline_exceeded", category="timeout")
        if timeout <= 0 or timeout > 60:
            raise SupervisorError("invalid_deadline")
        output_bytes = params.get("output_bytes")
        if isinstance(output_bytes, (int, float)) and not isinstance(output_bytes, bool) \
                and output_bytes > self.max_output_bytes:
            raise SupervisorError("output_limit_exceeded", category="limit")
        # input_digest 只对**会产生副作用的调用**校验：verify/prepare 之类查询带的是
        # 原操作的摘要（与本次 params 必然不同），拿它和当前 params 比对会误杀。
        provided_digest = ctx_in.get("input_digest")
        if provided_digest is not None and method in {"execute", "prepare"}:
            if digest_json(params) != provided_digest:
                # 调用方自报的摘要必须与真实参数一致，否则幂等键就是可伪造的。
                raise SupervisorError("input_digest_mismatch")
        ctx = self._context_for(method, params, ctx_in)
        conn = None
        try:
            conn, host_key, provider_key = self._connect()
            conn.settimeout(timeout)
            request = {"schema": RPC_VERSION, "request_id": ctx["operation_id"],
                       "method": method, "params": params, "context": ctx}
            if return_envelope:
                request["return_envelope"] = True
            if optional_fields:
                request["optional_fields"] = optional_fields
            conn.sendall(ipc.encode_frame(request, host_key, 0))
            payload = ipc.recv_frame(conn, provider_key, 0)
        except ipc.IpcError as exc:
            # 对端消失时 recv 抛的是 frame_truncated（读不到完整的长度/帧体），
            # 本质是"Provider 没了"，必须按断连处理并回收进程树；
            # 若当成普通协议错误向上抛，孤儿进程就没人收。
            if str(exc.code) in {"frame_truncated", "frame_sequence_invalid"}:
                self._terminate_process_group()
                raise SupervisorError("provider_disconnected") from exc
            raise SupervisorError(str(exc.code)) from exc
        except socket.timeout as exc:
            # 超时不做"静默重试"：先按进程级回收把不确定性消掉，再明确要求对账。
            self._recover_after_timeout()
            raise SupervisorError("call_timeout_reconcile_required",
                                  category="timeout") from exc
        except (EOFError, ConnectionResetError, BrokenPipeError) as exc:
            # leader 已经没了（崩溃/被杀）：立刻回收我们观测到的后代进程。
            # 只等 stop() 兜底是不够的——孤儿进程会继续跑，cleanup 永远报不干净。
            self._terminate_process_group()
            raise SupervisorError("provider_disconnected") from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
        if payload.get("cancelled") is True:
            raise SupervisorError("call_cancelled",
                                  effect_state=str(payload.get("effect_state") or "unknown"))
        if payload.get("error"):
            raise SupervisorError(str(payload["error"]))
        return payload

    def _recover_after_timeout(self) -> None:
        """一次调用超时后的恢复：杀进程树，再从同一不可变 staging 重新拉起。

        不这么做的话，一次慢调用会让整个引擎永久不可用——而这恰恰是热升级要避免的。
        恢复失败只把状态标成 FAILED，不吞掉原始超时错误。
        """
        was_running = self.record is not None and self.state in (State.READY, State.ACTIVE)
        self._terminate_process_group()
        if not was_running or self._launch_params is None:
            return
        try:
            _, manifest, store_path = self._launch_params
            self._spawn(manifest, store_path)
        except Exception:  # noqa: BLE001 - 恢复失败按不可用处理
            self._set_state(State.FAILED, reason="recover_after_timeout_failed")

    def _terminate_process_group(self) -> None:
        child = self._child
        if child is not None and child.poll() is None:
            try:
                os.killpg(os.getpgid(child.pid), signal.SIGTERM)
                child.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(child.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        # 整组信号不一定覆盖"先 setsid 出去"的后代，这里按观测到的身份逐个补刀。
        # 遗留子进程会继续占端口/句柄，cleanup_report 也就永远报不干净。
        self._reap_observed_children()

    def _observe_children(self) -> None:
        """记录 Provider 派生出来的子进程（pid + 创建时间）作为"我们拥有它们"的证据。"""
        child = self._child
        if child is None or child.poll() is not None:
            return
        try:
            import psutil
            parent = psutil.Process(child.pid)
            for entry in parent.children(recursive=True):
                try:
                    self._observed_children.setdefault(entry.pid, entry.create_time())
                except psutil.Error:
                    continue
        except Exception:  # noqa: BLE001 - 观测失败不影响主流程
            return

    def _reap_observed_children(self) -> None:
        if not self._observed_children:
            return
        try:
            import psutil
        except Exception:  # noqa: BLE001
            self._observed_children.clear()
            return
        for pid, create_time in list(self._observed_children.items()):
            try:
                process = psutil.Process(pid)
                if abs(process.create_time() - create_time) > 0.01:
                    # 同 pid 换了进程：不是我们那一个，绝不能杀。
                    self._observed_children.pop(pid, None)
                    continue
                process.kill()
            except psutil.NoSuchProcess:
                self._observed_children.pop(pid, None)
            except psutil.Error:
                continue

    def kill(self) -> None:
        """杀掉 Provider 进程（等价于外部崩溃）。

        注意语义：这是**进程级故障注入**，不是"关闭宿主"——staging、socket 目录、监视线程
        都要留着，交给崩溃策略决定是否自动重启。之前这里顺手停了 monitor 又删了 staging，
        结果 kill 一次引擎就永久起不来，热升级链路等于没有恢复能力。
        """
        self._set_state(State.FAILED, reason="killed")
        self._terminate_process_group()

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._monitor_stop.set()
        # 先停派发：worker 不再取新活，避免 stop 之后又有调用被派到已关闭的 Provider。
        self._workers_stop.set()
        with self._work_cv:
            self._work_cv.notify_all()
        try:
            self.call("drain", {}, timeout=1)
        except Exception:
            pass
        self._terminate_process_group()
        if self.record is not None:
            shutil.rmtree(self.record.staging_dir, ignore_errors=True)
        shutil.rmtree(self.socket_dir, ignore_errors=True)
        self._set_state(State.STOPPED)
