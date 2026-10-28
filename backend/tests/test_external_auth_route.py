import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.api.v1.auth import LoginRequest, external_login


class ExternalAuthRouteTest(unittest.TestCase):
    def test_external_login_uses_identity_chain_and_returns_application_token(self):
        expected = {"token": "identity-token", "username": "directory.user", "expires_in": 86400}
        with patch("app.identity.service.login_external", return_value=expected) as login:
            result = external_login(LoginRequest(username="directory.user", password="secret"), db=object())

        self.assertEqual(result, expected)
        login.assert_called_once()

    def test_external_login_rejects_invalid_directory_credentials(self):
        with patch("app.identity.service.login_external", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                external_login(LoginRequest(username="directory.user", password="wrong"), db=object())
        self.assertEqual(raised.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
