"""Synthetic production-namespace endpoint checks, without E07 acceptance."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.runtime.engine_packs.management import ManagementError, management_socket_path, request

ROOT = Path(__file__).resolve().parents[2]
WORKER = '''import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from app.runtime.engine_packs.management import management_socket_path,serve
class Probe:
    mode='production'
    def __init__(self,root,gid):
        self.root=Path(root);self.socket_path=management_socket_path(self.root)
        self.service_uid=os.getuid();self.admin_gid=gid
    def dispatch(self,method,args,*,peer):
        return {'probe':True,'peer_uid':peer[0]}
    def _audit(self,**kw):pass
serve(Probe(sys.argv[2],int(sys.argv[3])))
'''


def _worker(tmp_path, gid):
    repository = (tmp_path / "repository").resolve()
    repository.mkdir(mode=0o700)
    script = tmp_path / "transport-probe.py"
    script.write_text(WORKER)
    process = subprocess.Popen([sys.executable, str(script), str(ROOT / "backend"),
                                str(repository), str(gid)], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return repository, process


def test_current_group_endpoint_and_descriptor_guards(tmp_path):
    repository, process = _worker(tmp_path, os.getgid())
    path = management_socket_path(repository)
    try:
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            if process.poll() is not None: raise AssertionError(process.communicate())
            time.sleep(0.02)
        assert path.exists()
        response = request(path, "status", {}, expected_server_uid=os.getuid(),
                           expected_repository_root=repository)
        assert response["ok"] is True and response["result"]["peer_uid"] == os.getuid()
        with pytest.raises(ManagementError, match="management_repository_root_required"):
            request(path, "status", {}, expected_server_uid=os.getuid())
        with pytest.raises(ManagementError, match="management_endpoint_descriptor_invalid"):
            request(path, "status", {}, expected_server_uid=os.getuid() + 1,
                    expected_repository_root=repository)
        with pytest.raises(ManagementError, match="management_endpoint_descriptor_invalid"):
            request(path, "status", {}, expected_server_uid=os.getuid(),
                    expected_repository_root=repository.parent)
        descriptor = path.parent / "ENDPOINT.json"
        descriptor.chmod(0o666)
        with pytest.raises(ManagementError, match="management_endpoint_descriptor_unsafe"):
            request(path, "status", {}, expected_server_uid=os.getuid(),
                    expected_repository_root=repository)
        descriptor.chmod(0o640)
        current = json.loads(descriptor.read_text())
        descriptor.write_text(json.dumps({**current, "server_pid": current["server_pid"] + 1}))
        descriptor.chmod(0o640)
        with pytest.raises(ManagementError, match="management_server_identity_mismatch"):
            request(path, "status", {}, expected_server_uid=os.getuid(),
                    expected_repository_root=repository)
        descriptor.write_text(json.dumps(current)); descriptor.chmod(0o640)
        path.chmod(0o666)
        with pytest.raises(ManagementError, match="management_socket_unsafe"):
            request(path, "status", {}, expected_server_uid=os.getuid(),
                    expected_repository_root=repository)
        path.chmod(0o660)
    finally:
        process.terminate(); process.wait(timeout=5)
        shutil.rmtree(path.parent, ignore_errors=True)


def test_unavailable_admin_group_fails_closed(tmp_path):
    repository, process = _worker(tmp_path, os.getgid() + 100000)
    try:
        _, stderr = process.communicate(timeout=10)
        assert process.returncode != 0
        assert "management_admin_group_unavailable" in stderr
        assert not management_socket_path(repository).exists()
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=5)
        shutil.rmtree(management_socket_path(repository).parent, ignore_errors=True)
