import logging

import requests

from .base import BaseAdapter, NotifyResult

logger = logging.getLogger("app.notify.sms")


class SmsAdapter(BaseAdapter):
    def validate_config(self) -> list[str]:
        errors = []
        if not self.config.get("url"):
            errors.append("url is required")
        return errors

    def send(self, recipient: str, title: str, content: str) -> NotifyResult:
        url = self.config.get("url", "")
        if not url:
            return NotifyResult(False, error_message="url not configured")
        if not recipient:
            return NotifyResult(False, error_message="recipient phone number is required")

        payload = {
            "phone": recipient,
            "title": title,
            "content": content,
            "template_code": self.config.get("template_code", ""),
        }
        headers = {"Content-Type": "application/json"}
        token = self.config.get("token") or self.config.get("secret")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=self.config.get("timeout_seconds", 15))
            if 200 <= resp.status_code < 300:
                return NotifyResult(True, resp.status_code, resp.text)
            return NotifyResult(False, resp.status_code, resp.text, f"HTTP {resp.status_code}")
        except Exception as e:
            return NotifyResult(False, error_message=str(e))
