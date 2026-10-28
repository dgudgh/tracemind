import logging

import requests

from .base import BaseTicketAdapter, TicketResult

logger = logging.getLogger("app.itsm.zentao")


class ZenTaoAdapter(BaseTicketAdapter):
    def test_connection(self) -> list[str]:
        errors = []
        if not self.config.get("base_url"):
            errors.append("base_url is required")
        return errors

    def create_ticket(self, payload: dict) -> TicketResult:
        url = f"{self.config['base_url']}/api.php/v1/bugs"
        token = self.config.get("token_cipher", "")
        headers = {"Content-Type": "application/json", "Token": token}
        body = {
            "title": payload.get("title", ""),
            "steps": payload.get("description", ""),
            "severity": self._map_severity(payload.get("priority", "P3")),
            "pri": self._map_priority(payload.get("priority", "P3")),
            "assignedTo": payload.get("assignee", ""),
        }
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=self.config.get("timeout_seconds", 15))
            data = resp.json()
            if resp.status_code in (200, 201):
                bug_id = data.get("id", data.get("result", {}).get("id", ""))
                return TicketResult(True, str(bug_id), f"{self.config['base_url']}/bug-view-{bug_id}.html", "active", resp.text)
            return TicketResult(False, error_message=str(data.get("message", resp.text)))
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def get_ticket(self, external_id: str) -> TicketResult:
        url = f"{self.config['base_url']}/api.php/v1/bugs/{external_id}"
        token = self.config.get("token_cipher", "")
        try:
            resp = requests.get(url, headers={"Token": token}, timeout=self.config.get("timeout_seconds", 15))
            data = resp.json()
            if resp.status_code == 200:
                return TicketResult(True, external_id, "", data.get("status", ""), resp.text)
            return TicketResult(False, error_message=str(data))
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def update_ticket(self, external_id: str, payload: dict) -> TicketResult:
        url = f"{self.config['base_url']}/api.php/v1/bugs/{external_id}"
        token = self.config.get("token_cipher", "")
        try:
            resp = requests.put(url, json=payload, headers={"Content-Type": "application/json", "Token": token}, timeout=self.config.get("timeout_seconds", 15))
            return TicketResult(resp.status_code in (200, 204), external_id, "", "", resp.text)
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def close_ticket(self, external_id: str, payload: dict) -> TicketResult:
        return self.update_ticket(external_id, {"status": "closed", "resolution": payload.get("resolution", "")})

    def _map_priority(self, p: str) -> int:
        return {"P1": 1, "P2": 2, "P3": 3, "P4": 4}.get(p, 3)

    def _map_severity(self, p: str) -> int:
        return {"P1": 1, "P2": 2, "P3": 3, "P4": 4}.get(p, 3)
