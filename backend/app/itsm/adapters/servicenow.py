import logging

import requests

from .base import BaseTicketAdapter, TicketResult

logger = logging.getLogger("app.itsm.servicenow")


class ServiceNowAdapter(BaseTicketAdapter):
    def test_connection(self) -> list[str]:
        errors = []
        if not self.config.get("base_url"):
            errors.append("base_url is required")
        if not self.config.get("username") or not self.config.get("password_cipher"):
            errors.append("username/password is required")
        return errors

    def create_ticket(self, payload: dict) -> TicketResult:
        url = f"{self.config['base_url']}/api/now/table/incident"
        auth = (self.config.get("username", ""), self.config.get("password_cipher", ""))
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        body = {
            "short_description": payload.get("title", ""),
            "description": payload.get("description", ""),
            "priority": self._map_priority(payload.get("priority", "P3")),
            "category": payload.get("category", "database"),
            "assignment_group": payload.get("assignee", ""),
        }
        try:
            resp = requests.post(url, json=body, auth=auth, headers=headers, timeout=self.config.get("timeout_seconds", 15))
            data = resp.json()
            if resp.status_code in (200, 201):
                result = data.get("result", {})
                return TicketResult(True, result.get("sys_id", ""), f"{self.config['base_url']}/nav_to.do?uri=incident.do?sys_id={result.get('sys_id', '')}", "New", resp.text)
            return TicketResult(False, error_message=data.get("error", {}).get("message", resp.text))
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def get_ticket(self, external_id: str) -> TicketResult:
        url = f"{self.config['base_url']}/api/now/table/incident/{external_id}"
        auth = (self.config.get("username", ""), self.config.get("password_cipher", ""))
        try:
            resp = requests.get(url, auth=auth, headers={"Accept": "application/json"}, timeout=self.config.get("timeout_seconds", 15))
            data = resp.json()
            if resp.status_code == 200:
                result = data.get("result", {})
                return TicketResult(True, external_id, "", result.get("state", ""), resp.text)
            return TicketResult(False, error_message=str(data))
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def update_ticket(self, external_id: str, payload: dict) -> TicketResult:
        url = f"{self.config['base_url']}/api/now/table/incident/{external_id}"
        auth = (self.config.get("username", ""), self.config.get("password_cipher", ""))
        try:
            resp = requests.patch(url, json=payload, auth=auth, headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=self.config.get("timeout_seconds", 15))
            return TicketResult(resp.status_code in (200, 204), external_id, "", "", resp.text)
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def close_ticket(self, external_id: str, payload: dict) -> TicketResult:
        return self.update_ticket(external_id, {"state": "Closed", "close_notes": payload.get("resolution", "")})

    def _map_priority(self, p: str) -> str:
        return {"P1": "1", "P2": "2", "P3": "3", "P4": "4"}.get(p, "3")
