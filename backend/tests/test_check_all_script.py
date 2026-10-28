from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_all", ROOT / "scripts" / "check_all.py")
assert SPEC and SPEC.loader
CHECK_ALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK_ALL)


class PackageManagerCommandTest(unittest.TestCase):
    def test_prefers_npm_when_both_package_managers_are_available(self):
        with patch.object(CHECK_ALL, "command_exists", return_value=True):
            self.assertIn(CHECK_ALL.package_manager_command(), {"npm", "npm.cmd"})

    def test_falls_back_to_pnpm_when_npm_is_unavailable(self):
        def available(command: str) -> bool:
            return command in {"pnpm", "pnpm.cmd"}

        with patch.object(CHECK_ALL, "command_exists", side_effect=available):
            self.assertIn(CHECK_ALL.package_manager_command(), {"pnpm", "pnpm.cmd"})


class DiscoveryDeliveryGateTest(unittest.TestCase):
    def test_fast_always_runs_backend_and_frontend_delivery_gates(self):
        calls = []
        def record(name, cmd, cwd, filter_git_line_endings):
            calls.append((name,cmd,cwd))
            return 0
        with patch.object(CHECK_ALL.sys, "argv", ["check_all.py", "--fast"]), patch.object(CHECK_ALL,"run_step",side_effect=record):
            self.assertEqual(CHECK_ALL.main(), 0)
        gates = {name:(cmd,cwd) for name,cmd,cwd in calls if name.startswith("discovery delivery")}
        self.assertEqual(set(gates), {"discovery delivery backend","discovery delivery frontend"})
        cmd,cwd = gates["discovery delivery backend"]
        self.assertEqual(cmd[:3], [CHECK_ALL.sys.executable,"-m","pytest"])
        self.assertIn("backend/tests/test_discovery_delivery_matrix.py", cmd)
        self.assertEqual(cwd, ROOT)
        frontend_cmd,frontend_cwd = gates["discovery delivery frontend"]
        self.assertEqual(frontend_cmd[:2], ["node","--test"])
        self.assertIn("tests/asset-topology-layout.test.mjs",frontend_cmd)
        self.assertEqual(frontend_cwd, ROOT / "frontend")
        self.assertFalse(any("check_all.py" in cmd for _,cmd,_ in calls))

    def test_a_failed_delivery_gate_fails_the_whole_validation(self):
        for failing in ("discovery delivery backend","discovery delivery frontend"):
            with self.subTest(gate=failing), patch.object(CHECK_ALL.sys, "argv", ["check_all.py", "--fast"]), patch.object(CHECK_ALL,"run_step",side_effect=lambda name,*args: 1 if name == failing else 0):
                self.assertEqual(CHECK_ALL.main(), 1)


if __name__ == "__main__":
    unittest.main()
