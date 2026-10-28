import logging

import requests

from .base import BaseAdapter, NotifyResult

logger = logging.getLogger("app.notify.webhook")


class WebhookAdapter(BaseAdapter):
    def validate_config(self) -> list[str]:
        errors = []
        if not self.config.get("url"):
            errors.append("url is required")
        return errors

    def send(self, recipient: str, title: str, content: str) -> NotifyResult:
        url = self.config.get("url", "")
        if not url:
            return NotifyResult(False, error_message="url not configured")

        payload = {"title": title, "content": content, "recipient": recipient}
        headers = {"Content-Type": "application/json"}
        secret = self.config.get("secret")
        if secret:
            headers["Authorization"] = f"Bearer {secret}"

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            if 200 <= resp.status_code < 300:
                return NotifyResult(True, resp.status_code, resp.text)
            return NotifyResult(False, resp.status_code, resp.text, f"HTTP {resp.status_code}")
        except Exception as e:
            return NotifyResult(False, error_message=str(e))
