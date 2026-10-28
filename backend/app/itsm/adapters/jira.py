import logging

import requests

from .base import (
    OUTCOME_BLOCKED,
    OUTCOME_CONFIRMED,
    OUTCOME_FAILED,
    OUTCOME_UNKNOWN,
    BaseTicketAdapter,
    TicketResult,
)

logger = logging.getLogger("app.itsm.jira")

#: Status names treated as terminal. Jira instances differ, and "Done" is the only
#: terminal state many workflows expose, so refusing it would block legitimate closes.
CLOSED_STATES = {"closed", "done", "resolved", "complete", "completed", "已关闭", "已完成"}


class JiraAdapter(BaseTicketAdapter):
    def test_connection(self) -> list[str]:
        errors = []
        if not self.config.get("base_url"):
            errors.append("base_url is required")
        if not self.config.get("token_cipher"):
            errors.append("token (API token) is required")
        return errors

    def create_ticket(self, payload: dict) -> TicketResult:
        url = f"{self.config['base_url']}/rest/api/2/issue"
        token = self.config.get("token_cipher", "")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        project_key = self.config.get("extra_config", {}).get("project_key", "MES") if isinstance(self.config.get("extra_config"), dict) else "MES"
        body = {
            "fields": {
                "project": {"key": project_key},
                "summary": payload.get("title", ""),
                "description": {"type": "text", "version": 1, "text": payload.get("description", "")},
                "issuetype": {"name": self.config.get("extra_config", {}).get("issue_type", "Task") if isinstance(self.config.get("extra_config"), dict) else "Task"},
                "priority": {"name": self._map_priority(payload.get("priority", "P3"))},
                "assignee": {"name": payload.get("assignee", "")} if payload.get("assignee") else None,
            }
        }
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=self.config.get("timeout_seconds", 15))
            data = resp.json()
            if resp.status_code in (200, 201):
                return TicketResult(True, data.get("key", ""), f"{self.config['base_url']}/browse/{data.get('key', '')}", "To Do", resp.text)
            return TicketResult(False, error_message=data.get("errorMessages", [resp.text])[0] if data.get("errorMessages") else resp.text)
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def get_ticket(self, external_id: str) -> TicketResult:
        url = f"{self.config['base_url']}/rest/api/2/issue/{external_id}"
        token = self.config.get("token_cipher", "")
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=self.config.get("timeout_seconds", 15))
            data = resp.json()
            if resp.status_code == 200:
                return TicketResult(True, external_id, f"{self.config['base_url']}/browse/{external_id}", data.get("fields", {}).get("status", {}).get("name", ""), resp.text)
            return TicketResult(False, error_message=str(data))
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def update_ticket(self, external_id: str, payload: dict) -> TicketResult:
        url = f"{self.config['base_url']}/rest/api/2/issue/{external_id}"
        token = self.config.get("token_cipher", "")
        try:
            resp = requests.put(url, json=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, timeout=self.config.get("timeout_seconds", 15))
            return TicketResult(resp.status_code in (200, 204), external_id, "", "", resp.text)
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def close_ticket(self, external_id: str, payload: dict) -> TicketResult:
        """Move the issue to a terminal state and prove it landed there.

        The previous version returned ``Closed`` whenever the transition list could be read,
        including when no close/done transition existed at all and when the POST was rejected.
        The service layer closed the platform ticket and the source alert on that answer, so a
        misconfigured Jira workflow silently retired live alerts. Success now requires a
        post-transition read that shows a terminal state.
        """
        url = f"{self.config['base_url']}/rest/api/2/issue/{external_id}/transitions"
        token = self.config.get("token_cipher", "")
        headers = {"Authorization": f"Bearer {token}"}
        timeout = self.config.get("timeout_seconds", 15)
        try:
            listing = requests.get(url, headers=headers, timeout=timeout)
            if listing.status_code != 200:
                return TicketResult(
                    False, external_id, error_message=f"读取状态转换失败：HTTP {listing.status_code}",
                    response_body=listing.text, outcome=OUTCOME_FAILED,
                )
            transitions = listing.json().get("transitions", [])
            close_trans = next(
                (t for t in transitions
                 if "close" in t.get("name", "").lower() or "done" in t.get("name", "").lower()),
                None,
            )
            if close_trans is None:
                # Either the workflow forbids closing or it already happened. Both look
                # identical from the transition list, so ask the issue instead of guessing.
                current = self._read_status_name(external_id, headers, timeout)
                if current and current.lower() in CLOSED_STATES:
                    return TicketResult(True, external_id, "", current, "", outcome=OUTCOME_CONFIRMED)
                return TicketResult(
                    False, external_id, "", current or "", "",
                    error_message="Jira 工作流未提供关闭/完成转换，工单未关闭",
                    outcome=OUTCOME_BLOCKED,
                )
            posted = requests.post(
                url, json={"transition": {"id": close_trans["id"]}}, headers=headers, timeout=timeout,
            )
            if posted.status_code not in (200, 204):
                return TicketResult(
                    False, external_id, error_message=f"提交关闭转换失败：HTTP {posted.status_code}",
                    response_body=posted.text, outcome=OUTCOME_FAILED,
                )
            current = self._read_status_name(external_id, headers, timeout)
            if current is None:
                return TicketResult(
                    False, external_id, response_body=posted.text,
                    error_message="已提交关闭转换，但无法回读最终状态，按未确认处理",
                    outcome=OUTCOME_UNKNOWN,
                )
            if current.lower() not in CLOSED_STATES:
                return TicketResult(
                    False, external_id, "", current, posted.text,
                    error_message=f"已提交转换，但工单状态仍为 {current}",
                    outcome=OUTCOME_FAILED,
                )
            return TicketResult(True, external_id, "", current, posted.text, outcome=OUTCOME_CONFIRMED)
        except Exception as e:
            return TicketResult(False, error_message=str(e), outcome=OUTCOME_FAILED)

    def _read_status_name(self, external_id: str, headers: dict, timeout: int) -> str | None:
        """Current status name, or None when it cannot be read (never a guess)."""
        resp = requests.get(
            f"{self.config['base_url']}/rest/api/2/issue/{external_id}",
            params={"fields": "status"}, headers=headers, timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        try:
            return (resp.json().get("fields", {}).get("status", {}) or {}).get("name")
        except ValueError:
            return None

    def _map_priority(self, p: str) -> str:
        return {"P1": "Highest", "P2": "High", "P3": "Medium", "P4": "Low"}.get(p, "Medium")
