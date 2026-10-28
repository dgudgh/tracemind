import logging

import requests

from .base import BaseTicketAdapter, TicketResult

logger = logging.getLogger("app.itsm.webhook")


class WebhookTicketAdapter(BaseTicketAdapter):
    def test_connection(self) -> list[str]:
        errors = []
        if not self.config.get("base_url"):
            errors.append("base_url (webhook URL) is required")
        return errors

    def create_ticket(self, payload: dict) -> TicketResult:
        url = self.config.get("base_url", "")
        secret = self.config.get("token_cipher", "")
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=self.config.get("timeout_seconds", 15))
            if 200 <= resp.status_code < 300:
                data = resp.json() if resp.text else {}
                return TicketResult(True, str(data.get("id", "")), data.get("url", ""), data.get("status", "created"), resp.text)
            return TicketResult(False, error_message=f"HTTP {resp.status_code}: {resp.text[:500]}")
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def get_ticket(self, external_id: str) -> TicketResult:
        url = f"{self.config.get('base_url', '')}/{external_id}"
        secret = self.config.get("token_cipher", "")
        headers = {}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        try:
            resp = requests.get(url, headers=headers, timeout=self.config.get("timeout_seconds", 15))
            if resp.status_code == 200:
                data = resp.json() if resp.text else {}
                return TicketResult(True, external_id, "", data.get("status", ""), resp.text)
            return TicketResult(False, error_message=f"HTTP {resp.status_code}")
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def update_ticket(self, external_id: str, payload: dict) -> TicketResult:
        url = f"{self.config.get('base_url', '')}/{external_id}"
        secret = self.config.get("token_cipher", "")
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        try:
            resp = requests.put(url, json=payload, headers=headers, timeout=self.config.get("timeout_seconds", 15))
            return TicketResult(200 <= resp.status_code < 300, external_id, "", "", resp.text)
        except Exception as e:
            return TicketResult(False, error_message=str(e))

    def close_ticket(self, external_id: str, payload: dict) -> TicketResult:
        return self.update_ticket(external_id, {"status": "closed", "resolution": payload.get("resolution", "")})
