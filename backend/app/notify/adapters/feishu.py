import logging

import requests

from .base import BaseAdapter, NotifyResult

logger = logging.getLogger("app.notify.feishu")


class FeishuAdapter(BaseAdapter):
    def validate_config(self) -> list[str]:
        errors = []
        if not self.config.get("webhook_url"):
            errors.append("webhook_url is required")
        return errors

    def send(self, recipient: str, title: str, content: str) -> NotifyResult:
        url = self.config.get("webhook_url", "")
        if not url:
            return NotifyResult(False, error_message="webhook_url not configured")

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}},
                "elements": [{"tag": "markdown", "content": content}],
            },
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            body = resp.text
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0 or data.get("StatusCode") == 0:
                    return NotifyResult(True, resp.status_code, body)
                return NotifyResult(False, resp.status_code, body, data.get("msg", ""))
            return NotifyResult(False, resp.status_code, body, f"HTTP {resp.status_code}")
        except Exception as e:
            return NotifyResult(False, error_message=str(e))
