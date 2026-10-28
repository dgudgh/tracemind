import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.automation.api import _actor, approve, execute, submit_plan
from app.automation.schemas import ApprovalDecision, ApprovalSubmit, ExecutionRequest


def authenticated_request(username: str):
    return SimpleNamespace(state=SimpleNamespace(auth={"username": username}))


class AutomationApiIdentityTest(unittest.TestCase):
    def test_submit_ignores_forged_requester_and_uses_session_identity(self):
        automation = MagicMock()
        with patch("app.automation.api._service", return_value=automation):
            submit_plan(
                7,
                ApprovalSubmit(requester_id="forged-user"),
                authenticated_request("session-user"),
                db=object(),
            )

        automation.submit_plan.assert_called_once_with(7, requester_id="session-user", expires_at=None)

    def test_approve_and_execute_ignore_client_supplied_actor(self):
        automation = MagicMock()
        with patch("app.automation.api._service", return_value=automation):
            approve(
                8,
                ApprovalDecision(approver_id="forged-approver"),
                authenticated_request("real-approver"),
                db=object(),
            )
            execute(
                8,
                ExecutionRequest(executor_id="forged-executor"),
                authenticated_request("real-executor"),
                db=object(),
            )

        automation.approve.assert_called_once_with(8, "real-approver", None)
        automation.execute.assert_called_once_with(8, "real-executor", None)

    def test_actor_requires_authenticated_username(self):
        with self.assertRaises(HTTPException) as raised:
            _actor(SimpleNamespace(state=SimpleNamespace(auth={})))
        self.assertEqual(raised.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
