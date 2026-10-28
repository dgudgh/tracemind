from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


class ScriptBootstrapTest(unittest.TestCase):
    def test_scripts_importing_backend_app_bootstrap_the_backend_path_first(self):
        failures = []
        for path in sorted(SCRIPTS.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            app_positions = [
                position
                for marker in ("\nfrom app", "\nimport app")
                if (position := source.find(marker)) >= 0
            ]
            if not app_positions:
                continue
            bootstrap_positions = [
                position
                for marker in ("add_backend_to_path()", "sys.path.insert")
                if (position := source.find(marker)) >= 0
            ]
            if not bootstrap_positions or min(bootstrap_positions) > min(app_positions):
                failures.append(path.name)

        self.assertEqual(failures, [], f"Scripts missing backend path bootstrap: {failures}")


if __name__ == "__main__":
    unittest.main()
