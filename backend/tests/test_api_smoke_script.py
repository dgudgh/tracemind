from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_api_smoke.py"
SPEC = importlib.util.spec_from_file_location("check_api_smoke", SCRIPT_PATH)
assert SPEC and SPEC.loader
check_api_smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_api_smoke
SPEC.loader.exec_module(check_api_smoke)


class ApiSmokeScriptTest(unittest.TestCase):
    def test_bearer_headers_do_not_put_tokens_in_urls(self):
        self.assertEqual(check_api_smoke.bearer_headers(None), {})
        self.assertEqual(
            check_api_smoke.bearer_headers("token-value"),
            {"Authorization": "Bearer token-value"},
        )

    @patch.object(check_api_smoke.urllib.request, "urlopen")
    def test_openapi_request_uses_bearer_header(self, urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({"paths": {}}).encode()
        urlopen.return_value.__enter__.return_value = response

        payload = check_api_smoke.fetch_json(
            "http://localhost/openapi.json",
            timeout=1,
            token="token-value",
        )

        request = urlopen.call_args.args[0]
        self.assertEqual(payload, {"paths": {}})
        self.assertEqual(request.get_header("Authorization"), "Bearer token-value")


if __name__ == "__main__":
    unittest.main()
